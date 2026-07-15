"""Goal: create the normalized ``สินทรัพย์`` column without guessing a symbol.

Quick Start:
    pip install pandas
    python step_02_asset.py --raw raw.csv --previous step_01.csv --output step_02.csv
"""

from __future__ import annotations

import argparse
import json

import pandas as pd

GOAL = "normalize symbol เป็นตัวพิมพ์ใหญ่ และเว้นว่างเมื่อไม่มีหลักฐาน"
COLUMN = "สินทรัพย์"


def _text(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    result = pd.Series(pd.NA, index=frame.index, dtype="object")
    for name in names:
        if name in frame:
            values = frame[name]
            usable = values.notna() & values.astype(str).str.strip().ne("")
            result = result.where(result.notna() | ~usable, values)
    return result.astype("string").str.strip().mask(lambda value: value.eq(""), pd.NA)


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    values = _text(raw, ("symbol", COLUMN)).str.upper()
    return values, (f"สินทรัพย์ไม่ว่าง {int(values.notna().sum())}/{len(raw)} แถว",), {
        "source": "symbol"
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--output", default="step_02.csv")
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
