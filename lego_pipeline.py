"""Pure, sequential LEGO transformations for the Webull learning dashboard.

The module deliberately contains no Streamlit, Webull, or Firestore calls.  Each
``StageSpec`` points at ``transform`` in a complete, standalone Python file; the
UI prints that entire trusted file and executes the same callable.  Raw broker
documents remain separate from the accumulated 17-column learning dataframe so
secrets and unreviewed fields can never leak into exports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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

from trade_log import (
    EXECUTION_PRICE_COLUMNS,
    FEE_COLUMNS,
    FILLED_QUANTITY_COLUMNS,
    TRADE_PRICE_COLUMNS,
    observed_holdings_series,
    realized_cashflow_from_trades,
)


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
class PipelineContext:
    """Stable values shared by every transformation in one authenticated run."""

    fix_c: float
    source_hash: str = ""

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.fix_c)) or float(self.fix_c) <= 0:
            raise ValueError("fix_c must be finite and greater than 0")


@dataclass(frozen=True)
class StageValue:
    """One column plus human-readable validation details."""

    series: pd.Series
    diagnostics: tuple[str, ...] = ()
    provenance: dict[str, Any] = field(default_factory=dict)


StageFunction = Callable[
    [pd.DataFrame, pd.DataFrame, float],
    tuple[pd.Series, tuple[str, ...], dict[str, Any]],
]


@dataclass(frozen=True)
class StageSpec:
    number: int
    column_name: str
    title: str
    quick_start: str
    learning_guide: str
    run_fn: StageFunction

    @property
    def source_code(self) -> str:
        """Return the complete standalone file containing the executed callable."""

        source_path = inspect.getsourcefile(self.run_fn)
        if source_path is None:
            raise RuntimeError(f"cannot locate source file for LEGO Step {self.number}")
        return Path(source_path).read_text(encoding="utf-8")

    @property
    def goal(self) -> str:
        """Return the goal embedded in the same standalone runtime file."""

        module = inspect.getmodule(self.run_fn)
        goal = getattr(module, "GOAL", "") if module is not None else ""
        if not goal:
            raise RuntimeError(f"LEGO Step {self.number} has no embedded GOAL")
        return str(goal)

    @property
    def file_name(self) -> str:
        """Return a portable filename for downloading/running this block."""

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


def _object_series(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    result = pd.Series(pd.NA, index=frame.index, dtype="object")
    for name in names:
        if name not in frame.columns:
            continue
        values = frame[name]
        usable = values.notna() & values.astype(str).str.strip().ne("")
        result = result.where(result.notna() | ~usable, values)
    return result


def _text_series(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    values = _object_series(frame, names)
    result = values.astype("string").str.strip()
    return result.mask(result.eq(""), pd.NA)


def _numeric_series(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    for name in names:
        if name not in frame.columns:
            continue
        numeric = pd.to_numeric(frame[name], errors="coerce")
        result = result.where(result.notna(), numeric)
    return result


def _candidate_columns(columns, names: tuple[str, ...]) -> list[str]:
    candidates = [name for name in names if name in columns]
    for name in names:
        suffix = f"_{name}"
        for column in columns:
            if str(column).endswith(suffix) and column not in candidates:
                candidates.append(column)
    return candidates


def _numeric_with_suffixes(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    return _numeric_series(frame, tuple(_candidate_columns(frame.columns, names)))


def _text_with_suffixes(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    return _text_series(frame, tuple(_candidate_columns(frame.columns, names)))


def prepare_raw_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Remove CSV export indexes and normalize all sources to chronological rows."""

    prepared = frame.copy()
    drop_columns = [
        column
        for column in prepared.columns
        if str(column).strip() == ""
        or str(column).startswith("Unnamed:")
        or str(column) == "H1"
    ]
    if drop_columns:
        prepared = prepared.drop(columns=drop_columns)

    prepared[INTERNAL_ROW_ID] = np.arange(len(prepared), dtype=int)
    time_values = _object_series(prepared, ("created_at", "เวลา (UTC)"))
    parsed = pd.to_datetime(time_values, errors="coerce", utc=True)
    prepared["__lego_sort_time"] = parsed
    prepared = prepared.sort_values(
        ["__lego_sort_time", INTERNAL_ROW_ID],
        kind="stable",
        na_position="last",
    ).reset_index(drop=True)
    return prepared.drop(columns=["__lego_sort_time"])


def dataframe_fingerprint(frame: pd.DataFrame) -> str:
    """Hash nested/object-heavy data deterministically enough for UI invalidation."""

    payload = frame.to_json(
        orient="split",
        date_format="iso",
        date_unit="ms",
        default_handler=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _stage_time(
    raw: pd.DataFrame, accumulated: pd.DataFrame, context: PipelineContext
) -> StageValue:
    """Parse created_at into a nullable ISO-8601 UTC column."""

    source = _object_series(raw, ("created_at", "เวลา (UTC)"))
    parsed = pd.to_datetime(source, errors="coerce", utc=True)
    values = pd.Series(pd.NA, index=raw.index, dtype="string")
    valid = parsed.notna()
    values.loc[valid] = parsed.loc[valid].map(
        lambda value: value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    return StageValue(
        values,
        (f"เวลาใช้ได้ {int(valid.sum())}/{len(raw)} แถว",),
        {"source": "created_at → เวลา (UTC)"},
    )


def _stage_asset(
    raw: pd.DataFrame, accumulated: pd.DataFrame, context: PipelineContext
) -> StageValue:
    """Normalize the logged symbol without inventing a missing asset."""

    values = _text_series(raw, ("symbol", "สินทรัพย์")).str.upper()
    return StageValue(
        values,
        (f"สินทรัพย์ไม่ว่าง {int(values.notna().sum())}/{len(raw)} แถว",),
        {"source": "symbol"},
    )


def _stage_status(
    raw: pd.DataFrame, accumulated: pd.DataFrame, context: PipelineContext
) -> StageValue:
    """Normalize explicit status text; missing statuses stay nullable."""

    values = _text_series(raw, ("status", "สถานะ")).str.upper()
    return StageValue(
        values,
        (f"พบสถานะ {values.dropna().nunique()} แบบ",),
        {"source": "status"},
    )


def _stage_dna_step(
    raw: pd.DataFrame, accumulated: pd.DataFrame, context: PipelineContext
) -> StageValue:
    """Accept only whole, non-negative DNA step numbers."""

    numeric = _numeric_series(raw, ("dna_step", "DNA step"))
    valid = numeric.notna() & numeric.ge(0) & np.isclose(numeric % 1, 0)
    values = numeric.where(valid).astype("Int64")
    rejected = int(numeric.notna().sum() - valid.sum())
    return StageValue(
        values,
        (f"DNA step ใช้ได้ {int(valid.sum())}/{len(raw)} แถว", f"ปฏิเสธ {rejected} ค่า"),
        {"rule": "integer >= 0"},
    )


def _stage_dna_signal(
    raw: pd.DataFrame, accumulated: pd.DataFrame, context: PipelineContext
) -> StageValue:
    """Accept only the trained binary DNA signals 0 and 1."""

    numeric = _numeric_series(raw, ("dna_signal", "DNA signal"))
    valid = numeric.isin((0, 1))
    values = numeric.where(valid).astype("Int8")
    rejected = int(numeric.notna().sum() - valid.sum())
    return StageValue(
        values,
        (f"DNA signal ใช้ได้ {int(valid.sum())}/{len(raw)} แถว", f"ปฏิเสธ {rejected} ค่า"),
        {"rule": "0 or 1"},
    )


def _stage_price(
    raw: pd.DataFrame, accumulated: pd.DataFrame, context: PipelineContext
) -> StageValue:
    """Coalesce known quote fields and retain finite positive prices only."""

    names = (*TRADE_PRICE_COLUMNS, "ราคา Pₙ (USD)")
    numeric = _numeric_with_suffixes(raw, names)
    valid = numeric.notna() & np.isfinite(numeric) & numeric.gt(0)
    values = numeric.where(valid)
    return StageValue(
        values,
        (f"ราคาบวกใช้ได้ {int(valid.sum())}/{len(raw)} แถว",),
        {"candidate_fields": list(names)},
    )


def _stage_holdings(
    raw: pd.DataFrame, accumulated: pd.DataFrame, context: PipelineContext
) -> StageValue:
    """Use broker-observed holdings and never expected_position_after."""

    observed = observed_holdings_series(raw)
    csv_values = _numeric_series(raw, ("จำนวนถือครอง (หุ้น)",))
    values = observed.where(observed.notna(), csv_values)
    valid = values.notna() & np.isfinite(values) & values.ge(0)
    values = values.where(valid)
    return StageValue(
        values,
        (f"holdings ที่ยืนยัน/อ้างอิงได้ {int(valid.sum())}/{len(raw)} แถว",),
        {"priority": ["position_after", "market_state_quantity", "quantity"]},
    )


def _stage_action(
    raw: pd.DataFrame, accumulated: pd.DataFrame, context: PipelineContext
) -> StageValue:
    """Normalize BUY, SELL, and PASS decisions from the logged decision."""

    values = _text_series(raw, ("decision_action", "action", "คำสั่ง")).str.upper()
    valid = values.isin(("BUY", "SELL", "PASS"))
    values = values.where(valid)
    return StageValue(
        values,
        (f"คำสั่งมาตรฐาน {int(valid.sum())}/{len(raw)} แถว",),
        {"allowed": ["BUY", "SELL", "PASS"]},
    )


def _stage_side(
    raw: pd.DataFrame, accumulated: pd.DataFrame, context: PipelineContext
) -> StageValue:
    """Normalize the broker/order side; PASS rows remain blank."""

    values = _text_series(raw, ("side", "decision_side", "ฝั่ง")).str.upper()
    valid = values.isin(("BUY", "SELL"))
    values = values.where(valid)
    return StageValue(
        values,
        (f"ฝั่ง BUY/SELL {int(valid.sum())}/{len(raw)} แถว",),
        {"allowed": ["BUY", "SELL"]},
    )


def _stage_reason(
    raw: pd.DataFrame, accumulated: pd.DataFrame, context: PipelineContext
) -> StageValue:
    """Preserve the bot's explicit decision reason as normalized text."""

    values = _text_series(raw, ("decision_reason", "reason", "เหตุผล")).str.upper()
    return StageValue(
        values,
        (f"เหตุผลไม่ว่าง {int(values.notna().sum())}/{len(raw)} แถว",),
        {"source": "decision_reason"},
    )


def _stage_order_quantity(
    raw: pd.DataFrame, accumulated: pd.DataFrame, context: PipelineContext
) -> StageValue:
    """Coalesce the decision quantity and reject negative/non-finite values."""

    numeric = _numeric_series(
        raw,
        (
            "decision_order_qty",
            "decision_order_quantity",
            "order_quantity",
            "จำนวนสั่ง (หุ้น)",
        ),
    )
    valid = numeric.notna() & np.isfinite(numeric) & numeric.ge(0)
    values = numeric.where(valid)
    return StageValue(
        values,
        (f"จำนวนสั่งใช้ได้ {int(valid.sum())}/{len(raw)} แถว",),
        {"rule": "finite quantity >= 0"},
    )


def _stage_portfolio_value(
    raw: pd.DataFrame, accumulated: pd.DataFrame, context: PipelineContext
) -> StageValue:
    """Prefer decision-time value; otherwise derive it from safe quantity and price."""

    logged = _numeric_series(
        raw,
        ("decision_value_now_usd", "value_now_usd", "มูลค่าพอร์ต (USD)"),
    )
    position_before = _numeric_series(
        raw, ("position_before", "pre_order_market_state_quantity")
    )
    holdings = pd.to_numeric(accumulated["จำนวนถือครอง (หุ้น)"], errors="coerce")
    price = pd.to_numeric(accumulated["ราคา Pₙ (USD)"], errors="coerce")
    decision_quantity = position_before.where(position_before.notna(), holdings)
    fallback = decision_quantity * price
    values = logged.where(logged.notna(), fallback)
    values = values.where(values.notna() & np.isfinite(values) & values.ge(0))
    logged_count = int(logged.notna().sum())
    fallback_count = int((logged.isna() & values.notna()).sum())
    return StageValue(
        values.round(2),
        (f"logged {logged_count} แถว", f"fallback quantity×price {fallback_count} แถว"),
        {"logged_rows": logged_count, "fallback_rows": fallback_count},
    )


def _stage_target_gap(
    raw: pd.DataFrame, accumulated: pd.DataFrame, context: PipelineContext
) -> StageValue:
    """Compute signed target gap: positive means buy, negative means sell."""

    value = pd.to_numeric(accumulated["มูลค่าพอร์ต (USD)"], errors="coerce")
    gap = (float(context.fix_c) - value).round(2)
    return StageValue(
        gap,
        (f"FIX_C = {float(context.fix_c):,.2f} USD", "บวก=BUY · ลบ=SELL"),
        {"formula": "FIX_C - portfolio_value"},
    )


def _confirmed_ledger(raw: pd.DataFrame, fix_c: float) -> tuple[pd.DataFrame, float | None]:
    """Build the existing broker-confirmed ledger with an in-window anchor."""

    if raw.empty:
        return pd.DataFrame(
            {
                "ln_reference": pd.Series(dtype=float),
                "delta_actual": pd.Series(dtype=float),
                "actual_cumulative": pd.Series(dtype=float),
                "excess": pd.Series(dtype=float),
            },
            index=raw.index,
        ), None
    probe = realized_cashflow_from_trades(raw, float(fix_c), 1.0)
    execution_prices = probe.loc[probe["eligible"], "execution_price"].dropna()
    if execution_prices.empty:
        empty = pd.DataFrame(
            {
                "ln_reference": np.nan,
                "delta_actual": np.nan,
                "actual_cumulative": np.nan,
                "excess": np.nan,
            },
            index=raw.index,
        )
        return empty, None
    p0 = float(execution_prices.iloc[0])
    return realized_cashflow_from_trades(raw, float(fix_c), p0), p0


def _stage_reference(
    raw: pd.DataFrame, accumulated: pd.DataFrame, context: PipelineContext
) -> StageValue:
    """Use only the last confirmed execution price for R_n."""

    ledger, p0 = _confirmed_ledger(raw, context.fix_c)
    values = ledger["ln_reference"].round(2)
    message = "ยังไม่มี broker-confirmed fill" if p0 is None else f"P₀ execution = {p0:,.5f}"
    return StageValue(values, (message,), {"execution_anchor_p0": p0})


def _stage_delta_actual(
    raw: pd.DataFrame, accumulated: pd.DataFrame, context: PipelineContext
) -> StageValue:
    """Count incremental filled notional once, fee-aware when fee is logged."""

    ledger, p0 = _confirmed_ledger(raw, context.fix_c)
    values = ledger["delta_actual"].round(2)
    counted = int(values.fillna(0).ne(0).sum())
    return StageValue(
        values,
        (f"execution increments ที่ขยับเงินจริง {counted} แถว",),
        {
            "filled_quantity_fields": list(FILLED_QUANTITY_COLUMNS),
            "execution_price_fields": list(EXECUTION_PRICE_COLUMNS),
            "fee_fields": list(FEE_COLUMNS),
            "execution_anchor_p0": p0,
        },
    )


def _stage_actual_cumulative(
    raw: pd.DataFrame, accumulated: pd.DataFrame, context: PipelineContext
) -> StageValue:
    """Expose the chronological cumulative broker-confirmed cash balance."""

    ledger, p0 = _confirmed_ledger(raw, context.fix_c)
    values = ledger["actual_cumulative"].round(2)
    return StageValue(
        values,
        ("แถวก่อน confirmed fill แรกจะว่างโดยตั้งใจ",),
        {"execution_anchor_p0": p0},
    )


def _stage_excess(
    raw: pd.DataFrame, accumulated: pd.DataFrame, context: PipelineContext
) -> StageValue:
    """Compute broker-confirmed E_n as A_n minus execution reference R_n."""

    actual = pd.to_numeric(accumulated["Aₙ สะสม (USD)"], errors="coerce")
    reference = pd.to_numeric(accumulated["Rₙ อ้างอิง (USD)"], errors="coerce")
    values = (actual - reference).round(2)
    return StageValue(
        values,
        (f"Eₙ ที่พิสูจน์ได้ {int(values.notna().sum())}/{len(raw)} แถว",),
        {"formula": "A_n - R_n"},
    )


STAGES: tuple[StageSpec, ...] = (
    StageSpec(1, FINAL_COLUMNS[0], "เวลา UTC", "อ่าน created_at แล้วกด Run เพื่อสร้างเวลา UTC", "แปลงเวลาให้มี timezone ชัดเจน ค่าเสียจะเว้นว่างแทนการเดา", block_01),
    StageSpec(2, FINAL_COLUMNS[1], "สินทรัพย์", "รับตารางจาก Step 1 แล้ว normalize symbol", "ใช้ชื่อสินทรัพย์ที่ log บันทึกจริงและแปลงเป็นตัวพิมพ์ใหญ่", block_02),
    StageSpec(3, FINAL_COLUMNS[2], "สถานะ", "เพิ่มสถานะ lifecycle จาก trade log", "สถานะบอกว่าเป็น PASS, pending, filled หรือ error โดยไม่สร้างสถานะใหม่", block_03),
    StageSpec(4, FINAL_COLUMNS[3], "DNA step", "ตรวจลำดับ DNA เป็นจำนวนเต็มไม่ติดลบ", "DNA step คือ index ของ signal ตามเวลา ค่าเสียไม่ควรถูกปัดให้ดูเหมือนถูก", block_04),
    StageSpec(5, FINAL_COLUMNS[4], "DNA signal", "รับเฉพาะ signal 0 หรือ 1", "0 หมายถึงข้ามและ 1 หมายถึงเปิด gate ถัดไป ค่าอื่นถือว่าข้อมูลเสีย", block_05),
    StageSpec(6, FINAL_COLUMNS[5], "ราคา Pₙ", "เลือก quote บวกตัวแรกจาก field ที่รองรับ", "ราคานี้เป็น decision-time quote สำหรับตารางเรียนรู้ ไม่ใช่หลักฐานราคา fill", block_06),
    StageSpec(7, FINAL_COLUMNS[6], "จำนวนถือครอง", "เลือก holdings ที่ Webull Positions ยืนยัน", "ห้ามใช้ expected_position_after เพราะเป็นเพียงค่าคาดการณ์หลัง order", block_07),
    StageSpec(8, FINAL_COLUMNS[7], "คำสั่ง", "normalize BUY, SELL หรือ PASS", "คำสั่งมาจาก decision log และไม่ทำให้เกิด order เมื่อกด Run", block_08),
    StageSpec(9, FINAL_COLUMNS[8], "ฝั่ง", "แยก BUY/SELL side จากคำสั่ง", "PASS ไม่มีฝั่งจึงเว้นว่างอย่างตั้งใจ", block_09),
    StageSpec(10, FINAL_COLUMNS[9], "เหตุผล", "ส่งเหตุผลของ decision ต่อไป", "เหตุผลช่วยอธิบายว่าต่ำกว่าเป้า สูงกว่าเป้า หรืออยู่ใน threshold", block_10),
    StageSpec(11, FINAL_COLUMNS[10], "จำนวนสั่ง", "ตรวจ order quantity ก่อนเปิด UAT panel", "Run สร้างคอลัมน์เท่านั้น การ preview/place/cancel ใช้ปุ่มแยก", block_11),
    StageSpec(12, FINAL_COLUMNS[11], "มูลค่าพอร์ต", "ใช้ logged value หรือ fallback quantity×price", "ค่าจาก decision-time log มาก่อน เพื่อไม่เอา post-fill holdings ไปคูณย้อนหลัง", block_12),
    StageSpec(13, FINAL_COLUMNS[12], "ส่วนต่างเป้าหมาย", "คำนวณ FIX_C − มูลค่าพอร์ต", "ค่าบวกหมายถึงควรเพิ่มเงินซื้อ ค่าลบหมายถึงควรขายออก", block_13),
    StageSpec(14, FINAL_COLUMNS[13], "Rₙ อ้างอิง", "ใช้ execution price ที่ยืนยันแล้วเท่านั้น", "Rₙ หลักเป็น execution reference; what-if quote reference แสดงแยกที่ Final", block_14),
    StageSpec(15, FINAL_COLUMNS[14], "ΔAₙ ต่อสเต็ป", "นับ incremental filled notional เพียงครั้งเดียว", "SELL เป็นเงินรับบวก BUY เป็นเงินจ่ายลบ และหัก fee เมื่อ log มีข้อมูล", block_15),
    StageSpec(16, FINAL_COLUMNS[15], "Aₙ สะสม", "สะสมเงินจริงตามเวลาเก่าไปใหม่", "pending, rejected และ unfilled ไม่ขยับยอดเงินจริง", block_16),
    StageSpec(17, FINAL_COLUMNS[16], "Eₙ ส่วนเกินสะสม", "คำนวณ Eₙ = Aₙ − Rₙ", "จะแสดงเฉพาะเมื่อ Aₙ และ Rₙ มาจาก broker-confirmed execution contract", block_17),
)

STAGE_BY_NUMBER = {stage.number: stage for stage in STAGES}


def run_stage(
    stage_number: int,
    raw: pd.DataFrame,
    previous: StageResult | None,
    context: PipelineContext,
) -> StageResult:
    """Run exactly one stage and return its accumulated immutable-by-contract result."""

    try:
        spec = STAGE_BY_NUMBER[int(stage_number)]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("stage_number must be between 1 and 17") from exc

    if spec.number == 1:
        if previous is not None:
            raise ValueError("stage 1 must start without a previous StageResult")
        accumulated = pd.DataFrame(index=raw.index)
        previous_hash = "AUTH"
    else:
        if previous is None or previous.stage_number != spec.number - 1:
            raise ValueError(f"stage {spec.number} requires completed stage {spec.number - 1}")
        if len(previous.frame) != len(raw):
            raise ValueError("previous stage row count does not match raw data")
        accumulated = previous.frame.copy()
        previous_hash = previous.output_hash

    series, diagnostics, provenance = spec.run_fn(
        raw, accumulated, float(context.fix_c)
    )
    if len(series) != len(raw):
        raise ValueError(f"stage {spec.number} returned the wrong number of rows")
    accumulated[spec.column_name] = series.to_numpy()

    input_payload = json.dumps(
        {
            "stage": spec.number,
            "raw": context.source_hash or dataframe_fingerprint(raw),
            "previous": previous_hash,
            "fix_c": float(context.fix_c),
        },
        sort_keys=True,
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
    """Delete a stage and every downstream result after an upstream rerun."""

    for key in list(results):
        if int(key) >= int(stage_number):
            del results[key]


def final_dataframe(result: StageResult) -> pd.DataFrame:
    """Return the exact 17-column newest-first export contract."""

    if result.stage_number != 17:
        raise ValueError("final dataframe requires completed stage 17")
    missing = [column for column in FINAL_COLUMNS if column not in result.frame.columns]
    if missing:
        raise ValueError(f"missing final columns: {missing}")
    return result.frame.loc[:, FINAL_COLUMNS].iloc[::-1].reset_index(drop=True)


def what_if_dataframe(result: StageResult, fix_c: float) -> pd.DataFrame:
    """Build a separate quote-path learning ledger without calling it real cash."""

    if result.stage_number < 6:
        raise ValueError("what-if dataframe requires at least stage 6")
    frame = result.frame
    price = pd.to_numeric(frame["ราคา Pₙ (USD)"], errors="coerce")
    valid_indices = list(price.index[price.notna() & np.isfinite(price) & price.gt(0)])
    reference = pd.Series(np.nan, index=frame.index, dtype=float)
    delta = pd.Series(np.nan, index=frame.index, dtype=float)
    actual = pd.Series(np.nan, index=frame.index, dtype=float)
    excess = pd.Series(np.nan, index=frame.index, dtype=float)

    if valid_indices:
        p0 = float(price.loc[valid_indices[0]])
        previous = p0
        cumulative = 0.0
        for position, index in enumerate(valid_indices):
            current = float(price.loc[index])
            step_delta = 0.0 if position == 0 else float(fix_c) * (current / previous - 1.0)
            cumulative += step_delta
            step_reference = float(fix_c) * math.log(current / p0)
            delta.loc[index] = step_delta
            actual.loc[index] = cumulative
            reference.loc[index] = step_reference
            excess.loc[index] = cumulative - step_reference
            previous = current

    time_values = (
        frame["เวลา (UTC)"]
        if "เวลา (UTC)" in frame
        else pd.Series(pd.NA, index=frame.index)
    )
    output = pd.DataFrame(
        {
            "เวลา (UTC)": time_values,
            "ราคา Pₙ (USD)": price,
            "Rₙ what-if (USD)": reference.round(2),
            "ΔAₙ what-if (USD)": delta.round(2),
            "Aₙ what-if สะสม (USD)": actual.round(2),
            "Eₙ what-if สะสม (USD)": excess.round(2),
        }
    )
    return output.loc[:, WHAT_IF_COLUMNS].iloc[::-1].reset_index(drop=True)
