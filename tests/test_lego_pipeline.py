from __future__ import annotations

import math
import subprocess
import sys

import pandas as pd
import pytest

from lego_pipeline import (
    FINAL_COLUMNS,
    PipelineContext,
    PreviousAnchor,
    STAGES,
    build_snapshot_frame,
    final_dataframe,
    invalidate_from,
    prepare_raw_frame,
    run_stage,
)


def run_all(
    *,
    price: float,
    holdings: float,
    fix_c: float = 1500.0,
    diff: float = 30.0,
    dna_code: str = "bypass:100",
    anchor: PreviousAnchor = PreviousAnchor(),
):
    raw = build_snapshot_frame(
        snapshot_at="2026-07-16T12:00:00Z",
        symbol="AAPL",
        price=price,
        holdings=holdings,
    )
    context = PipelineContext(
        fix_c=fix_c,
        diff=diff,
        dna_code=dna_code,
        anchor=anchor,
        run_id="run-1",
        chain_key="chain-1",
    )
    results = {}
    previous = None
    for stage in STAGES:
        previous = run_stage(stage.number, raw, previous, context)
        results[stage.number] = previous
    return raw, context, results, final_dataframe(results[17])


def test_registry_has_exactly_17_standalone_blocks():
    assert tuple(stage.number for stage in STAGES) == tuple(range(1, 18))
    assert tuple(stage.column_name for stage in STAGES) == FINAL_COLUMNS
    for stage in STAGES:
        source = stage.source_code
        assert source.startswith('"""Goal:')
        assert "Quick Start:" in source
        assert "def transform(" in source
        assert 'if __name__ == "__main__":' in source
        assert "from lego_" not in source
        assert "from trade_log" not in source
        compile(source, stage.file_name, "exec")


def test_first_run_builds_exactly_one_new_buy_row_and_zero_ledger():
    _, _, _, final = run_all(price=100.0, holdings=10.0)

    assert tuple(final.columns) == FINAL_COLUMNS
    assert len(final) == 1
    row = final.iloc[0]
    assert row["สถานะ"] == "READY_BUY"
    assert row["DNA step"] == 0
    assert row["DNA signal"] == 1
    assert row["คำสั่ง"] == "BUY"
    assert row["ฝั่ง"] == "BUY"
    assert row["เหตุผล"] == "BELOW_TARGET"
    assert row["จำนวนสั่ง (หุ้น)"] == 5.0
    assert row["มูลค่าพอร์ต (USD)"] == 1000.0
    assert row["ส่วนต่างเป้าหมาย (USD)"] == 500.0
    assert row[list(FINAL_COLUMNS[13:])].tolist() == [0.0, 0.0, 0.0, 0.0]


def test_latest_anchor_only_drives_dna_and_price_path_recurrence():
    anchor = PreviousAnchor(
        row_id="previous",
        version=4,
        dna_step=8,
        price=100.0,
        p0=80.0,
        actual_cumulative=25.0,
    )
    _, _, _, final = run_all(
        price=120.0,
        holdings=15.0,
        fix_c=1500.0,
        anchor=anchor,
    )
    row = final.iloc[0]
    expected_reference = round(1500.0 * math.log(120.0 / 80.0), 2)
    expected_delta = round(1500.0 * (120.0 / 100.0 - 1.0), 2)
    assert row["DNA step"] == 9
    assert row["Rₙ อ้างอิง (USD)"] == expected_reference
    assert row["ΔAₙ ต่อสเต็ป (USD)"] == expected_delta
    assert row["Aₙ สะสม (USD)"] == round(25.0 + expected_delta, 2)
    assert row["Eₙ ส่วนเกินสะสม (USD)"] == round(
        row["Aₙ สะสม (USD)"] - expected_reference, 2
    )


@pytest.mark.parametrize(
    ("dna_code", "price", "holdings", "status", "action", "reason"),
    [
        ("bypass:5", 100.0, 15.1, "PASS_THRESHOLD", "PASS", "WITHIN_THRESHOLD"),
        ("bypass:5", 100.0, 10.0, "READY_BUY", "BUY", "BELOW_TARGET"),
        ("bypass:5", 100.0, 20.0, "READY_SELL", "SELL", "ABOVE_TARGET"),
    ],
)
def test_decision_cases(dna_code, price, holdings, status, action, reason):
    _, _, _, final = run_all(
        price=price,
        holdings=holdings,
        dna_code=dna_code,
    )
    row = final.iloc[0]
    assert row["สถานะ"] == status
    assert row["คำสั่ง"] == action
    assert row["เหตุผล"] == reason
    if action == "PASS":
        assert row["จำนวนสั่ง (หุ้น)"] == 0
        assert pd.isna(row["ฝั่ง"])


def test_dna_zero_forces_pass_even_when_gap_is_large():
    anchor = PreviousAnchor(
        row_id="step-zero",
        version=1,
        dna_step=0,
        price=90.0,
        p0=90.0,
        actual_cumulative=0.0,
    )
    _, _, _, final = run_all(
        price=100.0,
        holdings=10.0,
        dna_code="121012",
        anchor=anchor,
    )
    row = final.iloc[0]
    assert row["DNA step"] == 1
    assert row["DNA signal"] == 0
    assert row["สถานะ"] == "PASS_DNA_ZERO"
    assert row["คำสั่ง"] == "PASS"
    assert row["จำนวนสั่ง (หุ้น)"] == 0


def test_dna_exhaustion_fails_closed():
    anchor = PreviousAnchor(
        row_id="last",
        version=1,
        dna_step=0,
        price=100.0,
        p0=100.0,
        actual_cumulative=0.0,
    )
    raw = build_snapshot_frame(
        snapshot_at="2026-07-16T12:00:00Z",
        symbol="AAPL",
        price=100,
        holdings=10,
    )
    context = PipelineContext(
        fix_c=1500,
        diff=30,
        dna_code="bypass:1",
        anchor=anchor,
    )
    previous = None
    for number in range(1, 5):
        previous = run_stage(number, raw, previous, context)
    with pytest.raises(ValueError, match="outside decoded length"):
        run_stage(5, raw, previous, context)


def test_pipeline_rejects_zero_or_multiple_snapshot_rows():
    with pytest.raises(ValueError, match="exactly one snapshot row"):
        prepare_raw_frame(pd.DataFrame())
    with pytest.raises(ValueError, match="exactly one snapshot row"):
        prepare_raw_frame(pd.DataFrame([{"symbol": "A"}, {"symbol": "B"}]))


def test_rerun_invalidation_removes_current_and_downstream_only():
    _, _, results, _ = run_all(price=100.0, holdings=10.0)
    invalidate_from(results, 8)
    assert set(results) == set(range(1, 8))


def test_all_17_single_files_run_for_a_first_row(tmp_path):
    raw = build_snapshot_frame(
        snapshot_at="2026-07-16T12:00:00Z",
        symbol="AAPL",
        price=100,
        holdings=10,
    )
    raw_path = tmp_path / "snapshot.csv"
    raw.to_csv(raw_path, index=False)
    previous_path = None
    for stage in STAGES:
        script_path = tmp_path / stage.file_name
        output_path = tmp_path / f"step_{stage.number:02d}.csv"
        script_path.write_text(stage.source_code, encoding="utf-8")
        command = [
            sys.executable,
            str(script_path),
            "--raw",
            str(raw_path),
            "--output",
            str(output_path),
        ]
        if previous_path is not None:
            command.extend(["--previous", str(previous_path)])
        if stage.number == 5:
            command.extend(["--dna-code", "bypass:100"])
        if stage.number in {8, 11, 13, 14, 15}:
            command.extend(["--fix-c", "1500"])
        if stage.number == 8:
            command.extend(["--diff", "30"])
        previous_path = output_path
        completed = subprocess.run(
            command,
            cwd=tmp_path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
    final = pd.read_csv(previous_path)
    assert len(final) == 1
    assert tuple(final.columns) == FINAL_COLUMNS
