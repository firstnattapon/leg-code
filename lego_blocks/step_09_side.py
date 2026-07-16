"""Goal: derive one BUY/SELL side from the calculated action.

Quick Start:
    python step_09_side.py --raw snapshot.csv --previous step_08.csv
"""

from __future__ import annotations

import argparse
import json
import pandas as pd

GOAL = "derive side จากคำสั่งใหม่; PASS ไม่มี side"
COLUMN = "ฝั่ง"


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    action = str(previous["คำสั่ง"].iloc[0]).upper()
    side = action if action in {"BUY", "SELL"} else pd.NA
    values = pd.Series([side], index=raw.index, dtype="string")
    return values, ("side derived from action",), {"allowed": ["BUY", "SELL", None]}


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--output", default="step_09.csv")
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    values, diagnostics, provenance = transform(raw, previous, 1.0)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
