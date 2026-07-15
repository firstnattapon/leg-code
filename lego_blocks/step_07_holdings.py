"""Goal: create broker-observed ``จำนวนถือครอง (หุ้น)`` values.

Quick Start:
    pip install pandas numpy
    python step_07_holdings.py --raw raw.csv --previous step_06.csv --output step_07.csv

Expected/calculated positions are intentionally excluded.
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

GOAL = "ใช้เฉพาะ holdings ที่ Webull Positions ยืนยันหรือบันทึกไว้จริง"
COLUMN = "จำนวนถือครอง (หุ้น)"
OBSERVED_FIELDS = ("position_after", "market_state_quantity", "quantity", COLUMN)


def _number(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    for name in names:
        if name in frame:
            result = result.where(result.notna(), pd.to_numeric(frame[name], errors="coerce"))
    return result


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    numeric = _number(raw, OBSERVED_FIELDS)
    valid = numeric.notna() & np.isfinite(numeric) & numeric.ge(0)
    values = numeric.where(valid)
    return values, (f"holdings ที่ยืนยัน/อ้างอิงได้ {int(valid.sum())}/{len(raw)} แถว",), {
        "priority": list(OBSERVED_FIELDS[:-1]),
        "excluded": ["expected_position_after"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--output", default="step_07.csv")
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
