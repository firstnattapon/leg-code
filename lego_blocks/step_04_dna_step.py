"""Goal: calculate the next DNA step from the latest finalized anchor.

Quick Start:
    python step_04_dna_step.py --raw snapshot.csv --previous step_03.csv
"""

from __future__ import annotations

import argparse
import json
import pandas as pd

GOAL = "คำนวณ DNA step = latest final step + 1; chain แรกเริ่ม 0"
COLUMN = "DNA step"


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    anchor = dict(raw.attrs.get("anchor", {}))
    prior = anchor.get("dna_step")
    step = 0 if prior is None else int(prior) + 1
    if step < 0:
        raise ValueError("DNA step cannot be negative")
    values = pd.Series([step], index=raw.index, dtype="Int64")
    return values, (f"next DNA step = {step}",), {
        "anchor_row_id": anchor.get("row_id"),
        "anchor_version": int(anchor.get("version", 0)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--anchor-dna-step", type=int)
    parser.add_argument("--output", default="step_04.csv")
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    raw.attrs["anchor"] = {"dna_step": args.anchor_dna_step}
    values, diagnostics, provenance = transform(raw, previous, 1.0)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
