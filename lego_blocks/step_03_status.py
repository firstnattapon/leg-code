"""Goal: create ``สถานะ`` from the explicit lifecycle status only.

Quick Start:
    pip install pandas
    python step_03_status.py --raw raw.csv --previous step_02.csv --output step_03.csv
"""

from __future__ import annotations

import argparse
import json

import pandas as pd

GOAL = "normalize status โดยไม่สร้างสถานะสมมติ"
COLUMN = "สถานะ"


def _text(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    result = pd.Series(pd.NA, index=frame.index, dtype="object")
    for name in names:
        if name in frame:
            values = frame[name]
            usable = values.notna() & values.astype(str).str.strip().ne("")
            result = result.where(result.notna() | ~usable, values)
    return result.astype("string").str.strip().mask(lambda value: value.eq(""), pd.NA)


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    values = _text(raw, ("status", COLUMN)).str.upper()
    return values, (f"พบสถานะ {values.dropna().nunique()} แบบ",), {"source": "status"}


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--output", default="step_03.csv")
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
