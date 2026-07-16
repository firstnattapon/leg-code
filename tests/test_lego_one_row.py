"""Unit + contract tests for the one-new-row LEGO engine."""

from __future__ import annotations

import math

import pytest

import lego_one_row as engine
from lego_one_row import (
    CurrentSnapshot,
    PipelineError,
    PreviousAnchor,
    RunContext,
    StrategyParameters,
    build_decision,
    build_final_document,
    build_snapshot,
    compute_chain_key,
    compute_recurrence,
    compute_row,
    compute_run_id,
    present_row,
)


def make_params(**overrides) -> StrategyParameters:
    base = dict(fix_c=1500.0, diff=0.0, dna_code="bypass:100")
    base.update(overrides)
    return StrategyParameters(**base)


def make_snapshot(**overrides) -> CurrentSnapshot:
    base = dict(
        environment="Test (UAT)",
        account_fingerprint="acct1234abcd",
        symbol="AAPL",
        price=100.0,
        holdings=3.0,
        captured_at="2026-07-16T00:00:00+00:00",
    )
    base.update(overrides)
    return CurrentSnapshot(**base)


def make_ctx(snapshot=None, anchor=None, params=None) -> RunContext:
    snapshot = snapshot or make_snapshot()
    anchor = anchor or PreviousAnchor.genesis()
    params = params or make_params()
    chain_key = compute_chain_key(
        snapshot.environment, snapshot.account_fingerprint, snapshot.symbol, params
    )
    run_id = compute_run_id(chain_key, anchor, snapshot)
    return RunContext(
        run_id=run_id, chain_key=chain_key, snapshot=snapshot, anchor=anchor, params=params
    )


# --------------------------------------------------------------------------- #
# Snapshot construction — no trade log involved
# --------------------------------------------------------------------------- #
def test_build_snapshot_uses_positions_and_quote_only():
    positions = {"positions": [{"symbol": "AAPL", "quantity": "3"}]}
    quote = {"symbol": "AAPL", "last_price": "123.45"}
    snapshot = build_snapshot(
        environment="Test (UAT)",
        account_id="ACCOUNT-XYZ",
        symbol="aapl",
        positions_response=positions,
        quote_response=quote,
        captured_at="2026-07-16T00:00:00+00:00",
    )
    assert snapshot.symbol == "AAPL"
    assert snapshot.holdings == 3.0
    assert snapshot.price == 123.45
    # Account id never survives into the snapshot.
    assert "ACCOUNT-XYZ" not in snapshot.account_fingerprint


def test_build_snapshot_rejects_non_positive_quote():
    with pytest.raises(PipelineError):
        build_snapshot(
            environment="Test (UAT)",
            account_id="ACCT",
            symbol="AAPL",
            positions_response={"positions": []},
            quote_response={"symbol": "AAPL", "last_price": "0"},
        )


def test_missing_position_means_zero_holdings():
    snapshot = build_snapshot(
        environment="Test (UAT)",
        account_id="ACCT",
        symbol="AAPL",
        positions_response={"positions": [{"symbol": "MSFT", "quantity": "5"}]},
        quote_response={"symbol": "AAPL", "last_price": "10"},
    )
    assert snapshot.holdings == 0.0


# --------------------------------------------------------------------------- #
# First (genesis) row
# --------------------------------------------------------------------------- #
def test_first_row_initializes_baseline_to_zero():
    ctx = make_ctx()
    row = compute_row(ctx)
    cols = row.columns
    assert cols["DNA step"] == 0
    assert cols["ราคา Pₙ (USD)"] == 100.0
    assert row.metadata["p0"] == 100.0
    for name in ("Rₙ อ้างอิง (USD)", "ΔAₙ ต่อสเต็ป (USD)", "Aₙ สะสม (USD)", "Eₙ ส่วนเกินสะสม (USD)"):
        assert cols[name] == 0.0


def test_genesis_dna_signal_is_one_and_buy():
    ctx = make_ctx()  # holdings 3 * price 100 = 300, gap 1200 > 0
    row = compute_row(ctx)
    assert row.columns["DNA signal"] == 1
    assert row.columns["สถานะ"] == engine.STATUS_READY_BUY
    assert row.columns["คำสั่ง"] == "BUY"
    assert row.columns["ฝั่ง"] == "BUY"
    assert row.columns["จำนวนสั่ง (หุ้น)"] == 12.0
    assert row.columns["มูลค่าพอร์ต (USD)"] == 300.0
    assert row.columns["ส่วนต่างเป้าหมาย (USD)"] == 1200.0


# --------------------------------------------------------------------------- #
# Anchored row — price-path recurrence from a single prior anchor
# --------------------------------------------------------------------------- #
def test_anchored_recurrence_matches_formula():
    fix_c = 1500.0
    p0, prev_price, prev_actual = 100.0, 110.0, 40.0
    anchor = PreviousAnchor(
        exists=True,
        version=3,
        row_id="row-3",
        dna_step=3,
        p0=p0,
        prev_price=prev_price,
        prev_actual=prev_actual,
    )
    price = 121.0
    rec = compute_recurrence(price, anchor, fix_c)
    assert rec.reference == pytest.approx(fix_c * math.log(price / p0))
    assert rec.delta_actual == pytest.approx(fix_c * (price / prev_price - 1.0))
    assert rec.actual_cumulative == pytest.approx(prev_actual + rec.delta_actual)
    assert rec.excess == pytest.approx(rec.actual_cumulative - rec.reference)

    ctx = make_ctx(snapshot=make_snapshot(price=price), anchor=anchor)
    row = compute_row(ctx)
    assert row.columns["DNA step"] == 4
    assert row.metadata["prev_price"] == price
    assert row.metadata["prev_actual"] == pytest.approx(rec.actual_cumulative)


# --------------------------------------------------------------------------- #
# Decision object — one source for Steps 8–13
# --------------------------------------------------------------------------- #
def test_decision_pass_dna_zero():
    params = make_params(diff=0.0)
    decision = build_decision(holdings=1.0, price=100.0, dna_signal=0, params=params)
    assert decision.status == engine.STATUS_PASS_DNA_ZERO
    assert decision.action == "PASS"
    assert decision.side is None
    assert decision.quantity == 0.0


def test_decision_pass_threshold():
    params = make_params(diff=50.0)
    # value_now 1490, gap 10 within diff 50
    decision = build_decision(holdings=14.9, price=100.0, dna_signal=1, params=params)
    assert decision.status == engine.STATUS_PASS_THRESHOLD
    assert decision.action == "PASS"


def test_decision_ready_sell():
    params = make_params(diff=0.0)
    # value_now 2000 > fix_c 1500, gap -500
    decision = build_decision(holdings=20.0, price=100.0, dna_signal=1, params=params)
    assert decision.status == engine.STATUS_READY_SELL
    assert decision.side == "SELL"
    assert decision.quantity == round(500.0 / 100.0, 5)


def test_decision_quantity_uses_decimal_precision():
    params = make_params(diff=0.0, decimal_precision=3)
    decision = build_decision(holdings=0.0, price=7.0, dna_signal=1, params=params)
    # gap 1500, qty = 1500/7 rounded to 3 dp
    assert decision.quantity == round(1500.0 / 7.0, 3)


def test_pipeline_dna_zero_when_anchor_lands_on_zero_bit():
    # Craft a valid encoded DNA and locate a zero bit past index 0.
    from manual_tools import encode_dna

    encoded = encode_dna(50, 0, [7, 3, 5])  # length 50, rate 0, seeds 7,3,5
    params = make_params(dna_code=encoded)
    dna = params.decoded_dna()
    zero_indices = [i for i in range(1, len(dna)) if dna[i] == 0]
    assert zero_indices, "expected at least one zero bit in decoded DNA"
    target = zero_indices[0]
    anchor = PreviousAnchor(
        exists=True,
        version=target,
        row_id=f"row-{target}",
        dna_step=target - 1,
        p0=100.0,
        prev_price=100.0,
        prev_actual=0.0,
    )
    ctx = make_ctx(anchor=anchor, params=params)
    row = compute_row(ctx)
    assert row.columns["DNA step"] == target
    assert row.columns["DNA signal"] == 0
    assert row.columns["สถานะ"] == engine.STATUS_PASS_DNA_ZERO


# --------------------------------------------------------------------------- #
# DNA exhaustion — fail closed
# --------------------------------------------------------------------------- #
def test_dna_exhausted_fails_closed():
    params = make_params(dna_code="bypass:2")
    anchor = PreviousAnchor(
        exists=True, version=5, row_id="r", dna_step=5, p0=100.0, prev_price=100.0, prev_actual=0.0
    )
    ctx = make_ctx(anchor=anchor, params=params)
    with pytest.raises(PipelineError):
        compute_row(ctx)


# --------------------------------------------------------------------------- #
# Rounding — full precision stored, 2 dp presented
# --------------------------------------------------------------------------- #
def test_full_precision_stored_and_two_dp_presented():
    anchor = PreviousAnchor(
        exists=True, version=1, row_id="r", dna_step=0, p0=100.0, prev_price=100.0, prev_actual=0.0
    )
    ctx = make_ctx(snapshot=make_snapshot(price=101.0, holdings=1.0), anchor=anchor)
    row = compute_row(ctx)
    reference = row.columns["Rₙ อ้างอิง (USD)"]
    # Stored value keeps full precision (not equal to its own 2 dp rounding).
    assert reference != round(reference, 2)
    presented = present_row(row.columns)
    assert presented["Rₙ อ้างอิง (USD)"] == round(reference, 2)


# --------------------------------------------------------------------------- #
# Chain identity + run identity + Manual/All-in determinism
# --------------------------------------------------------------------------- #
def test_chain_key_changes_with_config():
    snap = make_snapshot()
    key_a = compute_chain_key(snap.environment, snap.account_fingerprint, snap.symbol, make_params(fix_c=1500))
    key_b = compute_chain_key(snap.environment, snap.account_fingerprint, snap.symbol, make_params(fix_c=1600))
    key_c = compute_chain_key(snap.environment, snap.account_fingerprint, snap.symbol, make_params(diff=5))
    assert key_a != key_b
    assert key_a != key_c


def test_run_id_deterministic_for_same_snapshot():
    ctx1 = make_ctx()
    ctx2 = make_ctx()
    assert ctx1.run_id == ctx2.run_id


def test_run_id_changes_with_new_snapshot():
    ctx1 = make_ctx(snapshot=make_snapshot(captured_at="2026-07-16T00:00:00+00:00"))
    ctx2 = make_ctx(snapshot=make_snapshot(captured_at="2026-07-16T00:05:00+00:00"))
    assert ctx1.run_id != ctx2.run_id


def test_compute_row_is_deterministic():
    ctx = make_ctx()
    a = compute_row(ctx)
    b = compute_row(ctx)
    assert a.columns == b.columns
    assert a.metadata == b.metadata


# --------------------------------------------------------------------------- #
# Stage decomposition + final document contract
# --------------------------------------------------------------------------- #
def test_stage_results_cover_17_columns_in_order():
    ctx = make_ctx()
    row = compute_row(ctx)
    assert len(row.stages) == 17
    assert tuple(s.column_name for s in row.stages) == engine.FINAL_COLUMNS
    # Status shows the interim draft value before the decision stage.
    status_stage = row.stages[2]
    assert status_stage.column_name == "สถานะ"
    assert status_stage.value == engine.STATUS_SNAPSHOT_READY


def test_final_document_has_17_columns_and_metadata():
    ctx = make_ctx()
    row = compute_row(ctx)
    document = build_final_document(row, ctx)
    assert tuple(document["columns_full_precision"].keys()) == engine.FINAL_COLUMNS
    assert document["run_id"] == ctx.run_id
    assert document["version"] == ctx.anchor.version + 1
    assert document["metadata"]["p0"] == 100.0
    # Provenance must not carry the account id or raw responses.
    assert "account_id" not in document["metadata"]


def test_validate_row_rejects_wrong_columns():
    with pytest.raises(PipelineError):
        engine.validate_row_columns({"only": 1})
