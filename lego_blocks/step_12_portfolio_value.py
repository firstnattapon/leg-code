"""Goal: create decision-time ``มูลค่าพอร์ต (USD)`` values.

Quick Start:
    pip install pandas numpy
    python step_12_portfolio_value.py --raw raw.csv --previous step_11.csv --output step_12.csv

Logged decision value wins; fallback is decision quantity times decision quote.
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

GOAL = "ใช้ logged portfolio value ก่อน แล้ว fallback จาก quantity × price พร้อม provenance"
COLUMN = "มูลค่าพอร์ต (USD)"


def _number(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    for name in names:
        if name in frame:
            result = result.where(result.notna(), pd.to_numeric(frame[name], errors="coerce"))
    return result


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    logged = _number(raw, ("decision_value_now_usd", "value_now_usd", COLUMN))
    before = _number(raw, ("position_before", "pre_order_market_state_quantity"))
    holdings = pd.to_numeric(previous["จำนวนถือครอง (หุ้น)"], errors="coerce")
    price = pd.to_numeric(previous["ราคา Pₙ (USD)"], errors="coerce")
    quantity = before.where(before.notna(), holdings)
    values = logged.where(logged.notna(), quantity * price)
    values = values.where(values.notna() & np.isfinite(values) & values.ge(0)).round(2)
    logged_count = int(logged.notna().sum())
    fallback_count = int((logged.isna() & values.notna()).sum())
    return values, (f"logged {logged_count} แถว", f"fallback quantity×price {fallback_count} แถว"), {
        "logged_rows": logged_count,
        "fallback_rows": fallback_count,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--output", default="step_12.csv")
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    if len(raw) != len(previous):
        raise ValueError("raw and previous must have the same row count")
    values, diagnostics, provenance = transform(raw, previous, 1500.0)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
