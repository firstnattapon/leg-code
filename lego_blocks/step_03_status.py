"""Goal: initialize the one-row calculation status.

Quick Start:
    python step_03_status.py --raw snapshot.csv --previous step_02.csv
"""

from __future__ import annotations

import argparse
import json
import pandas as pd

GOAL = "เริ่ม draft status เป็น SNAPSHOT_READY แล้วให้ Step 18 finalize"
COLUMN = "สถานะ"


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    values = pd.Series(["SNAPSHOT_READY"], index=raw.index, dtype="string")
    return values, ("draft status",), {"finalized_at": "Step 18"}


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--output", default="step_03.csv")
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    values, diagnostics, provenance = transform(raw, previous, 1.0)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
