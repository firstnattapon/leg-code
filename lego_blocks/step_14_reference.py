"""Goal: calculate R_n from the current price and persisted P0.

Quick Start:
    python step_14_reference.py --raw snapshot.csv --previous step_13.csv --fix-c 1500
"""

from __future__ import annotations

import argparse
import json
import math
import pandas as pd

GOAL = "คำนวณ Rₙ = FIX_C × ln(Pₙ/P₀) จาก latest chain metadata"
COLUMN = "Rₙ อ้างอิง (USD)"


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    price = float(previous["ราคา Pₙ (USD)"].iloc[0])
    anchor = dict(raw.attrs.get("anchor", {}))
    p0 = price if anchor.get("p0") is None else float(anchor["p0"])
    if price <= 0 or p0 <= 0:
        raise ValueError("P_n and P_0 must be greater than 0")
    reference = float(fix_c) * math.log(price / p0)
    values = pd.Series([reference], index=raw.index, dtype=float)
    return values, (f"P0={p0:.10f}",), {"p0": p0, "formula": "FIX_C*ln(P_n/P_0)"}


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--fix-c", type=float, default=1500.0)
    parser.add_argument("--anchor-p0", type=float)
    parser.add_argument("--output", default="step_14.csv")
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    raw.attrs["anchor"] = {"p0": args.anchor_p0}
    values, diagnostics, provenance = transform(raw, previous, args.fix_c)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
