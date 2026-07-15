"""Goal: create the nullable ``เวลา (UTC)`` column from logged timestamps.

Quick Start:
    pip install pandas
    python step_01_time_utc.py --raw raw.csv --output step_01.csv

Input: raw CSV from Webull/Firestore. Output: one-column accumulated CSV.
Invalid timestamps stay blank; the block never invents a time.
"""

from __future__ import annotations

import argparse
import json

import pandas as pd

GOAL = "แปลง created_at เป็น ISO-8601 UTC โดยไม่เดาค่าที่เสีย"
COLUMN = "เวลา (UTC)"


def _first(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    result = pd.Series(pd.NA, index=frame.index, dtype="object")
    for name in names:
        if name in frame:
            values = frame[name]
            usable = values.notna() & values.astype(str).str.strip().ne("")
            result = result.where(result.notna() | ~usable, values)
    return result


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    source = _first(raw, ("created_at", COLUMN))
    parsed = pd.to_datetime(source, errors="coerce", utc=True)
    values = pd.Series(pd.NA, index=raw.index, dtype="string")
    valid = parsed.notna()
    values.loc[valid] = parsed.loc[valid].map(
        lambda value: value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    return values, (f"เวลาใช้ได้ {int(valid.sum())}/{len(raw)} แถว",), {
        "source": "created_at → เวลา (UTC)"
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--output", default="step_01.csv")
    args = parser.parse_args()
    raw = pd.read_csv(args.raw)
    values, diagnostics, provenance = transform(raw, pd.DataFrame(index=raw.index), 1500.0)
    pd.DataFrame({COLUMN: values}).to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
