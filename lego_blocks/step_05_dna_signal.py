"""Goal: create nullable binary ``DNA signal`` values.

Quick Start:
    pip install pandas numpy
    python step_05_dna_signal.py --raw raw.csv --previous step_04.csv --output step_05.csv
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

GOAL = "รับ DNA signal เฉพาะ 0 หรือ 1"
COLUMN = "DNA signal"


def _number(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    for name in names:
        if name in frame:
            result = result.where(result.notna(), pd.to_numeric(frame[name], errors="coerce"))
    return result


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    numeric = _number(raw, ("dna_signal", COLUMN))
    valid = numeric.isin((0, 1))
    values = numeric.where(valid).astype("Int8")
    rejected = int(numeric.notna().sum() - valid.sum())
    return values, (f"DNA signal ใช้ได้ {int(valid.sum())}/{len(raw)} แถว", f"ปฏิเสธ {rejected} ค่า"), {
        "rule": "0 or 1"
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--output", default="step_05.csv")
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
