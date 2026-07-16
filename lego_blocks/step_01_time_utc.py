"""Goal: create one UTC timestamp from the immutable Step-0 snapshot.

Quick Start:
    python step_01_time_utc.py --raw snapshot.csv --output step_01.csv
"""

from __future__ import annotations

import argparse
import json
import pandas as pd

GOAL = "ใช้ snapshot_at ปัจจุบันสร้างเวลา UTC หนึ่งค่าต่อหนึ่ง run"
COLUMN = "เวลา (UTC)"


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    parsed = pd.to_datetime(raw["snapshot_at"], errors="coerce", utc=True)
    if len(parsed) != 1 or parsed.isna().any():
        raise ValueError("snapshot_at must contain one valid timestamp")
    values = parsed.map(
        lambda value: value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    ).astype("string")
    return values, ("immutable Step-0 timestamp",), {"source": "snapshot_at"}


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--output", default="step_01.csv")
    args = parser.parse_args()
    raw = pd.read_csv(args.raw)
    values, diagnostics, provenance = transform(raw, pd.DataFrame(index=raw.index), 1.0)
    pd.DataFrame({COLUMN: values}).to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
