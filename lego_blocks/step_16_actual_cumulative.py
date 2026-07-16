"""Goal: accumulate the new price-path delta onto the latest A_n anchor.

Quick Start:
    python step_16_actual_cumulative.py --raw snapshot.csv --previous step_15.csv
"""

from __future__ import annotations

import argparse
import json
import pandas as pd

GOAL = "คำนวณ Aₙ = Aₙ₋₁ + ΔAₙ; แถวแรกเริ่ม 0"
COLUMN = "Aₙ สะสม (USD)"


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    anchor = dict(raw.attrs.get("anchor", {}))
    prior = float(anchor.get("actual_cumulative", 0.0) or 0.0)
    delta = float(previous["ΔAₙ ต่อสเต็ป (USD)"].iloc[0])
    values = pd.Series([prior + delta], index=raw.index, dtype=float)
    return values, (f"previous A={prior:.10f}",), {"formula": "A_(n-1)+deltaA_n"}


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--anchor-actual", type=float, default=0.0)
    parser.add_argument("--output", default="step_16.csv")
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    raw.attrs["anchor"] = {"actual_cumulative": args.anchor_actual}
    values, diagnostics, provenance = transform(raw, previous, 1.0)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
