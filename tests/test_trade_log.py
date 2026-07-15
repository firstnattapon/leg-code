from __future__ import annotations

import pandas as pd

from manual_tools import rebalancing_cashflow_from_prices
from trade_log import (
    ACTUAL_CUMULATIVE_LABEL,
    DELTA_ACTUAL_LABEL,
    EXCESS_LABEL,
    GROUP_LOGGED,
    GROUP_REBALANCED,
    GROUP_REFERENCE,
    HOLDINGS_LABEL,
    EXPECTED_POSITION_LABEL,
    POSITION_BEFORE_LABEL,
    POSITION_OBSERVED_AT_LABEL,
    POSITION_SYNC_LABEL,
    REFERENCE_LABEL,
    TRADE_PRICE_COLUMNS,
    build_trade_log_display,
    find_trade_price_column,
    observed_holdings_series,
    position_sync_series,
    realized_cashflow_from_trades,
    trade_price_series,
)


def normalize(rows: list[dict]) -> pd.DataFrame:
    """Flatten Firestore documents exactly like the dashboard does."""
    return pd.json_normalize(rows, sep="_")


def test_nested_market_state_last_price_is_found():
    trades = normalize([
        {
            "created_at": 2,
            "status": "ORDER_SUBMITTED",
            "market_state": {"quantity": 1.0, "last_price": 101.5},
        },
        {
            "created_at": 1,
            "status": "PASS_THRESHOLD",
            "market_state": {"quantity": 1.0, "last_price": 100.0},
        },
    ])

    column = find_trade_price_column(trades)

    assert column == "market_state_last_price"
    assert trade_price_series(trades, column) == [100.0, 101.5]


def test_top_level_last_price_takes_priority_over_nested():
    trades = normalize([
        {"created_at": 1, "last_price": 100.0, "market_state": {"last_price": 999.0}},
    ])

    assert find_trade_price_column(trades) == "last_price"


def test_unknown_nesting_prefix_is_found_by_suffix():
    trades = normalize([
        {"created_at": 1, "order_result": {"fill_price": 55.0}},
    ])

    assert find_trade_price_column(trades) == "order_result_fill_price"


def test_column_without_usable_prices_is_skipped():
    trades = normalize([
        {"created_at": 1, "price": None, "market_state": {"last_price": 100.0}},
    ])

    assert find_trade_price_column(trades) == "market_state_last_price"


def test_rows_without_market_state_do_not_break_extraction():
    trades = normalize([
        {"created_at": 2, "status": "ERROR", "error_message": "boom"},
        {"created_at": 1, "status": "ORDER_SUBMITTED", "market_state": {"last_price": 100.0}},
    ])

    column = find_trade_price_column(trades)

    assert column == "market_state_last_price"
    assert trade_price_series(trades, column) == [100.0]


def test_no_price_column_returns_none():
    trades = normalize([{"created_at": 1, "status": "ERROR"}])

    assert find_trade_price_column(trades) is None


def test_series_is_chronological_and_filters_invalid_values():
    trades = pd.DataFrame({
        "created_at": [3, 1, 2],
        "last_price": ["101.5", None, -5],
    })

    assert trade_price_series(trades, "last_price") == [101.5]


def test_known_columns_cover_the_bot_log_schema():
    assert "last_price" in TRADE_PRICE_COLUMNS
    assert "market_state_last_price" in TRADE_PRICE_COLUMNS


# ---------------------------------------------------------------------------
# build_trade_log_display
# ---------------------------------------------------------------------------

def sample_log() -> pd.DataFrame:
    """A PASS followed by one broker-confirmed BUY fill."""
    return normalize([
        {
            "created_at": "2026-07-10T19:00:00Z",
            "symbol": "AAPL",
            "strategy_id": "SHANNON_DEMON_DNA",
            "state_document": "SHANNON_DEMON_DNA_SMR",
            "status": "ORDER_FILLED",
            "client_order_id": "order-1",
            "side": "BUY",
            "filled_quantity": 0.5,
            "position_reconciled": True,
            "position_before": 9.5,
            "expected_position_after": 10.0,
            "position_after": 10.0,
            "position_sync_status": "CONFIRMED",
            "position_observed_at": "2026-07-10T19:00:10Z",
            "order_status": {"average_filled_price": 110.0},
            "dna_step": 1,
            "dna_signal": 1,
            "baseline_pnl": 9.9,
            "decision": {
                "action": "BUY", "side": "BUY", "reason": "BELOW_TARGET",
                "order_qty": 0.5, "order_quantity": 0.5,
                "rebalance": 55.0, "rebalance_amount": 55.0,
                "value_now_usd": 1100.123456, "baseline_pnl": 9.9, "baseline": 9.9,
            },
            "market_state": {"quantity": 10.0, "last_price": 110.0},
        },
        {
            "created_at": "2026-07-10T18:00:00Z",
            "symbol": "AAPL",
            "strategy_id": "SHANNON_DEMON_DNA",
            "state_document": "SHANNON_DEMON_DNA_SMR",
            "status": "PASS_THRESHOLD",
            "dna_step": 0,
            "dna_signal": 1,
            "baseline_pnl": 0.0,
            "decision": {
                "action": "PASS", "side": None, "reason": "WITHIN_THRESHOLD",
                "order_qty": 0.0, "order_quantity": 0.0,
                "rebalance": 0.0, "rebalance_amount": 0.0,
                "value_now_usd": 1000.0, "baseline_pnl": 0.0, "baseline": 0.0,
            },
            "market_state": {"quantity": 10.0, "last_price": 100.0},
        },
    ])


def test_display_has_three_grouped_headers_in_order():
    display = build_trade_log_display(sample_log(), "market_state_last_price", 1500.0, 100.0)

    groups = list(dict.fromkeys(level0 for level0, _ in display.columns))
    assert groups == [GROUP_LOGGED, GROUP_REFERENCE, GROUP_REBALANCED]


def test_display_drops_duplicate_and_constant_columns():
    display = build_trade_log_display(sample_log(), "market_state_last_price", 1500.0, 100.0)

    labels = [label for _group, label in display.columns]
    # exact duplicates of equation outputs and constant metadata are gone
    for gone in ("decision_baseline", "decision_baseline_pnl", "decision_order_quantity",
                 "decision_rebalance_amount", "strategy_id", "state_document"):
        assert gone not in labels
    # readable price / dna fields are present
    assert (GROUP_LOGGED, "ราคา Pₙ (USD)") in display.columns
    assert (GROUP_LOGGED, "DNA step") in display.columns


def test_display_separates_confirmed_holdings_from_diagnostic_quantities():
    display = build_trade_log_display(sample_log(), "market_state_last_price", 1500.0, 100.0)

    assert display[(GROUP_LOGGED, HOLDINGS_LABEL)].iloc[0] == 10.0
    assert display[(GROUP_LOGGED, POSITION_BEFORE_LABEL)].iloc[0] == 9.5
    assert display[(GROUP_LOGGED, EXPECTED_POSITION_LABEL)].iloc[0] == 10.0
    assert display[(GROUP_LOGGED, POSITION_SYNC_LABEL)].iloc[0] == "CONFIRMED"
    assert display[(GROUP_LOGGED, POSITION_OBSERVED_AT_LABEL)].iloc[0] == "2026-07-10T19:00:10Z"


def test_expected_position_is_never_selected_as_observed_holdings():
    trades = normalize([
        {
            "created_at": "2026-07-10T19:00:00Z",
            "status": "ORDER_FILLED_POSITION_PENDING",
            "filled_quantity": 1.0,
            "position_reconciled": False,
            "position_sync_status": "MISMATCH",
            "expected_position_after": 999.0,
            "market_state": {"quantity": 5.0, "last_price": 100.0},
        }
    ])

    holdings = observed_holdings_series(trades)
    display = build_trade_log_display(trades, "market_state_last_price", 1500.0, 100.0)

    assert holdings.tolist() == [5.0]
    assert display[(GROUP_LOGGED, HOLDINGS_LABEL)].tolist() == [5.0]
    assert display[(GROUP_LOGGED, EXPECTED_POSITION_LABEL)].tolist() == [999.0]
    assert display[(GROUP_LOGGED, POSITION_SYNC_LABEL)].tolist() == ["MISMATCH"]


def test_legacy_terminal_on_unavailable_rows_are_explicitly_unverified():
    trades = normalize([
        {
            "status": "ORDER_FILLED_POSITION_UNAVAILABLE",
            "filled_quantity": 1.0,
            "position_reconciled": False,
        }
    ])

    assert position_sync_series(trades).tolist() == ["LEGACY_UNVERIFIED"]


def test_display_is_newest_first_and_matches_cashflow():
    display = build_trade_log_display(sample_log(), "market_state_last_price", 1500.0, 100.0)

    prices_col = display[(GROUP_LOGGED, "ราคา Pₙ (USD)")].tolist()
    assert prices_col == [110.0, 100.0]  # newest first

    # The PASS row is not execution.  The BUY fill spends 0.5*110 = 55 USD.
    assert display[(GROUP_REBALANCED, DELTA_ACTUAL_LABEL)].iloc[0] == -55.0
    assert display[(GROUP_REBALANCED, ACTUAL_CUMULATIVE_LABEL)].iloc[0] == -55.0
    assert pd.isna(display[(GROUP_REBALANCED, ACTUAL_CUMULATIVE_LABEL)].iloc[1])


def test_excess_equals_actual_minus_reference():
    display = build_trade_log_display(sample_log(), "market_state_last_price", 1500.0, 100.0)

    ref = display[(GROUP_REFERENCE, REFERENCE_LABEL)]
    actual = display[(GROUP_REBALANCED, ACTUAL_CUMULATIVE_LABEL)]
    excess = display[(GROUP_REBALANCED, EXCESS_LABEL)]
    valid = actual.notna() & ref.notna() & excess.notna()
    assert ((actual[valid] - ref[valid]).round(2) == excess[valid].round(2)).all()


def test_logged_money_columns_are_rounded():
    display = build_trade_log_display(sample_log(), "market_state_last_price", 1500.0, 100.0)

    value_now = display[(GROUP_LOGGED, "มูลค่าพอร์ต (USD)")].tolist()
    assert value_now == [1100.12, 1000.0]  # 1100.123456 rounded to 2dp


def test_rows_without_price_keep_logged_fields_and_blank_equations():
    trades = normalize([
        {"created_at": "2026-07-10T19:00:00Z", "status": "ERROR",
         "dna_step": 1, "error_message": "boom"},
        {"created_at": "2026-07-10T18:00:00Z", "status": "PASS_THRESHOLD",
         "dna_step": 0, "market_state": {"last_price": 100.0}},
    ])

    display = build_trade_log_display(trades, "market_state_last_price", 1500.0, 100.0)

    status = display[(GROUP_LOGGED, "สถานะ")].tolist()
    assert status == ["ERROR", "PASS_THRESHOLD"]  # both rows kept, newest first
    ref = display[(GROUP_REFERENCE, REFERENCE_LABEL)]
    assert pd.isna(ref.iloc[0])  # ERROR row has no price -> blank reference
    assert pd.isna(ref.iloc[1])  # PASS has a quote, but no broker execution


def test_price_column_is_resolved_dynamically():
    trades = normalize([
        {"created_at": "2026-07-10T18:00:00Z", "status": "OK",
         "last_price": 100.0, "quantity": 10.0},
    ])

    display = build_trade_log_display(trades, "last_price", 1500.0, 100.0)

    assert display[(GROUP_LOGGED, "ราคา Pₙ (USD)")].tolist() == [100.0]


# ---------------------------------------------------------------------------
# ส่วนต่างเป้าหมาย sign convention: sell = −, buy = +
# ---------------------------------------------------------------------------

def rebalance_row(created_at, side, action, rebalance, value_now):
    return {
        "created_at": created_at,
        "status": "ORDER_SUBMITTED" if side else "PASS_THRESHOLD",
        "decision": {
            "action": action, "side": side, "reason": "x",
            "rebalance": rebalance, "value_now_usd": value_now,
        },
        "market_state": {"quantity": 1.0, "last_price": 100.0},
    }


def test_rebalance_is_signed_sell_negative_buy_positive():
    trades = normalize([
        rebalance_row("2026-07-10T19:00:00Z", "SELL", "SELL", 200.0, 1700.0),
        rebalance_row("2026-07-10T18:00:00Z", "BUY", "BUY", 80.0, 1420.0),
    ])

    display = build_trade_log_display(trades, "market_state_last_price", 1500.0, 100.0)

    signed = display[(GROUP_LOGGED, "ส่วนต่างเป้าหมาย (USD)")].tolist()
    assert signed == [-200.0, 80.0]  # newest first: SELL → −, BUY → +


def test_pass_rows_take_sign_from_value_versus_fix_c():
    trades = normalize([
        rebalance_row("2026-07-10T19:00:00Z", None, "PASS", 11.31, 1511.31),
        rebalance_row("2026-07-10T18:00:00Z", None, "PASS", 6.5, 1493.5),
    ])

    display = build_trade_log_display(trades, "market_state_last_price", 1500.0, 100.0)

    signed = display[(GROUP_LOGGED, "ส่วนต่างเป้าหมาย (USD)")].tolist()
    # above target → would sell → −; below target → would buy → +
    assert signed == [-11.31, 6.5]


# ---------------------------------------------------------------------------
# Anchor P₀ = first price in the window → Aₙ/Rₙ/Eₙ start at 0
# ---------------------------------------------------------------------------

def test_rows_before_first_confirmed_fill_have_blank_realized_values():
    display = build_trade_log_display(sample_log(), "market_state_last_price", 1500.0, 100.0)

    oldest = display.iloc[-1]  # display is newest first
    assert pd.isna(oldest[(GROUP_REFERENCE, REFERENCE_LABEL)])
    assert pd.isna(oldest[(GROUP_REBALANCED, DELTA_ACTUAL_LABEL)])
    assert pd.isna(oldest[(GROUP_REBALANCED, ACTUAL_CUMULATIVE_LABEL)])
    assert pd.isna(oldest[(GROUP_REBALANCED, EXCESS_LABEL)])


def test_real_log_window_shows_near_zero_harvest_not_synthetic_step():
    """Prices from the 2026-07-10 AAPL export: one-way drift, all PASS.

    Anchored at the first in-window price there is no synthetic 3218-USD
    first step and the harvested excess is ~0 (no closed round trip).
    """
    prices = [314.55, 314.8, 314.96, 315.335, 315.545, 316.02, 316.32, 316.635]
    rows = rebalancing_cashflow_from_prices(prices, 1500.0, prices[0])

    assert rows[1]["delta_actual"] == 0.0  # first tick anchors, no fake jump
    final = rows[-1]
    assert abs(final["actual_cumulative"]) < 15.0  # ≈ 9.9, not 3228
    assert 0.0 <= final["excess"] < 0.05  # harvest ≈ 0 on a one-way path


# ---------------------------------------------------------------------------
# Realized execution ledger: only terminal fills reconciled to Positions count
# ---------------------------------------------------------------------------

def execution_row(
    created_at: int,
    *,
    status: str,
    side: str = "BUY",
    filled_quantity: float = 0.0,
    execution_price: float | None = None,
    position_reconciled: bool = False,
    client_order_id: str | None = None,
    fee: float | None = None,
) -> dict:
    row = {
        "created_at": created_at,
        "status": status,
        "side": side,
        "filled_quantity": filled_quantity,
        "position_reconciled": position_reconciled,
        "client_order_id": client_order_id or f"order-{created_at}",
        "last_price": 999.0,
    }
    if execution_price is not None:
        row["order_status"] = {"average_filled_price": execution_price}
    if fee is not None:
        row["transaction_fee"] = fee
    return row


def test_realized_ledger_uses_fill_price_quantity_side_and_fee():
    trades = normalize([
        execution_row(1, status="ORDER_FILLED", side="BUY", filled_quantity=2,
                      execution_price=10, position_reconciled=True, fee=0.25),
        execution_row(2, status="ORDER_FILLED", side="SELL", filled_quantity=1,
                      execution_price=12, position_reconciled=True, fee=0.10),
    ])

    ledger = realized_cashflow_from_trades(trades, fix_c=100, p0=10)

    assert ledger["delta_actual"].tolist() == [-20.25, 11.90]
    assert ledger["actual_cumulative"].tolist() == [-20.25, -8.35]
    assert ledger["execution_price"].tolist() == [10.0, 12.0]
    assert ledger["eligible"].tolist() == [True, True]


def test_pass_pending_rejected_unfilled_and_position_pending_do_not_move_cash():
    trades = normalize([
        execution_row(1, status="ORDER_FILLED", side="SELL", filled_quantity=1,
                      execution_price=10, position_reconciled=True),
        execution_row(2, status="PASS_THRESHOLD", execution_price=50),
        execution_row(3, status="ORDER_SUBMITTED", filled_quantity=0, execution_price=51),
        execution_row(4, status="ORDER_REJECTED", filled_quantity=0, execution_price=52),
        execution_row(5, status="ORDER_NOT_FILLED", filled_quantity=0, execution_price=53),
        execution_row(6, status="ORDER_FILLED_POSITION_PENDING", filled_quantity=2,
                      execution_price=54, position_reconciled=False),
    ])

    ledger = realized_cashflow_from_trades(trades, fix_c=100, p0=10)

    assert ledger["delta_actual"].tolist() == [10.0, 0.0, 0.0, 0.0, 0.0, 0.0]
    assert ledger["actual_cumulative"].tolist() == [10.0] * 6
    assert ledger["eligible"].tolist() == [True, False, False, False, False, False]


def test_fill_without_execution_price_is_not_fabricated_from_last_quote():
    trades = normalize([
        execution_row(1, status="ORDER_FILLED", side="SELL", filled_quantity=1,
                      execution_price=None, position_reconciled=True),
    ])

    ledger = realized_cashflow_from_trades(trades, fix_c=100, p0=10)

    assert not ledger.loc[0, "eligible"]
    assert pd.isna(ledger.loc[0, "delta_actual"])
    assert pd.isna(ledger.loc[0, "actual_cumulative"])


def test_terminal_partial_fill_counts_only_reconciled_increment_once():
    trades = normalize([
        execution_row(1, status="ORDER_PARTIAL_FILLED_TERMINAL", side="SELL",
                      filled_quantity=0.5, execution_price=10,
                      position_reconciled=True, client_order_id="same"),
        execution_row(2, status="ORDER_PARTIAL_FILLED_TERMINAL", side="SELL",
                      filled_quantity=0.5, execution_price=10,
                      position_reconciled=True, client_order_id="same"),
        execution_row(3, status="ORDER_FILLED", side="SELL",
                      filled_quantity=1.0, execution_price=11,
                      position_reconciled=True, client_order_id="same"),
    ])

    ledger = realized_cashflow_from_trades(trades, fix_c=100, p0=10)

    # 0.5*10 first; duplicate adds zero; final cumulative notional 1*11 adds 6.
    assert ledger["delta_actual"].tolist() == [5.0, 0.0, 6.0]
    assert ledger["actual_cumulative"].tolist() == [5.0, 5.0, 11.0]


def test_display_cashflow_columns_follow_realized_ledger_not_quote_ticks():
    trades = normalize([
        execution_row(1, status="ORDER_FILLED", side="BUY", filled_quantity=2,
                      execution_price=10, position_reconciled=True),
        execution_row(2, status="PASS_THRESHOLD", execution_price=99),
        execution_row(3, status="ORDER_FILLED", side="SELL", filled_quantity=1,
                      execution_price=12, position_reconciled=True),
    ])

    display = build_trade_log_display(trades, "last_price", 100, 10)

    # Display is newest-first. PASS carries the prior cumulative value and has ΔA=0.
    assert display[(GROUP_REBALANCED, DELTA_ACTUAL_LABEL)].tolist() == [12.0, 0.0, -20.0]
    assert display[(GROUP_REBALANCED, ACTUAL_CUMULATIVE_LABEL)].tolist() == [-8.0, -20.0, -20.0]
    ref = display[(GROUP_REFERENCE, REFERENCE_LABEL)]
    actual = display[(GROUP_REBALANCED, ACTUAL_CUMULATIVE_LABEL)]
    excess = display[(GROUP_REBALANCED, EXCESS_LABEL)]
    assert (actual - ref).round(2).tolist() == excess.round(2).tolist()
