"""Tests for the standalone one-new-row runner + parity with the engine."""

from __future__ import annotations

import numpy as np
import pytest

import webull_lego_single_file as sf
from lego_one_row import (
    CurrentSnapshot,
    PreviousAnchor,
    RunContext,
    StrategyParameters,
    compute_chain_key,
    compute_row,
    compute_run_id,
)


# --------------------------------------------------------------------------- #
# DNA + extraction helpers
# --------------------------------------------------------------------------- #
def test_bypass_dna_is_all_ones():
    dna, summary = sf.decode_dna("bypass:5")
    assert list(dna) == [1, 1, 1, 1, 1]
    assert summary["mode"] == "bypass"


def test_extract_holdings_and_price():
    positions = {"positions": [{"symbol": "AAPL", "quantity": "7"}]}
    quote = {"symbol": "AAPL", "last_price": "42.5"}
    assert sf.extract_holdings(positions, "aapl") == 7.0
    assert sf.extract_price(quote, "aapl") == 42.5


def test_missing_position_is_zero():
    assert sf.extract_holdings({"positions": []}, "AAPL") == 0.0


# --------------------------------------------------------------------------- #
# One-row computation
# --------------------------------------------------------------------------- #
def test_genesis_row_baseline_zero():
    row = sf.compute_one_row(
        symbol="AAPL", price=100.0, holdings=3.0, captured_at="t0", anchor=None, fix_c=1500.0
    )
    cols = row["columns"]
    assert cols["DNA step"] == 0
    assert cols["สถานะ"] == "READY_BUY"
    assert cols["Rₙ อ้างอิง (USD)"] == 0.0
    assert cols["Aₙ สะสม (USD)"] == 0.0
    assert row["metadata"]["p0"] == 100.0


def test_dna_zero_forces_pass(monkeypatch):
    # Force the decoded DNA to place a 0 at step 0 so PASS_DNA_ZERO triggers.
    monkeypatch.setattr(sf, "decode_dna", lambda code: (np.array([0, 1], dtype=np.int8), {}))
    row = sf.compute_one_row(
        symbol="AAPL", price=100.0, holdings=3.0, captured_at="t0", anchor=None, fix_c=1500.0
    )
    assert row["columns"]["สถานะ"] == "PASS_DNA_ZERO"
    assert row["columns"]["คำสั่ง"] == "PASS"


def test_dna_exhausted_raises():
    anchor = {"dna_step": 5, "p0": 100.0, "prev_price": 100.0, "prev_actual": 0.0}
    with pytest.raises(ValueError):
        sf.compute_one_row(
            symbol="AAPL", price=100.0, holdings=1.0, captured_at="t", anchor=anchor,
            fix_c=1500.0, dna_code="bypass:2",
        )


def test_present_row_rounds_financials_only():
    row = sf.compute_one_row(
        symbol="AAPL", price=101.0, holdings=1.0, captured_at="t",
        anchor={"dna_step": 0, "p0": 100.0, "prev_price": 100.0, "prev_actual": 0.0},
        fix_c=1500.0,
    )
    presented = sf.present_row(row["columns"])
    assert presented["Rₙ อ้างอิง (USD)"] == round(row["columns"]["Rₙ อ้างอิง (USD)"], 2)
    assert presented["DNA step"] == 1  # non-financial untouched (anchor step 0 -> 1)


# --------------------------------------------------------------------------- #
# Parity: single file == engine for the same inputs
# --------------------------------------------------------------------------- #
def _engine_columns(price, holdings, anchor, fix_c, diff, dna_code, precision, captured_at):
    params = StrategyParameters(fix_c=fix_c, diff=diff, dna_code=dna_code, decimal_precision=precision)
    snap = CurrentSnapshot(
        environment="Test (UAT)", account_fingerprint="fp", symbol="AAPL",
        price=price, holdings=holdings, captured_at=captured_at,
    )
    chain_key = compute_chain_key(snap.environment, snap.account_fingerprint, snap.symbol, params)
    run_id = compute_run_id(chain_key, anchor, snap)
    ctx = RunContext(run_id=run_id, chain_key=chain_key, snapshot=snap, anchor=anchor, params=params)
    return compute_row(ctx).columns


def _assert_columns_equal(a, b):
    assert set(a) == set(b)
    for key in a:
        va, vb = a[key], b[key]
        if isinstance(va, float) or isinstance(vb, float):
            assert va == pytest.approx(vb), key
        else:
            assert va == vb, key


@pytest.mark.parametrize(
    "price,holdings,fix_c,diff",
    [
        (100.0, 3.0, 1500.0, 0.0),   # BUY
        (100.0, 20.0, 1500.0, 0.0),  # SELL
        (100.0, 15.0, 1500.0, 10.0), # PASS_THRESHOLD
    ],
)
def test_single_file_matches_engine_genesis(price, holdings, fix_c, diff):
    engine_cols = _engine_columns(price, holdings, PreviousAnchor.genesis(), fix_c, diff, "bypass:100", 5, "t0")
    sf_cols = sf.compute_one_row(
        symbol="AAPL", price=price, holdings=holdings, captured_at="t0", anchor=None,
        fix_c=fix_c, diff=diff, dna_code="bypass:100", decimal_precision=5,
    )["columns"]
    _assert_columns_equal(engine_cols, sf_cols)


def test_single_file_matches_engine_anchored():
    engine_anchor = PreviousAnchor(
        exists=True, version=3, row_id="r", dna_step=3, p0=100.0, prev_price=110.0, prev_actual=40.0
    )
    sf_anchor = {"dna_step": 3, "p0": 100.0, "prev_price": 110.0, "prev_actual": 40.0}
    engine_cols = _engine_columns(121.0, 5.0, engine_anchor, 1500.0, 0.0, "bypass:100", 5, "t1")
    sf_cols = sf.compute_one_row(
        symbol="AAPL", price=121.0, holdings=5.0, captured_at="t1", anchor=sf_anchor,
        fix_c=1500.0, diff=0.0, dna_code="bypass:100", decimal_precision=5,
    )["columns"]
    _assert_columns_equal(engine_cols, sf_cols)
