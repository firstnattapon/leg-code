"""Goal: derive the reason for the new-row decision.

Quick Start:
    python step_10_reason.py --raw snapshot.csv --previous step_09.csv
"""

from __future__ import annotations

import argparse
import json
import pandas as pd

GOAL = "derive reason จาก DNA signal และ calculated action"
COLUMN = "เหตุผล"


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    signal = int(previous["DNA signal"].iloc[0])
    action = str(previous["คำสั่ง"].iloc[0]).upper()
    if signal == 0:
        reason = "DNA_ZERO"
    elif action == "PASS":
        reason = "WITHIN_THRESHOLD"
    elif action == "BUY":
        reason = "BELOW_TARGET"
    elif action == "SELL":
        reason = "ABOVE_TARGET"
    else:
        raise ValueError("unknown calculated action")
    values = pd.Series([reason], index=raw.index, dtype="string")
    return values, ("reason derived from one decision",), {"source": "DNA signal + action"}


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--output", default="step_10.csv")
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    values, diagnostics, provenance = transform(raw, previous, 1.0)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
