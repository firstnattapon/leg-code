"""Pure one-new-row LEGO transformations for the Webull learning dashboard.

One authenticated run owns one immutable Webull snapshot and produces one draft
row.  Steps 1→17 add one calculated column at a time; Step 18 validates and
finalizes the exact 17-column row.  Historical trade-log rows are never pipeline
input.  Only the latest finalized row is represented by :class:`PreviousAnchor`
for DNA and price-path recurrence.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import inspect
import json
import math
from pathlib import Path
from typing import Any, Callable, MutableMapping

import numpy as np
import pandas as pd

from lego_blocks.step_01_time_utc import transform as block_01
from lego_blocks.step_02_asset import transform as block_02
from lego_blocks.step_03_status import transform as block_03
from lego_blocks.step_04_dna_step import transform as block_04
from lego_blocks.step_05_dna_signal import transform as block_05
from lego_blocks.step_06_price import transform as block_06
from lego_blocks.step_07_holdings import transform as block_07
from lego_blocks.step_08_action import transform as block_08
from lego_blocks.step_09_side import transform as block_09
from lego_blocks.step_10_reason import transform as block_10
from lego_blocks.step_11_order_quantity import transform as block_11
from lego_blocks.step_12_portfolio_value import transform as block_12
from lego_blocks.step_13_target_gap import transform as block_13
from lego_blocks.step_14_reference import transform as block_14
from lego_blocks.step_15_delta_actual import transform as block_15
from lego_blocks.step_16_actual_cumulative import transform as block_16
from lego_blocks.step_17_excess import transform as block_17


PIPELINE_SCHEMA_VERSION = 4

FINAL_COLUMNS: tuple[str, ...] = (
    "เวลา (UTC)",
    "สินทรัพย์",
    "สถานะ",
    "DNA step",
    "DNA signal",
    "ราคา Pₙ (USD)",
    "จำนวนถือครอง (หุ้น)",
    "คำสั่ง",
    "ฝั่ง",
    "เหตุผล",
    "จำนวนสั่ง (หุ้น)",
    "มูลค่าพอร์ต (USD)",
    "ส่วนต่างเป้าหมาย (USD)",
    "Rₙ อ้างอิง (USD)",
    "ΔAₙ ต่อสเต็ป (USD)",
    "Aₙ สะสม (USD)",
    "Eₙ ส่วนเกินสะสม (USD)",
)

WHAT_IF_COLUMNS: tuple[str, ...] = (
    "เวลา (UTC)",
    "ราคา Pₙ (USD)",
    "Rₙ what-if (USD)",
    "ΔAₙ what-if (USD)",
    "Aₙ what-if สะสม (USD)",
    "Eₙ what-if สะสม (USD)",
)

INTERNAL_ROW_ID = "__lego_row_id"


@dataclass(frozen=True)
class PreviousAnchor:
    """Only persisted state allowed to influence the next calculated row."""

    row_id: str | None = None
    version: int = 0
    dna_step: int | None = None
    price: float | None = None
    p0: float | None = None
    actual_cumulative: float = 0.0

    @property
    def is_initial(self) -> bool:
        return self.row_id is None


@dataclass(frozen=True)
class PipelineContext:
    """Stable calculation parameters and anchor for one immutable snapshot."""

    fix_c: float
    diff: float = 0.0
    dna_code: str = ""
    decimal_precision: int = 5
    source_hash: str = ""
    run_id: str = ""
    chain_key: str = ""
    anchor: PreviousAnchor = field(default_factory=PreviousAnchor)

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.fix_c)) or float(self.fix_c) <= 0:
            raise ValueError("fix_c must be finite and greater than 0")
        if not math.isfinite(float(self.diff)) or float(self.diff) < 0:
            raise ValueError("diff must be finite and greater than or equal to 0")
        if int(self.decimal_precision) < 0:
            raise ValueError("decimal_precision must be greater than or equal to 0")


@dataclass(frozen=True)
class StageSpec:
    number: int
    column_name: str
    title: str
    quick_start: str
    learning_guide: str
    run_fn: Callable[
        [pd.DataFrame, pd.DataFrame, float],
        tuple[pd.Series, tuple[str, ...], dict[str, Any]],
    ]

    @property
    def source_code(self) -> str:
        source_path = inspect.getsourcefile(self.run_fn)
        if source_path is None:
            raise RuntimeError(f"cannot locate source file for LEGO Step {self.number}")
        return Path(source_path).read_text(encoding="utf-8")

    @property
    def goal(self) -> str:
        module = inspect.getmodule(self.run_fn)
        goal = getattr(module, "GOAL", "") if module is not None else ""
        if not goal:
            raise RuntimeError(f"LEGO Step {self.number} has no embedded GOAL")
        return str(goal)

    @property
    def file_name(self) -> str:
        source_path = inspect.getsourcefile(self.run_fn)
        if source_path is None:
            raise RuntimeError(f"cannot locate source file for LEGO Step {self.number}")
        return Path(source_path).name


@dataclass
class StageResult:
    stage_number: int
    frame: pd.DataFrame
    diagnostics: tuple[str, ...]
    provenance: dict[str, Any]
    input_hash: str
    output_hash: str
    completed_at: str


def prepare_raw_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize an immutable Step-0 snapshot and enforce one-row cardinality."""

    prepared = frame.copy()
    drop_columns = [
        column
        for column in prepared.columns
        if str(column).strip() == ""
        or str(column).startswith("Unnamed:")
        or str(column) in {"H1", INTERNAL_ROW_ID}
    ]
    if drop_columns:
        prepared = prepared.drop(columns=drop_columns)
    prepared = prepared.reset_index(drop=True)
    if len(prepared) != 1:
        raise ValueError("one-new-row pipeline requires exactly one snapshot row")
    prepared[INTERNAL_ROW_ID] = 0
    return prepared


def build_snapshot_frame(
    *,
    snapshot_at: str | datetime,
    symbol: str,
    price: float,
    holdings: float,
) -> pd.DataFrame:
    """Create the only raw row accepted by the calculation pipeline."""

    normalized_symbol = str(symbol).strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol is required")
    numeric_price = float(price)
    numeric_holdings = float(holdings)
    if not math.isfinite(numeric_price) or numeric_price <= 0:
        raise ValueError("snapshot price must be finite and greater than 0")
    if not math.isfinite(numeric_holdings) or numeric_holdings < 0:
        raise ValueError("snapshot holdings must be finite and non-negative")
    timestamp = pd.Timestamp(snapshot_at)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")
    return prepare_raw_frame(
        pd.DataFrame(
            [
                {
                    "snapshot_at": timestamp.isoformat(),
                    "symbol": normalized_symbol,
                    "last_price": numeric_price,
                    "quantity": numeric_holdings,
                }
            ]
        )
    )


def dataframe_fingerprint(frame: pd.DataFrame) -> str:
    payload = frame.to_json(
        orient="split",
        date_format="iso",
        date_unit="ms",
        default_handler=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stage_attrs(context: PipelineContext) -> dict[str, Any]:
    return {
        "dna_code": context.dna_code.strip(),
        "diff": float(context.diff),
        "decimal_precision": int(context.decimal_precision),
        "anchor": asdict(context.anchor),
        "run_id": context.run_id,
        "chain_key": context.chain_key,
    }


STAGES: tuple[StageSpec, ...] = (
    StageSpec(1, FINAL_COLUMNS[0], "เวลา UTC", "ใช้เวลา snapshot ที่ Step 0 ตรึงไว้", "หนึ่ง row ใช้ timestamp เดียวตลอด chain", block_01),
    StageSpec(2, FINAL_COLUMNS[1], "สินทรัพย์", "normalize symbol จาก snapshot", "symbol ต้องมาจาก input ปัจจุบัน ไม่อ่าน trade log เก่า", block_02),
    StageSpec(3, FINAL_COLUMNS[2], "สถานะ", "เริ่ม draft status เป็น SNAPSHOT_READY", "Step 18 จะ finalize เป็น PASS_DNA_ZERO, PASS_THRESHOLD, READY_BUY หรือ READY_SELL", block_03),
    StageSpec(4, FINAL_COLUMNS[3], "DNA step", "latest anchor + 1; chain แรกเริ่ม 0", "ใช้เฉพาะ latest final anchor และไม่สแกนประวัติ", block_04),
    StageSpec(5, FINAL_COLUMNS[4], "DNA signal", "decode DNA_CODE[DNA step]", "signal ต้อง deterministic และ out-of-range ต้อง fail-closed", block_05),
    StageSpec(6, FINAL_COLUMNS[5], "ราคา Pₙ", "ใช้ positive quote จาก immutable Step-0 snapshot", "ห้าม refresh รายแท็บเพราะหนึ่ง row ต้องอ้างข้อมูลเวลาเดียวกัน", block_06),
    StageSpec(7, FINAL_COLUMNS[6], "จำนวนถือครอง", "ใช้ holdings จาก Webull Positions snapshot", "ห้ามใช้ expected/calculated position", block_07),
    StageSpec(8, FINAL_COLUMNS[7], "คำสั่ง", "คำนวณ BUY/SELL/PASS จาก DNA, holdings, price, FIX_C และ DIFF", "ไม่คัดลอก decision field จากเอกสารเก่า", block_08),
    StageSpec(9, FINAL_COLUMNS[8], "ฝั่ง", "derive BUY/SELL side จากคำสั่ง", "PASS ไม่มี side", block_09),
    StageSpec(10, FINAL_COLUMNS[9], "เหตุผล", "derive DNA_ZERO/WITHIN_THRESHOLD/BELOW_TARGET/ABOVE_TARGET", "เหตุผลต้องสอดคล้องกับ decision เดียวกัน", block_10),
    StageSpec(11, FINAL_COLUMNS[10], "จำนวนสั่ง", "quantity = round(|gap|/price, precision)", "PASS ต้องเป็น 0 และค่าผิดต้อง fail-closed", block_11),
    StageSpec(12, FINAL_COLUMNS[11], "มูลค่าพอร์ต", "holdings × price", "คำนวณจาก snapshot ปัจจุบัน", block_12),
    StageSpec(13, FINAL_COLUMNS[12], "ส่วนต่างเป้าหมาย", "FIX_C − portfolio value", "บวก=BUY ลบ=SELL", block_13),
    StageSpec(14, FINAL_COLUMNS[13], "Rₙ อ้างอิง", "Rₙ = FIX_C × ln(Pₙ/P₀)", "P₀ มาจาก first row metadata", block_14),
    StageSpec(15, FINAL_COLUMNS[14], "ΔAₙ ต่อสเต็ป", "ΔAₙ = FIX_C × (Pₙ/Pₙ₋₁ − 1)", "ใช้ previous price จาก latest anchor เท่านั้น", block_15),
    StageSpec(16, FINAL_COLUMNS[15], "Aₙ สะสม", "Aₙ = Aₙ₋₁ + ΔAₙ", "แถวแรกเริ่ม 0", block_16),
    StageSpec(17, FINAL_COLUMNS[16], "Eₙ ส่วนเกินสะสม", "Eₙ = Aₙ − Rₙ", "identity ต้องเป็นจริงทุก final row", block_17),
)

STAGE_BY_NUMBER = {stage.number: stage for stage in STAGES}


def run_stage(
    stage_number: int,
    raw: pd.DataFrame,
    previous: StageResult | None,
    context: PipelineContext,
) -> StageResult:
    """Run one pure stage over exactly one immutable snapshot row."""

    try:
        spec = STAGE_BY_NUMBER[int(stage_number)]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("stage_number must be between 1 and 17") from exc
    snapshot = prepare_raw_frame(raw)
    if spec.number == 1:
        if previous is not None:
            raise ValueError("stage 1 must start without a previous StageResult")
        accumulated = pd.DataFrame(index=[0])
        previous_hash = "AUTH"
    else:
        if previous is None or previous.stage_number != spec.number - 1:
            raise ValueError(f"stage {spec.number} requires completed stage {spec.number - 1}")
        if len(previous.frame) != 1:
            raise ValueError("previous stage must contain exactly one draft row")
        accumulated = previous.frame.copy()
        previous_hash = previous.output_hash

    stage_raw = snapshot.copy(deep=False)
    stage_raw.attrs.update(_stage_attrs(context))
    series, diagnostics, provenance = spec.run_fn(
        stage_raw, accumulated, float(context.fix_c)
    )
    if len(series) != 1:
        raise ValueError(f"stage {spec.number} must return exactly one value")
    accumulated[spec.column_name] = series.to_numpy()

    input_payload = json.dumps(
        {
            "stage": spec.number,
            "snapshot": context.source_hash or dataframe_fingerprint(snapshot),
            "previous": previous_hash,
            "fix_c": float(context.fix_c),
            "diff": float(context.diff),
            "precision": int(context.decimal_precision),
            "dna_code_hash": hashlib.sha256(
                context.dna_code.strip().encode("utf-8")
            ).hexdigest(),
            "anchor": asdict(context.anchor),
            "run_id": context.run_id,
            "chain_key": context.chain_key,
        },
        sort_keys=True,
        default=str,
    )
    input_hash = hashlib.sha256(input_payload.encode("utf-8")).hexdigest()
    output_hash = dataframe_fingerprint(accumulated)
    return StageResult(
        stage_number=spec.number,
        frame=accumulated,
        diagnostics=tuple(diagnostics),
        provenance=dict(provenance),
        input_hash=input_hash,
        output_hash=output_hash,
        completed_at=datetime.now(timezone.utc).isoformat(),
    )


def invalidate_from(
    results: MutableMapping[int, StageResult], stage_number: int
) -> None:
    for key in list(results):
        if int(key) >= int(stage_number):
            del results[key]


def _final_status(frame: pd.DataFrame) -> str:
    signal = int(pd.to_numeric(frame["DNA signal"], errors="raise").iloc[0])
    action = str(frame["คำสั่ง"].iloc[0]).strip().upper()
    if signal == 0:
        return "PASS_DNA_ZERO"
    if action == "PASS":
        return "PASS_THRESHOLD"
    if action == "BUY":
        return "READY_BUY"
    if action == "SELL":
        return "READY_SELL"
    raise ValueError("cannot finalize unknown calculation action")


def final_dataframe(result: StageResult) -> pd.DataFrame:
    """Validate and finalize exactly one 17-column row for Step 18."""

    if result.stage_number != 17:
        raise ValueError("final dataframe requires completed stage 17")
    if len(result.frame) != 1:
        raise ValueError("final dataframe requires exactly one row")
    missing = [column for column in FINAL_COLUMNS if column not in result.frame.columns]
    if missing:
        raise ValueError(f"missing final columns: {missing}")
    final = result.frame.loc[:, FINAL_COLUMNS].copy().reset_index(drop=True)
    final.loc[0, "สถานะ"] = _final_status(final)
    for column in FINAL_COLUMNS[11:]:
        final[column] = pd.to_numeric(final[column], errors="raise").round(2)
    return final


def what_if_dataframe(result: StageResult, fix_c: float) -> pd.DataFrame:
    """Compatibility view: the final row is already the price-path ledger."""

    final = final_dataframe(result)
    return pd.DataFrame(
        [
            {
                "เวลา (UTC)": final.loc[0, "เวลา (UTC)"],
                "ราคา Pₙ (USD)": final.loc[0, "ราคา Pₙ (USD)"],
                "Rₙ what-if (USD)": final.loc[0, "Rₙ อ้างอิง (USD)"],
                "ΔAₙ what-if (USD)": final.loc[0, "ΔAₙ ต่อสเต็ป (USD)"],
                "Aₙ what-if สะสม (USD)": final.loc[0, "Aₙ สะสม (USD)"],
                "Eₙ what-if สะสม (USD)": final.loc[0, "Eₙ ส่วนเกินสะสม (USD)"],
            }
        ],
        columns=WHAT_IF_COLUMNS,
    )
