"""Goal: calculate one price-path delta from the latest finalized price.

Quick Start:
    python step_15_delta_actual.py --raw snapshot.csv --previous step_14.csv --fix-c 1500
"""

from __future__ import annotations

import argparse
import json
import pandas as pd

GOAL = "คำนวณ ΔAₙ = FIX_C × (Pₙ/Pₙ₋₁ − 1); แถวแรกเป็น 0"
COLUMN = "ΔAₙ ต่อสเต็ป (USD)"


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    price = float(previous["ราคา Pₙ (USD)"].iloc[0])
    anchor = dict(raw.attrs.get("anchor", {}))
    previous_price = anchor.get("price")
    delta = 0.0 if previous_price is None else float(fix_c) * (price / float(previous_price) - 1.0)
    values = pd.Series([delta], index=raw.index, dtype=float)
    return values, ("latest-anchor recurrence",), {
        "previous_price": previous_price,
        "formula": "FIX_C*(P_n/P_(n-1)-1)",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--fix-c", type=float, default=1500.0)
    parser.add_argument("--anchor-price", type=float)
    parser.add_argument("--output", default="step_15.csv")
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    raw.attrs["anchor"] = {"price": args.anchor_price}
    values, diagnostics, provenance = transform(raw, previous, args.fix_c)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
