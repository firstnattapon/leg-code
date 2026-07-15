"""Goal: create broker-confirmed ``Eₙ ส่วนเกินสะสม (USD)`` values.

Quick Start:
    pip install pandas
    python step_17_excess.py --raw raw.csv --previous step_16.csv --output step_17.csv
"""

from __future__ import annotations

import argparse
import json

import pandas as pd

GOAL = "คำนวณ Eₙ = Aₙ − Rₙ เฉพาะเมื่อค่าทั้งสองพิสูจน์ได้"
COLUMN = "Eₙ ส่วนเกินสะสม (USD)"


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    actual = pd.to_numeric(previous["Aₙ สะสม (USD)"], errors="coerce")
    reference = pd.to_numeric(previous["Rₙ อ้างอิง (USD)"], errors="coerce")
    values = (actual - reference).round(2)
    return values, (f"Eₙ ที่พิสูจน์ได้ {int(values.notna().sum())}/{len(raw)} แถว",), {
        "formula": "A_n - R_n"
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--output", default="step_17.csv")
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
