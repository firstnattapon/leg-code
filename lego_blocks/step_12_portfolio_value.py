"""Goal: calculate current portfolio value for the one new row.

Quick Start:
    python step_12_portfolio_value.py --raw snapshot.csv --previous step_11.csv
"""

from __future__ import annotations

import argparse
import json
import pandas as pd

GOAL = "คำนวณมูลค่าพอร์ต = holdings × current snapshot price"
COLUMN = "มูลค่าพอร์ต (USD)"


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    holdings = float(previous["จำนวนถือครอง (หุ้น)"].iloc[0])
    price = float(previous["ราคา Pₙ (USD)"].iloc[0])
    values = pd.Series([holdings * price], index=raw.index, dtype=float)
    return values, ("full precision calculation",), {"formula": "holdings*price"}


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--output", default="step_12.csv")
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    values, diagnostics, provenance = transform(raw, previous, 1.0)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
