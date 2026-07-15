"""Market-price display and broker-confirmed cash-flow for the dashboard.

``last_price`` is a decision-time quote used only by the Learning Guide's
what-if chart.  Realized cash requires a terminal fill, reconciled Positions,
positive filled quantity, and a separately logged execution price.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

TRADE_PRICE_COLUMNS = (
    "last_price",
    "price",
    "market_state_last_price",
    "decision_last_price",
    "fill_price",
    "filled_price",
    "avg_price",
    "executed_price",
)

# Execution prices deliberately exclude ``last_price``/``price``.  Those are
# decision-time market quotes and cannot prove how much cash the broker
# actually exchanged.  The bot may add any of these names at the top level or
# under a flattened order/order-status payload.
EXECUTION_PRICE_COLUMNS = (
    "average_filled_price",
    "average_fill_price",
    "avg_filled_price",
    "avg_fill_price",
    "filled_price",
    "fill_price",
    "executed_price",
    "execution_price",
)

FILLED_QUANTITY_COLUMNS = (
    "filled_quantity",
    "cumulative_filled_quantity",
    "filled_qty",
)

FEE_COLUMNS = (
    "transaction_fee",
    "filled_fee",
    "execution_fee",
    "commission",
)

REALIZED_TERMINAL_STATUSES = frozenset({
    "ORDER_FILLED",
    "ORDER_PARTIAL_FILLED_TERMINAL",
    # Compatibility for a directly-normalized broker document.  The position
    # reconciliation requirement below remains mandatory.
    "FILLED",
})


def _candidate_columns(columns, names: tuple[str, ...]) -> list[str]:
    """Exact names first, then flattened ``*_<name>`` variants."""
    candidates = [name for name in names if name in columns]
    for name in names:
        suffix = f"_{name}"
        for column in columns:
            if column.endswith(suffix) and column not in candidates:
                candidates.append(column)
    return candidates


def _coalesced_numeric(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    """First finite numeric value per row from a prioritized field list."""
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    for column in _candidate_columns(frame.columns, names):
        values = pd.to_numeric(frame[column], errors="coerce")
        result = result.where(result.notna(), values)
    return result


def _coalesced_text(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    result = pd.Series(None, index=frame.index, dtype=object)
    for column in _candidate_columns(frame.columns, names):
        values = frame[column]
        usable = values.notna() & values.astype(str).str.strip().ne("")
        result = result.where(result.notna() | ~usable, values)
    return result


def realized_cashflow_from_trades(
    trades: pd.DataFrame,
    fix_c: float,
    p0: float,
) -> pd.DataFrame:
    """Build a gross-cash ledger from broker-confirmed execution events.

    A row is eligible only when all facts required by ``webull/doc`` agree:
    terminal filled status, positive cumulative filled quantity, a real
    execution price, and ``position_reconciled is True``.  Decision-time
    quotes never substitute for a missing execution price.

    Webull reports cumulative fill quantity.  Repeated lifecycle snapshots for
    the same ``client_order_id`` are converted to incremental notional so a
    partial fill cannot be counted twice.  Fees are subtracted when the log
    exposes a cumulative fee; otherwise the result is explicitly gross cash.
    Non-execution rows carry the last cumulative values without moving them.
    """
    if not math.isfinite(float(fix_c)) or not math.isfinite(float(p0)):
        raise ValueError("fix_c and p0 must be finite")
    if fix_c <= 0 or p0 <= 0:
        raise ValueError("fix_c and p0 must be greater than 0")

    ordered = trades.copy()
    if "created_at" in ordered.columns:
        ordered = ordered.sort_values("created_at")
    ordered = ordered.reset_index(drop=True)

    statuses = (
        _coalesced_text(ordered, ("status",))
        .fillna("")
        .astype(str)
        .str.upper()
    )
    sides = (
        _coalesced_text(ordered, ("side", "decision_side"))
        .fillna("")
        .astype(str)
        .str.upper()
    )
    order_ids = _coalesced_text(ordered, ("client_order_id", "order_id"))
    filled = _coalesced_numeric(ordered, FILLED_QUANTITY_COLUMNS)
    execution_price = _coalesced_numeric(ordered, EXECUTION_PRICE_COLUMNS)
    fees = _coalesced_numeric(ordered, FEE_COLUMNS).fillna(0.0)

    if "position_reconciled" in ordered.columns:
        reconciled = ordered["position_reconciled"].map(
            lambda value: isinstance(value, (bool, np.bool_)) and bool(value)
        )
    else:
        reconciled = pd.Series(False, index=ordered.index, dtype=bool)

    candidate_fill = (
        statuses.isin(REALIZED_TERMINAL_STATUSES)
        & sides.isin(("BUY", "SELL"))
        & filled.gt(0)
        & reconciled
    )
    eligible_input = candidate_fill & execution_price.gt(0)

    output = pd.DataFrame(index=ordered.index)
    output["candidate_fill"] = candidate_fill
    output["missing_execution_price"] = candidate_fill & ~execution_price.gt(0)
    output["eligible"] = False
    output["execution_price"] = np.nan
    output["filled_quantity"] = np.nan
    output["delta_actual"] = np.nan
    output["actual_cumulative"] = np.nan
    output["ln_reference"] = np.nan
    output["excess"] = np.nan

    actual = 0.0
    has_execution = False
    counted: dict[str, tuple[float, float, float]] = {}
    last_reference = math.nan

    for position in ordered.index:
        if eligible_input.iloc[position]:
            key_value = order_ids.iloc[position]
            key = str(key_value) if pd.notna(key_value) else f"row:{position}"
            cumulative_qty = float(filled.iloc[position])
            price = float(execution_price.iloc[position])
            cumulative_notional = cumulative_qty * price
            cumulative_fee = max(0.0, float(fees.iloc[position]))
            previous_qty, previous_notional, previous_fee = counted.get(
                key, (0.0, 0.0, 0.0)
            )

            # Ignore duplicate/stale cumulative snapshots.  A higher filled
            # quantity contributes only the newly observed cumulative notional.
            if cumulative_qty > previous_qty + 1e-12:
                incremental_notional = cumulative_notional - previous_notional
                incremental_fee = max(0.0, cumulative_fee - previous_fee)
                if incremental_notional > 0:
                    sign = 1.0 if sides.iloc[position] == "SELL" else -1.0
                    delta = sign * incremental_notional - incremental_fee
                    actual += delta
                    has_execution = True
                    counted[key] = (
                        cumulative_qty,
                        cumulative_notional,
                        cumulative_fee,
                    )
                    last_reference = fix_c * math.log(price / p0)
                    output.loc[position, "eligible"] = True
                    output.loc[position, "execution_price"] = price
                    output.loc[position, "filled_quantity"] = cumulative_qty - previous_qty
                    output.loc[position, "delta_actual"] = delta

        if has_execution:
            if pd.isna(output.loc[position, "delta_actual"]):
                output.loc[position, "delta_actual"] = 0.0
            output.loc[position, "actual_cumulative"] = actual
            output.loc[position, "ln_reference"] = last_reference
            output.loc[position, "excess"] = actual - last_reference

    return output


def trade_price_column_candidates(columns) -> list[str]:
    """Price-column candidates in priority order: exact names, then columns
    produced by flattening a nested payload (suffix ``_<name>``)."""
    candidates = [column for column in TRADE_PRICE_COLUMNS if column in columns]
    for name in TRADE_PRICE_COLUMNS:
        suffix = f"_{name}"
        for column in columns:
            if column.endswith(suffix) and column not in candidates:
                candidates.append(column)
    return candidates


def find_trade_price_column(trades: pd.DataFrame) -> str | None:
    """First candidate column holding at least one usable (positive) price."""
    for column in trade_price_column_candidates(trades.columns):
        if trade_price_series(trades, column):
            return column
    return None


def trade_price_series(trades: pd.DataFrame, price_column: str) -> list[float]:
    """Chronological positive prices from the (newest-first) trade log."""
    ordered = trades
    if "created_at" in trades.columns:
        ordered = trades.sort_values("created_at")
    prices = pd.to_numeric(ordered[price_column], errors="coerce")
    return [float(price) for price in prices if pd.notna(price) and price > 0]


# ---------------------------------------------------------------------------
# Grouped, human-readable trade-log table
# ---------------------------------------------------------------------------
#
# The raw log flattens to ~20 columns with three exact duplicates of every
# equation output (baseline_pnl / decision_baseline / decision_baseline_pnl,
# decision_rebalance / decision_rebalance_amount, decision_order_qty /
# decision_order_quantity). ``build_trade_log_display`` renames the useful
# fields to Thai labels and lays them out under three grouped headers:
#
#   ① Logged DNA          — what the bot actually recorded per tick
#   ② Execution reference   — Rₙ = Fix_c × ln(P_exec / P₀)
#   ③ Realized cash         — Aₙ (สะสม), ΔAₙ (ต่อ fill), Eₙ = Aₙ − Rₙ
#
# Groups ② and ③ never use a market quote as an execution-price substitute.

GROUP_LOGGED = "① Logged DNA (บันทึกจากบอท)"
GROUP_REFERENCE = "② Execution reference · Rₙ = Fix_c·ln(P_exec/P₀)"
GROUP_REBALANCED = "③ Realized execution cash · Aₙ, Eₙ"

# (source column, readable label). The price column is resolved at runtime
# because it may be last_price, market_state_last_price, or a suffix match.
_LOGGED_LABELS: tuple[tuple[str, str], ...] = (
    ("created_at", "เวลา (UTC)"),
    ("symbol", "สินทรัพย์"),
    ("status", "สถานะ"),
    ("dna_step", "DNA step"),
    ("dna_signal", "DNA signal"),
    ("decision_action", "คำสั่ง"),
    ("decision_side", "ฝั่ง"),
    ("decision_reason", "เหตุผล"),
    ("decision_order_qty", "จำนวนสั่ง (หุ้น)"),
    ("decision_value_now_usd", "มูลค่าพอร์ต (USD)"),
    ("decision_rebalance", "ส่วนต่างเป้าหมาย (USD)"),
)
_PRICE_LABEL = "ราคา Pₙ (USD)"
HOLDINGS_LABEL = "จำนวนถือครอง (หุ้น)"
POSITION_BEFORE_LABEL = "ก่อน order (หุ้น)"
EXPECTED_POSITION_LABEL = "คาดหลัง fill (หุ้น)"
POSITION_SYNC_LABEL = "สถานะ sync"
POSITION_OBSERVED_AT_LABEL = "ยืนยันล่าสุด (UTC)"
_REBALANCE_LABEL = "ส่วนต่างเป้าหมาย (USD)"
REFERENCE_LABEL = "Rₙ อ้างอิงจาก execution (USD)"
EXECUTION_PRICE_LABEL = "ราคา execute จริง (USD)"
FILLED_QUANTITY_LABEL = "จำนวน fill ที่นับ (หุ้น)"
DELTA_ACTUAL_LABEL = "ΔAₙ ต่อ fill (USD; หัก fee เมื่อมี)"
ACTUAL_CUMULATIVE_LABEL = "Aₙ สะสมจาก fill (USD; หัก fee เมื่อมี)"
EXCESS_LABEL = "Eₙ = Aₙ − Rₙ (USD)"
# Logged USD amounts carry float noise (e.g. 11.311520399999836); round the
# money-valued logged columns so the table reads cleanly.
_ROUNDED_LOGGED_LABELS = frozenset({"มูลค่าพอร์ต (USD)", _REBALANCE_LABEL})


def signed_rebalance_series(ordered: pd.DataFrame, fix_c: float) -> pd.Series:
    """ส่วนต่างเป้าหมาย with a portfolio-adjustment sign: sell = −, buy = +.

    The bot logs ``decision_rebalance`` as ``abs(fix_c − value_now)``
    (strategy.py), losing the direction. The sign is restored from
    ``decision_side`` (SELL → −, BUY → +); PASS rows fall back to comparing
    the logged portfolio value against ``fix_c``. Note this is the opposite
    convention to the cash-flow columns ΔAₙ/Aₙ, where + is cash received
    from selling — here + means "money to add" (buy) and − "to take out"
    (sell).
    """
    magnitude = pd.to_numeric(
        ordered.get("decision_rebalance"), errors="coerce"
    ).abs()
    side = ordered.get(
        "decision_side", pd.Series(None, index=ordered.index, dtype=object)
    )
    value_now = pd.to_numeric(
        ordered.get("decision_value_now_usd"), errors="coerce"
    )
    sign = np.where(
        side == "SELL",
        -1.0,
        np.where(
            side == "BUY",
            1.0,
            np.where(value_now.notna() & (value_now > fix_c), -1.0, 1.0),
        ),
    )
    return (magnitude * sign).round(2)


def _exact_numeric(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    """Coalesce exact columns only; never suffix-match expected quantities."""
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    for name in names:
        if name not in frame.columns:
            continue
        values = pd.to_numeric(frame[name], errors="coerce")
        result = result.where(result.notna(), values)
    return result


def observed_holdings_series(frame: pd.DataFrame) -> pd.Series:
    """Broker-observed holdings, excluding every expected/calculated value.

    New lifecycle rows publish ``position_after`` from Webull Positions.  PASS
    and legacy rows use the exact market-state/top-level quantity snapshots.
    The explicit column list prevents ``expected_position_after`` or an order
    quantity from being selected by a loose suffix match.
    """
    return _exact_numeric(
        frame,
        ("position_after", "market_state_quantity", "quantity"),
    )


def position_sync_series(frame: pd.DataFrame) -> pd.Series:
    explicit = (
        frame["position_sync_status"].fillna("").astype(str).str.strip().str.upper()
        if "position_sync_status" in frame.columns
        else pd.Series("", index=frame.index, dtype=object)
    )
    statuses = (
        frame["status"].fillna("").astype(str).str.strip().str.upper()
        if "status" in frame.columns
        else pd.Series("", index=frame.index, dtype=object)
    )
    reconciled = (
        frame["position_reconciled"].map(
            lambda value: isinstance(value, (bool, np.bool_)) and bool(value)
        )
        if "position_reconciled" in frame.columns
        else pd.Series(False, index=frame.index, dtype=bool)
    )
    filled = _exact_numeric(
        frame,
        ("filled_quantity", "cumulative_filled_quantity", "filled_qty"),
    ).fillna(0.0)
    legacy = statuses.isin({
        "ORDER_FILLED_POSITION_UNAVAILABLE",
        "ORDER_FILLED_POSITION_UNCONFIRMED",
        "ORDER_PARTIAL_POSITION_UNAVAILABLE",
        "ORDER_PARTIAL_POSITION_UNCONFIRMED",
    })

    result = explicit.copy()
    result = result.mask(result.eq("") & reconciled, "CONFIRMED")
    result = result.mask(result.eq("") & legacy, "LEGACY_UNVERIFIED")
    result = result.mask(result.eq("") & filled.gt(0), "PENDING")
    return result


def build_trade_log_display(
    trades: pd.DataFrame,
    price_column: str,
    fix_c: float,
    p0: float,
) -> pd.DataFrame:
    """Return a newest-first table with three grouped, renamed column blocks.

    Cumulative figures (group ③) are summed oldest-first from eligible broker
    fills and the frame is reversed for display.  Non-execution rows keep their
    logged fields and cannot move realized cumulative cash.
    """
    ordered = trades
    if "created_at" in trades.columns:
        ordered = trades.sort_values("created_at")
    ordered = ordered.reset_index(drop=True)

    realized = realized_cashflow_from_trades(ordered, fix_c, p0)

    columns: dict[tuple[str, str], Any] = {}

    def add_logged(source: str, label: str) -> None:
        if source not in ordered.columns:
            return
        if label == _REBALANCE_LABEL:
            # Restore the direction the bot's abs() dropped: sell = −, buy = +.
            series = signed_rebalance_series(ordered, fix_c)
        else:
            series = ordered[source]
            if label in _ROUNDED_LOGGED_LABELS:
                series = pd.to_numeric(series, errors="coerce").round(2)
        columns[(GROUP_LOGGED, label)] = series.to_numpy()

    for source, label in _LOGGED_LABELS:
        add_logged(source, label)
        if source == "dna_signal":  # slot the price right after the DNA fields
            add_logged(price_column, _PRICE_LABEL)
            holdings = observed_holdings_series(ordered)
            if holdings.notna().any():
                columns[(GROUP_LOGGED, HOLDINGS_LABEL)] = holdings.round(8).to_numpy()

            position_before = _exact_numeric(
                ordered,
                ("position_before", "pre_order_market_state_quantity"),
            )
            if position_before.notna().any():
                columns[(GROUP_LOGGED, POSITION_BEFORE_LABEL)] = (
                    position_before.round(8).to_numpy()
                )

            expected = _exact_numeric(ordered, ("expected_position_after",))
            if expected.notna().any():
                columns[(GROUP_LOGGED, EXPECTED_POSITION_LABEL)] = (
                    expected.round(8).to_numpy()
                )

            sync = position_sync_series(ordered)
            if sync.ne("").any():
                columns[(GROUP_LOGGED, POSITION_SYNC_LABEL)] = sync.to_numpy()

            if "position_observed_at" in ordered.columns:
                columns[(GROUP_LOGGED, POSITION_OBSERVED_AT_LABEL)] = (
                    ordered["position_observed_at"].to_numpy()
                )

    columns[(GROUP_REFERENCE, EXECUTION_PRICE_LABEL)] = (
        realized["execution_price"].round(5).to_numpy()
    )
    columns[(GROUP_REFERENCE, REFERENCE_LABEL)] = (
        realized["ln_reference"].round(2).to_numpy()
    )
    columns[(GROUP_REBALANCED, FILLED_QUANTITY_LABEL)] = (
        realized["filled_quantity"].round(8).to_numpy()
    )
    columns[(GROUP_REBALANCED, DELTA_ACTUAL_LABEL)] = (
        realized["delta_actual"].round(2).to_numpy()
    )
    columns[(GROUP_REBALANCED, ACTUAL_CUMULATIVE_LABEL)] = (
        realized["actual_cumulative"].round(2).to_numpy()
    )
    columns[(GROUP_REBALANCED, EXCESS_LABEL)] = realized["excess"].round(2).to_numpy()

    display = pd.DataFrame(columns)
    display.columns = pd.MultiIndex.from_tuples(display.columns)
    # Chronological order was oldest-first for the cumulative sum; show newest
    # first, matching the log convention.
    return display.iloc[::-1].reset_index(drop=True)
