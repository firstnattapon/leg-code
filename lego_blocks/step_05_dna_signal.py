"""Goal: deterministically decode one DNA signal for the calculated DNA step.

Quick Start:
    python step_05_dna_signal.py --raw snapshot.csv --previous step_04.csv --dna-code "bypass:100"
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json

import numpy as np
import pandas as pd

GOAL = "คำนวณ DNA signal = DNA_CODE[DNA step] โดยไม่อ่าน signal จาก trade log"
COLUMN = "DNA signal"


@dataclass(frozen=True)
class DnaSpec:
    length: int
    mutation_rate: float
    dna_seed: int
    mutation_seeds: tuple[int, ...]


def decode_number_stream(encoded: str) -> list[int]:
    if not encoded or not encoded.isdigit():
        raise ValueError("DNA string must be a non-empty digit string")
    values: list[int] = []
    index = 0
    while index < len(encoded):
        width = int(encoded[index])
        index += 1
        if width <= 0 or index + width > len(encoded):
            raise ValueError("invalid DNA width/value stream")
        values.append(int(encoded[index:index + width]))
        index += width
    return values


def parse_dna_spec(encoded: str) -> DnaSpec:
    numbers = decode_number_stream(encoded)
    if len(numbers) < 3 or numbers[0] <= 0:
        raise ValueError("DNA must encode length, mutation rate, and seed")
    rate = float(numbers[1])
    if rate > 1:
        rate /= 100.0
    if not 0 <= rate <= 1:
        raise ValueError("DNA mutation rate must be between 0 and 100%")
    return DnaSpec(
        length=int(numbers[0]),
        mutation_rate=rate,
        dna_seed=int(numbers[2]),
        mutation_seeds=tuple(int(value) for value in numbers[3:]),
    )


def decode_dna(encoded: str) -> tuple[np.ndarray, dict[str, object]]:
    text = encoded.strip()
    if text.lower().startswith("bypass:"):
        length = int(text.split(":", 1)[1])
        if length <= 0:
            raise ValueError("bypass length must be greater than 0")
        return np.ones(length, dtype=np.int8), {"mode": "bypass", "length": length}
    if text.startswith("["):
        value = json.loads(text)
        if (
            not isinstance(value, list)
            or len(value) != 2
            or value[0] != 1
            or type(value[1]) is not int
            or value[1] <= 0
        ):
            raise ValueError("bypass array must be [1, length]")
        return np.ones(value[1], dtype=np.int8), {
            "mode": "bypass",
            "length": value[1],
        }
    spec = parse_dna_spec(text)
    dna = np.random.default_rng(spec.dna_seed).integers(
        0, 2, size=spec.length
    ).astype(np.int8)
    dna[0] = 1
    for seed in spec.mutation_seeds:
        mask = np.random.default_rng(seed).random(spec.length) < spec.mutation_rate
        dna[mask] = 1 - dna[mask]
        dna[0] = 1
    metadata = asdict(spec)
    metadata.update({"mode": "encoded"})
    return dna, metadata


def transform(raw: pd.DataFrame, previous: pd.DataFrame, fix_c: float):
    dna_code = str(raw.attrs.get("dna_code", "")).strip()
    if not dna_code:
        raise ValueError("DNA_CODE is required")
    step = int(previous["DNA step"].iloc[0])
    dna, metadata = decode_dna(dna_code)
    if not 0 <= step < len(dna):
        raise ValueError(f"DNA step {step} is outside decoded length {len(dna)}")
    signal = int(dna[step])
    values = pd.Series([signal], index=raw.index, dtype="Int8")
    return values, (f"DNA[{step}]={signal}",), {
        "decoder": metadata,
        "dna_code_stored": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=GOAL)
    parser.add_argument("--raw", required=True)
    parser.add_argument("--previous", required=True)
    parser.add_argument("--dna-code", required=True)
    parser.add_argument("--output", default="step_05.csv")
    args = parser.parse_args()
    raw, previous = pd.read_csv(args.raw), pd.read_csv(args.previous)
    raw.attrs["dna_code"] = args.dna_code
    values, diagnostics, provenance = transform(raw, previous, 1.0)
    previous[COLUMN] = values.to_numpy()
    previous.to_csv(args.output, index=False)
    print(json.dumps({"goal": GOAL, "diagnostics": diagnostics, "provenance": provenance}))


if __name__ == "__main__":
    main()
