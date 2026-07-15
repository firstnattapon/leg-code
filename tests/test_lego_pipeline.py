from __future__ import annotations

import math
import subprocess
import sys

import pandas as pd

from lego_pipeline import (
    FINAL_COLUMNS,
    PipelineContext,
    STAGES,
    final_dataframe,
    invalidate_from,
    prepare_raw_frame,
    run_stage,
    what_if_dataframe,
)


def run_all(raw: pd.DataFrame, fix_c: float = 1500.0):
    prepared = prepare_raw_frame(raw)
    context = PipelineContext(fix_c=fix_c)
    results = {}
    previous = None
    for stage in STAGES:
        previous = run_stage(stage.number, prepared, previous, context)
        results[stage.number] = previous
    return prepared, results


def sample_rows() -> pd.DataFrame:
    return pd.json_normalize(
        [
            {
                "created_at": "2026-07-13T14:21:01.347Z",
                "symbol": "aapl",
                "status": "ORDER_SUBMITTED",
                "dna_step": 9,
                "dna_signal": 1,
                "last_price": 321.24,
                "quantity": 4.7,
                "decision": {
                    "action": "BUY",
                    "side": "BUY",
                    "reason": "BELOW_TARGET",
                    "order_qty": 0.03,
                    "value_now_usd": 1510.0,
                },
            },
            {
                "created_at": "2026-07-13T14:20:25.906Z",
                "symbol": "AAPL",
                "status": "PASS_THRESHOLD",
                "dna_step": 8,
                "dna_signal": 1,
                "last_price": 321.525,
                "quantity": 4.7,
                "decision": {
                    "action": "PASS",
                    "side": None,
                    "reason": "WITHIN_THRESHOLD",
                    "order_qty": 0.0,
                    "value_now_usd": 1500.0,
                },
            },
        ],
        sep="_",
    )


def test_registry_has_exactly_17_ordered_columns():
    assert len(STAGES) == 17
    assert tuple(stage.number for stage in STAGES) == tuple(range(1, 18))
    assert tuple(stage.column_name for stage in STAGES) == FINAL_COLUMNS
    for stage in STAGES:
        source = stage.source_code
        assert source.startswith('"""Goal:')
        assert "Quick Start:" in source
        assert "def transform(" in source
        assert 'if __name__ == "__main__":' in source
        assert stage.goal in source
        assert stage.file_name.startswith(f"step_{stage.number:02d}_")
        assert stage.run_fn.__module__.startswith("lego_blocks.step_")
        assert "from lego_" not in source
        assert "from trade_log" not in source
        compile(source, stage.file_name, "exec")


def test_pipeline_dna_decoder_fills_only_truly_missing_logged_signals():
    prepared = prepare_raw_frame(
        pd.DataFrame(
            [
                {"dna_step": 0, "dna_signal": 0},
                {"dna_step": 1},
                {"dna_step": 1, "dna_signal": 2},
                {"dna_step": 8},
            ]
        )
    )
    context = PipelineContext(fix_c=1500.0, dna_code="bypass:2")
    previous = None
    for step in range(1, 6):
        previous = run_stage(step, prepared, previous, context)

    values = previous.frame["DNA signal"].tolist()
    assert values[:2] == [0, 1]
    assert pd.isna(values[2])  # invalid logged data is rejected, never overwritten
    assert pd.isna(values[3])  # decoded sequence has no step 8
    assert previous.provenance["decoder"]["mode"] == "bypass"
    assert previous.provenance["dna_code_stored"] is False


def test_all_17_single_files_run_as_a_complete_cli_chain(tmp_path):
    prepared = prepare_raw_frame(sample_rows())
    raw_path = tmp_path / "raw.csv"
    prepared.to_csv(raw_path, index=False)
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
        assert '"goal":' in completed.stdout
        previous_path = output_path

    standalone_final = pd.read_csv(previous_path)
    assert tuple(standalone_final.columns) == FINAL_COLUMNS
    assert len(standalone_final) == len(prepared)


def test_full_chain_returns_exact_newest_first_contract():
    _, results = run_all(sample_rows())

    final = final_dataframe(results[17])

    assert tuple(final.columns) == FINAL_COLUMNS
    assert len(final) == 2
    assert final.loc[0, "DNA step"] == 9
    assert final.loc[1, "DNA step"] == 8
    assert final.loc[0, "สินทรัพย์"] == "AAPL"
    assert final.loc[0, "ส่วนต่างเป้าหมาย (USD)"] == -10.0


def test_what_if_matches_csv_style_anchor_to_one_cent():
    _, results = run_all(sample_rows())

    what_if = what_if_dataframe(results[17], 1500.0)

    newest = what_if.iloc[0]
    oldest = what_if.iloc[1]
    assert oldest["ราคา Pₙ (USD)"] == 321.525
    assert oldest["Rₙ what-if (USD)"] == 0.0
    assert oldest["ΔAₙ what-if (USD)"] == 0.0
    assert newest["Rₙ what-if (USD)"] == -1.33
    assert newest["ΔAₙ what-if (USD)"] == -1.33
    assert newest["Aₙ what-if สะสม (USD)"] == -1.33
    assert newest["Eₙ what-if สะสม (USD)"] == 0.0


def test_expected_position_never_becomes_verified_holdings():
    raw = pd.DataFrame(
        [
            {
                "created_at": "2026-07-13T14:20:00Z",
                "symbol": "AAPL",
                "status": "ORDER_FILLED_POSITION_PENDING",
                "dna_step": 1,
                "dna_signal": 1,
                "last_price": 100,
                "expected_position_after": 999,
                "market_state_quantity": 5,
                "decision_action": "BUY",
                "decision_side": "BUY",
                "decision_reason": "BELOW_TARGET",
                "decision_order_qty": 1,
                "decision_value_now_usd": 500,
            }
        ]
    )

    _, results = run_all(raw)

    assert final_dataframe(results[17]).loc[0, "จำนวนถือครอง (หุ้น)"] == 5


def test_invalid_dna_values_become_nullable_instead_of_being_rounded():
    raw = sample_rows()
    raw.loc[0, "dna_step"] = -1
    raw.loc[1, "dna_signal"] = 2

    _, results = run_all(raw)
    final = final_dataframe(results[17])

    assert pd.isna(final.loc[0, "DNA step"])
    assert pd.isna(final.loc[1, "DNA signal"])


def test_rerun_invalidation_removes_current_and_downstream_only():
    _, results = run_all(sample_rows())

    invalidate_from(results, 8)

    assert set(results) == set(range(1, 8))


def confirmed_fill_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "created_at": 1,
                "symbol": "AAPL",
                "status": "ORDER_FILLED",
                "side": "BUY",
                "filled_quantity": 2.0,
                "position_reconciled": True,
                "client_order_id": "one",
                "average_filled_price": 10.0,
                "transaction_fee": 0.25,
                "last_price": 999.0,
                "quantity": 2.0,
                "dna_step": 0,
                "dna_signal": 1,
                "decision_action": "BUY",
                "decision_side": "BUY",
                "decision_reason": "BELOW_TARGET",
                "decision_order_qty": 2.0,
                "decision_value_now_usd": 20.0,
            },
            {
                "created_at": 2,
                "symbol": "AAPL",
                "status": "ORDER_NOT_FILLED",
                "side": "SELL",
                "filled_quantity": 0.0,
                "position_reconciled": False,
                "client_order_id": "two",
                "average_filled_price": 12.0,
                "last_price": 888.0,
                "quantity": 2.0,
                "dna_step": 1,
                "dna_signal": 1,
                "decision_action": "SELL",
                "decision_side": "SELL",
                "decision_reason": "ABOVE_TARGET",
                "decision_order_qty": 1.0,
                "decision_value_now_usd": 24.0,
            },
            {
                "created_at": 3,
                "symbol": "AAPL",
                "status": "ORDER_FILLED",
                "side": "SELL",
                "filled_quantity": 1.0,
                "position_reconciled": True,
                "client_order_id": "three",
                "average_filled_price": 12.0,
                "transaction_fee": 0.10,
                "last_price": 777.0,
                "quantity": 1.0,
                "dna_step": 2,
                "dna_signal": 1,
                "decision_action": "SELL",
                "decision_side": "SELL",
                "decision_reason": "ABOVE_TARGET",
                "decision_order_qty": 1.0,
                "decision_value_now_usd": 12.0,
            },
        ]
    )


def test_broker_ledger_uses_execution_not_quote_and_ignores_nonfill():
    _, results = run_all(confirmed_fill_rows(), fix_c=100.0)
    final = final_dataframe(results[17])

    newest = final.iloc[0]
    nonfill = final.iloc[1]
    oldest = final.iloc[2]
    assert oldest["ΔAₙ ต่อสเต็ป (USD)"] == -20.25
    assert oldest["Rₙ อ้างอิง (USD)"] == 0.0
    assert nonfill["ΔAₙ ต่อสเต็ป (USD)"] == 0.0
    assert nonfill["Aₙ สะสม (USD)"] == -20.25
    assert newest["ΔAₙ ต่อสเต็ป (USD)"] == 11.9
    assert newest["Aₙ สะสม (USD)"] == -8.35
    assert newest["Rₙ อ้างอิง (USD)"] == round(100 * math.log(12 / 10), 2)
    assert newest["Eₙ ส่วนเกินสะสม (USD)"] == round(
        newest["Aₙ สะสม (USD)"] - newest["Rₙ อ้างอิง (USD)"], 2
    )


def test_csv_like_no_fill_rows_leave_main_ledger_blank():
    _, results = run_all(sample_rows())
    final = final_dataframe(results[17])

    for column in FINAL_COLUMNS[13:]:
        assert final[column].isna().all()


def test_prepare_raw_frame_drops_unnamed_export_index():
    raw = sample_rows()
    raw.insert(0, "Unnamed: 0", [1, 0])

    prepared = prepare_raw_frame(raw)

    assert "Unnamed: 0" not in prepared
    assert prepared.loc[0, "dna_step"] == 8


def test_empty_trade_collection_can_complete_all_learning_stages():
    prepared, results = run_all(pd.DataFrame())

    assert prepared.empty
    assert final_dataframe(results[17]).empty
    assert tuple(final_dataframe(results[17]).columns) == FINAL_COLUMNS
