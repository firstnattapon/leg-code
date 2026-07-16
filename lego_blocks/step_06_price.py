"""Goal: use one positive quote from the immutable Step-0 snapshot.

Quick Start:
    python step_06_price.py --raw snapshot.csv --previous step_05.csv
"""

from __future__ import annotations

import argparse
import json
import numpy as np
import pandas as pd

GOAL = "ใช้ current quote จาก snapshot โดยไม่ refresh หรืออ่านราคาเก่า"
COLUMN = "ราคา Pₙ (USD)"


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    values = pd.to_numeric(raw["last_price"], errors="coerce")
    if len(values) != 1 or values.isna().any() or not np.isfinite(values.iloc[0]) or values.iloc[0] <= 0:
        raise ValueError("snapshot price must be finite and greater than 0")
    return values.astype(float), ("one immutable quote",), {"source": "Step-0 snapshot"}


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--output", default="step_06.csv")
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    values, diagnostics, provenance = transform(raw, previous, 1.0)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
