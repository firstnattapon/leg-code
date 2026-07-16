"""Goal: calculate the signed target gap for the one new row.

Quick Start:
    python step_13_target_gap.py --raw snapshot.csv --previous step_12.csv --fix-c 1500
"""

from __future__ import annotations

import argparse
import json
import pandas as pd

GOAL = "คำนวณ target gap = FIX_C − portfolio value"
COLUMN = "ส่วนต่างเป้าหมาย (USD)"


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    value = float(previous["มูลค่าพอร์ต (USD)"].iloc[0])
    values = pd.Series([float(fix_c) - value], index=raw.index, dtype=float)
    return values, ("positive=BUY; negative=SELL",), {"formula": "FIX_C-portfolio_value"}


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--fix-c", type=float, default=1500.0)
    parser.add_argument("--output", default="step_13.csv")
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    values, diagnostics, provenance = transform(raw, previous, args.fix_c)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
