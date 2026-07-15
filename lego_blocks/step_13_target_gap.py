"""Goal: create signed ``ส่วนต่างเป้าหมาย (USD)`` values.

Quick Start:
    pip install pandas
    python step_13_target_gap.py --raw raw.csv --previous step_12.csv --fix-c 1500 --output step_13.csv
"""

from __future__ import annotations

import argparse
import json
import math

import pandas as pd

GOAL = "คำนวณ FIX_C − มูลค่าพอร์ต; บวก=ควรซื้อ ลบ=ควรขาย"
COLUMN = "ส่วนต่างเป้าหมาย (USD)"


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    if not math.isfinite(float(fix_c)) or float(fix_c) <= 0:
        raise ValueError("fix_c must be finite and greater than 0")
    value = pd.to_numeric(previous["มูลค่าพอร์ต (USD)"], errors="coerce")
    values = (float(fix_c) - value).round(2)
    return values, (f"FIX_C = {float(fix_c):,.2f} USD", "บวก=BUY · ลบ=SELL"), {
        "formula": "FIX_C - portfolio_value"
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--fix-c", type=float, default=1500.0)
    parser.add_argument("--output", default="step_13.csv")
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
