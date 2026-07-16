"""Goal: calculate one order quantity from the current target gap.

Quick Start:
    python step_11_order_quantity.py --raw snapshot.csv --previous step_10.csv --fix-c 1500
"""

from __future__ import annotations

import argparse
import json
import math
import pandas as pd

GOAL = "คำนวณ quantity = round(|FIX_C-holdings*price|/price, precision)"
COLUMN = "จำนวนสั่ง (หุ้น)"


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    action = str(previous["คำสั่ง"].iloc[0]).upper()
    price = float(previous["ราคา Pₙ (USD)"].iloc[0])
    holdings = float(previous["จำนวนถือครอง (หุ้น)"].iloc[0])
    precision = int(raw.attrs.get("decimal_precision", 5))
    if price <= 0 or holdings < 0 or fix_c <= 0 or precision < 0:
        raise ValueError("invalid order quantity inputs")
    gap = float(fix_c) - holdings * price
    quantity = 0.0 if action == "PASS" else round(abs(gap) / price, precision)
    if not math.isfinite(quantity) or quantity < 0:
        raise ValueError("calculated order quantity is invalid")
    if action in {"BUY", "SELL"} and quantity <= 0:
        raise ValueError("BUY/SELL quantity became zero after rounding")
    values = pd.Series([quantity], index=raw.index, dtype=float)
    return values, (f"precision={precision}",), {"formula": "abs(gap)/price"}


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--fix-c", type=float, default=1500.0)
    parser.add_argument("--precision", type=int, default=5)
    parser.add_argument("--output", default="step_11.csv")
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    raw.attrs["decimal_precision"] = args.precision
    values, diagnostics, provenance = transform(raw, previous, args.fix_c)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
