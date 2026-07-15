"""Goal: create broker-confirmed ``ΔAₙ ต่อสเต็ป (USD)`` values.

Quick Start:
    pip install pandas numpy
    python step_15_delta_actual.py --raw raw.csv --previous step_14.csv --fix-c 1500 --output step_15.csv

SELL is positive cash, BUY is negative cash, and cumulative fee is deducted.
Input rows must be chronological (oldest to newest), as prepared by the app.
"""

from __future__ import annotations

import argparse
import json
import math

import numpy as np
import pandas as pd

GOAL = "คำนวณ incremental filled notional และ fee โดย deduplicate partial fill ด้วย order ID"
COLUMN = "ΔAₙ ต่อสเต็ป (USD)"
EXECUTION_PRICES = (
    "average_filled_price", "average_fill_price", "avg_filled_price",
    "avg_fill_price", "filled_price", "fill_price", "executed_price", "execution_price",
)
FILLED_QUANTITIES = ("filled_quantity", "cumulative_filled_quantity", "filled_qty")
FEES = ("transaction_fee", "filled_fee", "execution_fee", "commission")
TERMINAL = frozenset({"ORDER_FILLED", "ORDER_PARTIAL_FILLED_TERMINAL", "FILLED"})


def _candidates(columns, names: tuple[str, ...]) -> list[str]:
    result = [name for name in names if name in columns]
    for name in names:
        for column in columns:
            if str(column).endswith(f"_{name}") and column not in result:
                result.append(column)
    return result


def _number(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    for name in _candidates(frame.columns, names):
        result = result.where(result.notna(), pd.to_numeric(frame[name], errors="coerce"))
    return result


def _text(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    result = pd.Series(None, index=frame.index, dtype=object)
    for name in _candidates(frame.columns, names):
        values = frame[name]
        usable = values.notna() & values.astype(str).str.strip().ne("")
        result = result.where(result.notna() | ~usable, values)
    return result


def _ledger(raw: pd.DataFrame, fix_c: float) -> tuple[pd.DataFrame, float | None]:
    if not math.isfinite(float(fix_c)) or float(fix_c) <= 0:
        raise ValueError("fix_c must be finite and greater than 0")
    status = _text(raw, ("status",)).fillna("").astype(str).str.upper()
    side = _text(raw, ("side", "decision_side")).fillna("").astype(str).str.upper()
    order_id = _text(raw, ("client_order_id", "order_id"))
    filled, price = _number(raw, FILLED_QUANTITIES), _number(raw, EXECUTION_PRICES)
    fees = _number(raw, FEES).fillna(0.0)
    reconciled = (
        raw["position_reconciled"].map(lambda value: isinstance(value, (bool, np.bool_)) and bool(value))
        if "position_reconciled" in raw
        else pd.Series(False, index=raw.index, dtype=bool)
    )
    eligible_input = status.isin(TERMINAL) & side.isin(("BUY", "SELL")) & filled.gt(0) & price.gt(0) & reconciled
    eligible_prices = price[eligible_input]
    output = pd.DataFrame(np.nan, index=raw.index, columns=["ln_reference", "delta_actual", "actual_cumulative"])
    if eligible_prices.empty:
        return output, None
    p0, actual, has_execution = float(eligible_prices.iloc[0]), 0.0, False
    counted: dict[str, tuple[float, float, float]] = {}
    last_reference = math.nan
    for row in raw.index:
        if eligible_input.loc[row]:
            key = str(order_id.loc[row]) if pd.notna(order_id.loc[row]) else f"row:{row}"
            quantity, execution, fee = float(filled.loc[row]), float(price.loc[row]), max(0.0, float(fees.loc[row]))
            old_quantity, old_notional, old_fee = counted.get(key, (0.0, 0.0, 0.0))
            notional = quantity * execution
            if quantity > old_quantity + 1e-12 and notional - old_notional > 0:
                delta = (1.0 if side.loc[row] == "SELL" else -1.0) * (notional - old_notional) - max(0.0, fee - old_fee)
                actual += delta
                has_execution = True
                counted[key] = (quantity, notional, fee)
                last_reference = float(fix_c) * math.log(execution / p0)
                output.loc[row, "delta_actual"] = delta
        if has_execution:
            output.loc[row, "delta_actual"] = 0.0 if pd.isna(output.loc[row, "delta_actual"]) else output.loc[row, "delta_actual"]
            output.loc[row, "actual_cumulative"] = actual
            output.loc[row, "ln_reference"] = last_reference
    return output, p0


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    ledger, p0 = _ledger(raw, fix_c)
    values = ledger["delta_actual"].round(2)
    counted = int(values.fillna(0).ne(0).sum())
    return values, (f"execution increments ที่ขยับเงินจริง {counted} แถว",), {
        "filled_quantity_fields": list(FILLED_QUANTITIES),
        "execution_price_fields": list(EXECUTION_PRICES),
        "fee_fields": list(FEES),
        "execution_anchor_p0": p0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--fix-c", type=float, default=1500.0)
    parser.add_argument("--output", default="step_15.csv")
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    if len(raw) != len(previous):
        raise ValueError("raw and previous must have the same row count")
    values, diagnostics, provenance = transform(raw, previous, args.fix_c)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
