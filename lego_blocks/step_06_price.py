"""Goal: create positive decision-time quote ``ราคา Pₙ (USD)`` values.

Quick Start:
    pip install pandas numpy
    python step_06_price.py --raw raw.csv --previous step_05.csv --output step_06.csv
"""

from __future__ import annotations

import argparse
import json

import numpy as np
import pandas as pd

GOAL = "เลือก quote บวกจาก field ที่รองรับ โดยไม่ใช้แทน execution price"
COLUMN = "ราคา Pₙ (USD)"
PRICE_FIELDS = (
    "last_price", "price", "market_state_last_price", "decision_last_price",
    "fill_price", "filled_price", "avg_price", "executed_price", COLUMN,
)


def _candidates(columns, names: tuple[str, ...]) -> list[str]:
    result = [name for name in names if name in columns]
    for name in names:
        for column in columns:
            if str(column).endswith(f"_{name}") and column not in result:
                result.append(column)
    return result


def _number(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    for name in _candidates(frame.columns, names):
        result = result.where(result.notna(), pd.to_numeric(frame[name], errors="coerce"))
    return result


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    numeric = _number(raw, PRICE_FIELDS)
    valid = numeric.notna() & np.isfinite(numeric) & numeric.gt(0)
    values = numeric.where(valid)
    return values, (f"ราคาบวกใช้ได้ {int(valid.sum())}/{len(raw)} แถว",), {
        "candidate_fields": list(PRICE_FIELDS)
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--output", default="step_06.csv")
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
