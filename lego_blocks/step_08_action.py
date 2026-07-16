"""Goal: calculate BUY, SELL, or PASS for the new row.

Quick Start:
    python step_08_action.py --raw snapshot.csv --previous step_07.csv --fix-c 1500 --diff 30
"""

from __future__ import annotations

import argparse
import json
import math
import pandas as pd

GOAL = "คำนวณคำสั่งใหม่จาก DNA, holdings, price, FIX_C และ DIFF"
COLUMN = "คำสั่ง"


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    signal = int(previous["DNA signal"].iloc[0])
    price = float(previous["ราคา Pₙ (USD)"].iloc[0])
    holdings = float(previous["จำนวนถือครอง (หุ้น)"].iloc[0])
    diff = float(raw.attrs.get("diff", 0.0))
    if not all(math.isfinite(value) for value in (fix_c, diff, price, holdings)):
        raise ValueError("decision inputs must be finite")
    if fix_c <= 0 or diff < 0 or price <= 0 or holdings < 0:
        raise ValueError("decision inputs are outside the allowed range")
    gap = float(fix_c) - holdings * price
    if signal == 0 or abs(gap) <= diff:
        action = "PASS"
    else:
        action = "BUY" if gap > 0 else "SELL"
    values = pd.Series([action], index=raw.index, dtype="string")
    return values, (f"gap={gap:.10f}; diff={diff:.10f}",), {
        "formula": "FIX_C - holdings*price",
        "dna_zero_forces_pass": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--fix-c", type=float, default=1500.0)
    parser.add_argument("--diff", type=float, default=0.0)
    parser.add_argument("--output", default="step_08.csv")
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    raw.attrs["diff"] = args.diff
    values, diagnostics, provenance = transform(raw, previous, args.fix_c)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
