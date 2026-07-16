"""Goal: calculate E_n = A_n - R_n for the one new row.

Quick Start:
    python step_17_excess.py --raw snapshot.csv --previous step_16.csv
"""

from __future__ import annotations

import argparse
import json
import pandas as pd

GOAL = "คำนวณ Eₙ = Aₙ − Rₙ และรักษา identity ใน final row"
COLUMN = "Eₙ ส่วนเกินสะสม (USD)"


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    actual = float(previous["Aₙ สะสม (USD)"].iloc[0])
    reference = float(previous["Rₙ อ้างอิง (USD)"].iloc[0])
    values = pd.Series([actual - reference], index=raw.index, dtype=float)
    return values, ("E_n identity",), {"formula": "A_n-R_n"}


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--output", default="step_17.csv")
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    values, diagnostics, provenance = transform(raw, previous, 1.0)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
