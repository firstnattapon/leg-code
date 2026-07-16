"""One-new-row LEGO engine for the Webull learning dashboard.

This module replaces the old multi-row history pipeline.  A single authenticated
run reads **one** immutable Webull snapshot (account / balance / positions /
quote) and **one** recurrence anchor (the latest final row of the same chain),
then Steps 1→17 compute the 17 columns of exactly **one** new row.  Step 18 (in
:mod:`lego_state`) appends that row transactionally.

Design contract
---------------
* No Firestore ``shannon_demon_trades`` read ever feeds the pipeline.  Holdings
  come from the live Positions snapshot, price from the live Quote snapshot.
* Every column is derived from the current snapshot, the strategy configuration,
  the previous anchor, or a declared formula — never copied from a prior row.
* The decision (``action``/``side``/``reason``/``quantity``/``value``/``gap``) is
  built exactly once in :func:`build_decision`; Steps 8–13 only expose fields of
  that one object.
* Steps 14–17 use the price-path recurrence from the previous anchor only:
  ``R_n = FIX_C·ln(P_n/P_0)``, ``ΔA_n = FIX_C·(P_n/P_{n-1} − 1)``,
  ``A_n = A_{n-1} + ΔA_n``, ``E_n = A_n − R_n``.
* Financial values are computed at full precision and only rounded to 2 dp for
  presentation/export; the order quantity is rounded to ``decimal_precision``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
from typing import Any

from manual_tools import (
    DEFAULT_ORDER_DECIMAL_PRECISION,
    decode_dna,
    dna_summary,
    extract_last_price,
    extract_quantity,
)


# Bump when the row/stage/state contract changes so callers can detect a stale
# import after a Streamlit hot-reload.
ONE_ROW_SCHEMA_VERSION = 1

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

# Columns that carry money and are only rounded to 2 dp at presentation time.
FINANCIAL_COLUMNS: frozenset[str] = frozenset(
    {
        "ราคา Pₙ (USD)",
        "มูลค่าพอร์ต (USD)",
        "ส่วนต่างเป้าหมาย (USD)",
        "Rₙ อ้างอิง (USD)",
        "ΔAₙ ต่อสเต็ป (USD)",
        "Aₙ สะสม (USD)",
        "Eₙ ส่วนเกินสะสม (USD)",
    }
)

# Draft status shown while the snapshot is validated but the decision is not yet
# resolved; the final row's status is the decision status below.
STATUS_SNAPSHOT_READY = "SNAPSHOT_READY"
STATUS_PASS_DNA_ZERO = "PASS_DNA_ZERO"
STATUS_PASS_THRESHOLD = "PASS_THRESHOLD"
STATUS_READY_BUY = "READY_BUY"
STATUS_READY_SELL = "READY_SELL"

RESOLVED_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_PASS_DNA_ZERO,
        STATUS_PASS_THRESHOLD,
        STATUS_READY_BUY,
        STATUS_READY_SELL,
    }
)

# Stage number after which the status column carries the resolved decision
# status instead of the interim SNAPSHOT_READY draft value.
DECISION_STAGE = 8


class PipelineError(RuntimeError):
    """Fail-closed error raised when the one-row contract cannot be honored."""


# --------------------------------------------------------------------------- #
# 1) Data contracts
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StrategyParameters:
    """Immutable strategy configuration; a change starts a new chain."""

    fix_c: float
    diff: float = 0.0
    dna_code: str = "bypass:100"
    strategy_id: str = "shannon_demon_lego"
    decimal_precision: int = DEFAULT_ORDER_DECIMAL_PRECISION

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.fix_c)) or float(self.fix_c) <= 0:
            raise PipelineError("fix_c must be finite and greater than 0")
        if not math.isfinite(float(self.diff)) or float(self.diff) < 0:
            raise PipelineError("diff must be finite and non-negative")
        if int(self.decimal_precision) < 0:
            raise PipelineError("decimal_precision cannot be negative")
        # Validate the DNA code eagerly so a bad code fails at Step 0, not Step 5.
        _ = self.decoded_dna()

    def decoded_dna(self):
        """Decode the DNA once; raises PipelineError on an invalid code."""

        try:
            return decode_dna(self.dna_code)
        except Exception as exc:  # normalize to the fail-closed contract
            raise PipelineError(f"invalid DNA_CODE: {exc}") from exc

    def dna_hash(self) -> str:
        return hashlib.sha256(self.decoded_dna().tobytes()).hexdigest()

    def config_hash(self) -> str:
        """Stable hash of everything that defines a chain's calculation."""

        payload = json.dumps(
            {
                "strategy_id": self.strategy_id,
                "fix_c": float(self.fix_c),
                "diff": float(self.diff),
                "decimal_precision": int(self.decimal_precision),
                "dna_hash": self.dna_hash(),
            },
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CurrentSnapshot:
    """One immutable Webull read; secrets/raw responses stay out of this object."""

    environment: str
    account_fingerprint: str
    symbol: str
    price: float
    holdings: float
    captured_at: str

    def __post_init__(self) -> None:
        if not str(self.symbol).strip():
            raise PipelineError("snapshot symbol is required")
        if not math.isfinite(float(self.price)) or float(self.price) <= 0:
            raise PipelineError("snapshot price must be finite and greater than 0")
        if not math.isfinite(float(self.holdings)) or float(self.holdings) < 0:
            raise PipelineError("snapshot holdings must be finite and non-negative")

    def fingerprint(self) -> str:
        payload = "\x00".join(
            (
                self.environment,
                self.account_fingerprint,
                self.symbol.strip().upper(),
                repr(float(self.price)),
                repr(float(self.holdings)),
                self.captured_at,
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PreviousAnchor:
    """The latest final row of the same chain, or a genesis (no prior row)."""

    exists: bool
    version: int = 0
    row_id: str | None = None
    dna_step: int | None = None
    p0: float | None = None
    prev_price: float | None = None
    prev_actual: float | None = None

    @classmethod
    def genesis(cls) -> "PreviousAnchor":
        return cls(exists=False, version=0)


@dataclass(frozen=True)
class RunContext:
    """Everything one authenticated run needs; created once at Step 0."""

    run_id: str
    chain_key: str
    snapshot: CurrentSnapshot
    anchor: PreviousAnchor
    params: StrategyParameters


@dataclass(frozen=True)
class RebalanceDecision:
    """The single decision object exposed by Steps 8–13."""

    status: str
    action: str
    side: str | None
    reason: str
    quantity: float
    value_now: float
    gap: float
    dna_signal: int


@dataclass(frozen=True)
class StageResult:
    """One computed column with provenance for a single new row."""

    stage_number: int
    column_name: str
    value: Any
    diagnostics: tuple[str, ...]
    provenance: dict[str, Any]
    input_hash: str
    output_hash: str


@dataclass(frozen=True)
class ComputedRow:
    """The fully resolved 17-column new row plus recurrence metadata."""

    columns: dict[str, Any]
    metadata: dict[str, Any]
    decision: RebalanceDecision
    stages: tuple[StageResult, ...] = field(default_factory=tuple)


# --------------------------------------------------------------------------- #
# 2) Chain identity and run identity
# --------------------------------------------------------------------------- #
def compute_chain_key(
    environment: str,
    account_fingerprint: str,
    symbol: str,
    params: StrategyParameters,
) -> str:
    """Separate namespaces by environment, account, symbol, and config."""

    payload = "\x00".join(
        (
            str(environment).strip(),
            str(account_fingerprint).strip(),
            str(symbol).strip().upper(),
            params.config_hash(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def compute_run_id(chain_key: str, anchor: PreviousAnchor, snapshot: CurrentSnapshot) -> str:
    """Deterministic per (chain, anchor version, captured snapshot).

    Pressing Step 18 twice in the same run reuses the same snapshot and anchor,
    so the run_id is identical and the append is idempotent.  A fresh Connect &
    Load captures a new snapshot and therefore a new run_id.
    """

    payload = "\x00".join(
        (chain_key, str(int(anchor.version)), snapshot.fingerprint())
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


# --------------------------------------------------------------------------- #
# 3) Snapshot / anchor construction (Step 0 helpers)
# --------------------------------------------------------------------------- #
def account_fingerprint(account_id: str) -> str:
    normalized = str(account_id).strip()
    if not normalized:
        return "missing"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def build_snapshot(
    *,
    environment: str,
    account_id: str,
    symbol: str,
    positions_response: Any,
    quote_response: Any,
    captured_at: str | None = None,
) -> CurrentSnapshot:
    """Turn live Positions + Quote reads into one immutable snapshot.

    Holdings are the broker-observed position for ``symbol`` (0 when absent) and
    the price is the live quote.  A non-positive quote fails closed.
    """

    normalized_symbol = str(symbol).strip().upper()
    if not normalized_symbol:
        raise PipelineError("a symbol is required to build the snapshot")
    holdings = extract_quantity(positions_response, normalized_symbol)
    price = extract_last_price(quote_response, normalized_symbol)
    if not math.isfinite(price) or price <= 0:
        raise PipelineError(
            f"no positive live quote for {normalized_symbol}; cannot build a row"
        )
    return CurrentSnapshot(
        environment=str(environment),
        account_fingerprint=account_fingerprint(account_id),
        symbol=normalized_symbol,
        price=float(price),
        holdings=float(holdings),
        captured_at=captured_at or datetime.now(timezone.utc).isoformat(),
    )


def anchor_from_state(state: dict[str, Any] | None) -> PreviousAnchor:
    """Build the recurrence anchor from a ``webull_lego_state`` document."""

    if not state:
        return PreviousAnchor.genesis()
    try:
        return PreviousAnchor(
            exists=True,
            version=int(state["version"]),
            row_id=str(state.get("latest_row_id") or "") or None,
            dna_step=int(state["dna_step"]),
            p0=float(state["p0"]),
            prev_price=float(state["prev_price"]),
            prev_actual=float(state["prev_actual"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise PipelineError(f"corrupt chain state document: {exc}") from exc


# --------------------------------------------------------------------------- #
# 4) Decision + recurrence
# --------------------------------------------------------------------------- #
def dna_step_for(anchor: PreviousAnchor) -> int:
    """Next DNA step: previous + 1, or 0 for the genesis row."""

    if not anchor.exists or anchor.dna_step is None:
        return 0
    return int(anchor.dna_step) + 1


def dna_signal_for(params: StrategyParameters, dna_step: int) -> int:
    """Deterministic DNA signal for ``dna_step``; fail-closed when exhausted."""

    dna = params.decoded_dna()
    if dna_step < 0 or dna_step >= len(dna):
        raise PipelineError(
            f"DNA exhausted: step {dna_step} outside decoded length {len(dna)}"
        )
    return int(dna[dna_step])


def build_decision(
    *,
    holdings: float,
    price: float,
    dna_signal: int,
    params: StrategyParameters,
) -> RebalanceDecision:
    """Build the one decision object shared by Steps 8–13.

    ``gap = FIX_C − holdings·price``.  DNA 0 forces PASS_DNA_ZERO; within the
    ``diff`` band is PASS_THRESHOLD; otherwise BUY when the gap is positive and
    SELL when negative, with ``quantity = round(|gap|/price, decimal_precision)``.
    """

    value_now = float(holdings) * float(price)
    gap = float(params.fix_c) - value_now
    if int(dna_signal) == 0:
        return RebalanceDecision(
            status=STATUS_PASS_DNA_ZERO,
            action="PASS",
            side=None,
            reason=STATUS_PASS_DNA_ZERO,
            quantity=0.0,
            value_now=value_now,
            gap=gap,
            dna_signal=0,
        )
    if abs(gap) <= float(params.diff):
        return RebalanceDecision(
            status=STATUS_PASS_THRESHOLD,
            action="PASS",
            side=None,
            reason=STATUS_PASS_THRESHOLD,
            quantity=0.0,
            value_now=value_now,
            gap=gap,
            dna_signal=int(dna_signal),
        )
    quantity = round(abs(gap) / float(price), int(params.decimal_precision))
    if gap > 0:
        return RebalanceDecision(
            status=STATUS_READY_BUY,
            action="BUY",
            side="BUY",
            reason=STATUS_READY_BUY,
            quantity=float(quantity),
            value_now=value_now,
            gap=gap,
            dna_signal=int(dna_signal),
        )
    return RebalanceDecision(
        status=STATUS_READY_SELL,
        action="SELL",
        side="SELL",
        reason=STATUS_READY_SELL,
        quantity=float(quantity),
        value_now=value_now,
        gap=gap,
        dna_signal=int(dna_signal),
    )


@dataclass(frozen=True)
class Recurrence:
    p0: float
    reference: float
    delta_actual: float
    actual_cumulative: float
    excess: float


def compute_recurrence(price: float, anchor: PreviousAnchor, fix_c: float) -> Recurrence:
    """Price-path recurrence from the previous anchor only.

    First row: ``P0 = P_n`` and ``R0 = ΔA0 = A0 = E0 = 0``.  Otherwise use the
    stored baseline ``P0``, previous price ``P_{n-1}`` and previous cumulative
    ``A_{n-1}``.
    """

    price = float(price)
    fix_c = float(fix_c)
    if price <= 0 or not math.isfinite(price):
        raise PipelineError("recurrence price must be finite and greater than 0")
    if not anchor.exists:
        return Recurrence(p0=price, reference=0.0, delta_actual=0.0, actual_cumulative=0.0, excess=0.0)
    p0 = float(anchor.p0)
    prev_price = float(anchor.prev_price)
    prev_actual = float(anchor.prev_actual)
    if p0 <= 0 or prev_price <= 0:
        raise PipelineError("anchor baseline/previous price must be greater than 0")
    reference = fix_c * math.log(price / p0)
    delta_actual = fix_c * (price / prev_price - 1.0)
    actual_cumulative = prev_actual + delta_actual
    excess = actual_cumulative - reference
    return Recurrence(
        p0=p0,
        reference=reference,
        delta_actual=delta_actual,
        actual_cumulative=actual_cumulative,
        excess=excess,
    )


# --------------------------------------------------------------------------- #
# 5) Row computation + stage decomposition
# --------------------------------------------------------------------------- #
def compute_row(ctx: RunContext) -> ComputedRow:
    """Compute the single new row for ``ctx`` at full precision."""

    snapshot = ctx.snapshot
    params = ctx.params
    dna_step = dna_step_for(ctx.anchor)
    dna_signal = dna_signal_for(params, dna_step)
    decision = build_decision(
        holdings=snapshot.holdings,
        price=snapshot.price,
        dna_signal=dna_signal,
        params=params,
    )
    recurrence = compute_recurrence(snapshot.price, ctx.anchor, params.fix_c)

    columns: dict[str, Any] = {
        "เวลา (UTC)": snapshot.captured_at,
        "สินทรัพย์": snapshot.symbol,
        "สถานะ": decision.status,
        "DNA step": dna_step,
        "DNA signal": dna_signal,
        "ราคา Pₙ (USD)": snapshot.price,
        "จำนวนถือครอง (หุ้น)": snapshot.holdings,
        "คำสั่ง": decision.action,
        "ฝั่ง": decision.side,
        "เหตุผล": decision.reason,
        "จำนวนสั่ง (หุ้น)": decision.quantity,
        "มูลค่าพอร์ต (USD)": decision.value_now,
        "ส่วนต่างเป้าหมาย (USD)": decision.gap,
        "Rₙ อ้างอิง (USD)": recurrence.reference,
        "ΔAₙ ต่อสเต็ป (USD)": recurrence.delta_actual,
        "Aₙ สะสม (USD)": recurrence.actual_cumulative,
        "Eₙ ส่วนเกินสะสม (USD)": recurrence.excess,
    }
    metadata = {
        "chain_key": ctx.chain_key,
        "run_id": ctx.run_id,
        "dna_step": dna_step,
        "p0": recurrence.p0,
        "prev_price": snapshot.price,
        "prev_actual": recurrence.actual_cumulative,
        "anchor_row_id": ctx.anchor.row_id,
        "anchor_version": int(ctx.anchor.version),
        "anchor_exists": bool(ctx.anchor.exists),
        "environment": snapshot.environment,
        "account_fingerprint": snapshot.account_fingerprint,
        "symbol": snapshot.symbol,
        "strategy_config_hash": params.config_hash(),
        "snapshot_fingerprint": snapshot.fingerprint(),
    }
    stages = _stage_results(ctx, columns, decision, dna_step, dna_signal)
    return ComputedRow(columns=columns, metadata=metadata, decision=decision, stages=stages)


def _hash_value(*parts: Any) -> str:
    payload = json.dumps(parts, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# Per-stage diagnostics/provenance descriptions.  Steps 1–7 read the snapshot,
# Step 8 builds the decision, Steps 9–13 expose it, Steps 14–17 apply recurrence.
_STAGE_TITLES: dict[int, str] = {
    1: "เวลา UTC ของ snapshot",
    2: "สินทรัพย์จาก snapshot",
    3: "สถานะ snapshot/decision",
    4: "DNA step = ก่อนหน้า+1",
    5: "DNA signal จาก DNA_CODE",
    6: "ราคา Pₙ จาก live quote",
    7: "holdings จาก live positions",
    8: "สร้าง decision object",
    9: "ฝั่งจาก decision",
    10: "เหตุผลจาก decision",
    11: "จำนวนสั่งจาก decision",
    12: "มูลค่าพอร์ต = holdings×price",
    13: "ส่วนต่าง = FIX_C − มูลค่าพอร์ต",
    14: "Rₙ = FIX_C·ln(Pₙ/P₀)",
    15: "ΔAₙ = FIX_C·(Pₙ/Pₙ₋₁−1)",
    16: "Aₙ = Aₙ₋₁ + ΔAₙ",
    17: "Eₙ = Aₙ − Rₙ",
}


def stage_title(stage_number: int) -> str:
    return _STAGE_TITLES.get(int(stage_number), FINAL_COLUMNS[int(stage_number) - 1])


def _stage_results(
    ctx: RunContext,
    columns: dict[str, Any],
    decision: RebalanceDecision,
    dna_step: int,
    dna_signal: int,
) -> tuple[StageResult, ...]:
    """Decompose the resolved row into 17 chained, fingerprinted stage results."""

    results: list[StageResult] = []
    previous_hash = ctx.snapshot.fingerprint()
    provenance_by_stage: dict[int, dict[str, Any]] = {
        1: {"source": "snapshot.captured_at"},
        2: {"source": "snapshot.symbol"},
        3: {"draft": STATUS_SNAPSHOT_READY, "resolved": decision.status},
        4: {"rule": "previous_dna_step + 1 or 0", "anchor_version": int(ctx.anchor.version)},
        5: {"dna_code": ctx.params.dna_code, "dna_step": dna_step},
        6: {"source": "live quote"},
        7: {"source": "live positions"},
        8: {"formula": "gap = FIX_C - holdings*price", "decision": decision.status},
        9: {"source": "decision.side"},
        10: {"source": "decision.reason"},
        11: {"rule": f"round(|gap|/price, {ctx.params.decimal_precision})"},
        12: {"formula": "holdings * price"},
        13: {"formula": "FIX_C - value_now", "fix_c": float(ctx.params.fix_c)},
        14: {"formula": "FIX_C*ln(Pn/P0)", "anchor_exists": bool(ctx.anchor.exists)},
        15: {"formula": "FIX_C*(Pn/Pn_1 - 1)"},
        16: {"formula": "A_(n-1) + dA_n"},
        17: {"formula": "A_n - R_n"},
    }
    for stage_number in range(1, 18):
        column_name = FINAL_COLUMNS[stage_number - 1]
        value = columns[column_name]
        # Status column shows the interim draft value until the decision stage.
        if column_name == "สถานะ" and stage_number < DECISION_STAGE:
            value = STATUS_SNAPSHOT_READY
        diagnostics = (f"{stage_title(stage_number)} · 1 แถวใหม่",)
        provenance = dict(provenance_by_stage.get(stage_number, {}))
        input_hash = _hash_value(stage_number, previous_hash, ctx.chain_key)
        output_hash = _hash_value(stage_number, column_name, value)
        results.append(
            StageResult(
                stage_number=stage_number,
                column_name=column_name,
                value=value,
                diagnostics=diagnostics,
                provenance=provenance,
                input_hash=input_hash,
                output_hash=output_hash,
            )
        )
        previous_hash = output_hash
    return tuple(results)


def draft_status_for_stage(computed: ComputedRow, revealed_through: int) -> str:
    """Status shown in the Manual UI after revealing ``revealed_through`` steps."""

    if int(revealed_through) < DECISION_STAGE:
        return STATUS_SNAPSHOT_READY
    return computed.decision.status


# --------------------------------------------------------------------------- #
# 6) Presentation / export
# --------------------------------------------------------------------------- #
def present_value(column_name: str, value: Any) -> Any:
    """Round financial columns to 2 dp for display/export only."""

    if value is None:
        return None
    if column_name in FINANCIAL_COLUMNS:
        try:
            return round(float(value), 2)
        except (TypeError, ValueError):
            return value
    return value


def present_row(columns: dict[str, Any]) -> dict[str, Any]:
    """Return the 17-column row rounded for presentation, in contract order."""

    return {name: present_value(name, columns.get(name)) for name in FINAL_COLUMNS}


def validate_row_columns(columns: dict[str, Any]) -> None:
    """Fail closed unless the row has exactly the 17 contract columns in order."""

    keys = tuple(columns.keys())
    if keys != FINAL_COLUMNS:
        missing = [name for name in FINAL_COLUMNS if name not in columns]
        extra = [name for name in keys if name not in FINAL_COLUMNS]
        raise PipelineError(
            "final row must have exactly the 17 contract columns in order; "
            f"missing={missing} extra={extra}"
        )


def build_final_document(computed: ComputedRow, ctx: RunContext) -> dict[str, Any]:
    """Assemble the immutable ``webull_lego_rows`` document for one run."""

    validate_row_columns(computed.columns)
    stage_fingerprints = {
        str(stage.stage_number): stage.output_hash for stage in computed.stages
    }
    return {
        "run_id": ctx.run_id,
        "chain_key": ctx.chain_key,
        "anchor_version": int(ctx.anchor.version),
        "version": int(ctx.anchor.version) + 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "columns_full_precision": dict(computed.columns),
        "columns_presented": present_row(computed.columns),
        "metadata": dict(computed.metadata),
        "provenance": {
            "stage_fingerprints": stage_fingerprints,
            "snapshot_fingerprint": ctx.snapshot.fingerprint(),
            "strategy_config_hash": ctx.params.config_hash(),
            "dna_summary": _safe_dna_summary(ctx.params.dna_code),
        },
    }


def _safe_dna_summary(dna_code: str) -> dict[str, Any]:
    try:
        summary = dna_summary(dna_code)
        # The full decoded bit list is large and not needed downstream.
        summary.pop("output", None)
        return summary
    except Exception as exc:  # never let a summary break persistence
        return {"error": str(exc)}
