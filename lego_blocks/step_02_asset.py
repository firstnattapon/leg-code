"""Goal: create one normalized symbol from the current snapshot.

Quick Start:
    python step_02_asset.py --raw snapshot.csv --previous step_01.csv
"""

from __future__ import annotations

import argparse
import json
import pandas as pd

GOAL = "normalize current snapshot symbol เป็นตัวพิมพ์ใหญ่"
COLUMN = "สินทรัพย์"


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    values = raw["symbol"].astype("string").str.strip().str.upper()
    if len(values) != 1 or values.isna().any() or values.eq("").any():
        raise ValueError("snapshot symbol is required")
    return values, ("one current symbol",), {"source": "snapshot symbol"}


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--output", default="step_02.csv")
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    values, diagnostics, provenance = transform(raw, previous, 1.0)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
