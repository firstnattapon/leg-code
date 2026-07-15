"""Goal: prove or decode nullable binary ``DNA signal`` values.

Quick Start:
    pip install pandas numpy
    python step_05_dna_signal.py --raw raw.csv --previous step_04.csv --dna-code "bypass:100"

``DNA_CODE`` follows the Shannon Demon Learning Guide exactly:

* encoded: compact ``[width][value]`` stream containing length, mutation rate,
  DNA seed, then zero or more mutation seeds;
* ``bypass:N`` or ``[1,N]``: an explicit all-ones learning/test sequence.

Logged broker/bot signals always win.  Decoding only fills rows whose signal is
missing and whose logged DNA step points inside the decoded sequence.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

GOAL = "ตรวจ signal ที่บอทบันทึก และถอด DNA_CODE ด้วย seed + mutation ตาม Learning Guide"
COLUMN = "DNA signal"


@dataclass(frozen=True)
class DnaSpec:
    length: int
    mutation_rate: float
    dna_seed: int
    mutation_seeds: tuple[int, ...]
    raw_numbers: tuple[int, ...]


def decode_number_stream(encoded: str) -> list[int]:
    """Decode ``[width][value][width][value]...`` into integers."""

    if not encoded or not encoded.isdigit():
        raise ValueError("DNA string must be a non-empty digit string")
    values: list[int] = []
    index = 0
    while index < len(encoded):
        width = int(encoded[index])
        index += 1
        if width <= 0:
            raise ValueError("DNA token width must be greater than 0")
        end = index + width
        if end > len(encoded):
            raise ValueError("DNA string ended before a full token was decoded")
        values.append(int(encoded[index:end]))
        index = end
    return values


def _mutation_rate(raw_rate: int | float) -> float:
    rate = float(raw_rate)
    if rate < 0:
        raise ValueError("DNA mutation rate cannot be negative")
    if rate > 1:
        rate /= 100.0
    if rate > 1:
        raise ValueError("DNA mutation rate cannot be greater than 100%")
    return rate


def parse_dna_spec(encoded: str) -> DnaSpec:
    numbers = decode_number_stream(encoded)
    if len(numbers) < 3:
        raise ValueError("DNA string must encode length, rate, and at least one seed")
    if numbers[0] <= 0:
        raise ValueError("DNA length must be greater than 0")
    return DnaSpec(
        length=int(numbers[0]),
        mutation_rate=_mutation_rate(numbers[1]),
        dna_seed=int(numbers[2]),
        mutation_seeds=tuple(int(seed) for seed in numbers[3:]),
        raw_numbers=tuple(numbers),
    )


def _bypass_length(encoded: str) -> int | None:
    text = encoded.strip()
    if text.lower().startswith("bypass:"):
        try:
            length = int(text.split(":", 1)[1].strip())
        except ValueError as exc:
            raise ValueError("Bypass DNA length must be an integer") from exc
        if length <= 0:
            raise ValueError("Bypass DNA length must be greater than 0")
        return length
    if text.startswith("["):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("Bypass DNA array format must be [1, length]") from exc
        if (
            not isinstance(value, list)
            or len(value) != 2
            or type(value[0]) is not int
            or type(value[1]) is not int
            or value[0] != 1
            or value[1] <= 0
        ):
            raise ValueError("Bypass DNA array format must be [1, length]")
        return value[1]
    return None


def decode_dna(encoded: str) -> tuple[np.ndarray, dict[str, object]]:
    """Return the deterministic Hybrid Multi-Mutation signal array + metadata."""

    bypass = _bypass_length(encoded)
    if bypass is not None:
        return np.ones(bypass, dtype=np.int8), {
            "mode": "bypass",
            "length": bypass,
            "mutation_rate": 0.0,
            "seeds": [],
        }

    spec = parse_dna_spec(encoded)
    rng = np.random.default_rng(seed=spec.dna_seed)
    dna = rng.integers(0, 2, size=spec.length).astype(np.int8)
    dna[0] = 1
    for seed in spec.mutation_seeds:
        mutation_rng = np.random.default_rng(seed=seed)
        mask = mutation_rng.random(spec.length) < spec.mutation_rate
        dna[mask] = 1 - dna[mask]
        dna[0] = 1
    metadata = asdict(spec)
    metadata.update({"mode": "encoded", "seeds": [spec.dna_seed, *spec.mutation_seeds]})
    return dna, metadata


def _number(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    for name in names:
        if name in frame:
            result = result.where(result.notna(), pd.to_numeric(frame[name], errors="coerce"))
    return result


def _logged_signal(frame: pd.DataFrame) -> pd.Series:
    result = pd.Series(pd.NA, index=frame.index, dtype="object")
    for name in ("dna_signal", COLUMN):
        if name not in frame:
            continue
        values = frame[name]
        present = values.notna() & values.astype(str).str.strip().ne("")
        result = result.where(result.notna() | ~present, values)
    return result


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    logged = _logged_signal(raw)
    logged_present = logged.notna()
    numeric = _number(raw, ("dna_signal", COLUMN))
    valid = numeric.isin((0, 1))
    values = numeric.where(valid).astype("Int8")
    rejected = int((logged_present & ~valid).sum())
    dna_code = str(raw.attrs.get("dna_code", "")).strip()
    decoded_rows = 0
    out_of_range = 0
    metadata: dict[str, object] = {"mode": "logged-only"}
    if dna_code:
        dna, metadata = decode_dna(dna_code)
        steps = pd.to_numeric(previous.get("DNA step"), errors="coerce")
        missing = ~logged_present & values.isna() & steps.notna()
        for index in values.index[missing]:
            step = int(steps.loc[index])
            if 0 <= step < len(dna):
                values.loc[index] = int(dna[step])
                decoded_rows += 1
            else:
                out_of_range += 1
    diagnostics = (
        f"signal จาก log {int(valid.sum())}/{len(raw)} แถว",
        f"signal ที่ถอดจาก DNA_CODE {decoded_rows} แถว",
        f"step นอกช่วง DNA {out_of_range} แถว",
        f"ปฏิเสธ signal ที่ไม่ใช่ 0/1 จำนวน {rejected} ค่า",
    )
    provenance = {
        "priority": ["logged dna_signal", "decoded DNA_CODE[dna_step]"],
        "decoder": metadata,
        "dna_code_stored": False,
    }
    return values, diagnostics, provenance


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--output", default="step_05.csv")
    parser.add_argument(
        "--dna-code",
        default="",
        help="Encoded DNA, bypass:N, or [1,N]. Logged signals still take priority.",
    )
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    raw.attrs["dna_code"] = args.dna_code
    if len(raw) != len(previous):
        raise ValueError("raw and previous must have the same row count")
    values, diagnostics, provenance = transform(raw, previous, 1500.0)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
