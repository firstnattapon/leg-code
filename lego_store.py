"""Firestore state and Step-18 persistence for the one-new-row LEGO chain."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from typing import Any, Mapping

import numpy as np
import pandas as pd
from google.cloud import firestore
from google.oauth2 import service_account

from lego_pipeline import FINAL_COLUMNS, PipelineContext, PreviousAnchor, StageResult


@dataclass(frozen=True)
class FirestoreCollections:
    rows: str = "webull_lego_rows"
    state: str = "webull_lego_state"
    order_audit: str = "webull_lego_order_audit"


@dataclass(frozen=True)
class PersistResult:
    run_id: str
    chain_key: str
    version: int
    created: bool


class StaleAnchorError(RuntimeError):
    """The latest pointer moved after Step 0; the run must restart."""


def firestore_client(firebase_info: Mapping[str, Any]) -> firestore.Client:
    if not firebase_info:
        raise ValueError("Missing [firebase_service_account] in Streamlit secrets")
    credentials = service_account.Credentials.from_service_account_info(
        dict(firebase_info)
    )
    return firestore.Client(
        credentials=credentials,
        project=str(firebase_info["project_id"]),
    )


def strategy_config_hash(
    *,
    strategy_id: str,
    fix_c: float,
    diff: float,
    dna_code: str,
    decimal_precision: int,
) -> str:
    payload = json.dumps(
        {
            "strategy_id": str(strategy_id).strip(),
            "fix_c": float(fix_c),
            "diff": float(diff),
            "dna_sha256": hashlib.sha256(
                str(dna_code).strip().encode("utf-8")
            ).hexdigest(),
            "decimal_precision": int(decimal_precision),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_chain_key(
    *,
    environment: str,
    account_fingerprint: str,
    symbol: str,
    strategy_hash: str,
) -> str:
    payload = "\x00".join(
        (
            str(environment).strip(),
            str(account_fingerprint).strip(),
            str(symbol).strip().upper(),
            str(strategy_hash).strip(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def deterministic_run_id(
    *,
    chain_key: str,
    snapshot_hash: str,
    anchor_version: int,
) -> str:
    payload = f"{chain_key}\x00{snapshot_hash}\x00{int(anchor_version)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _snapshot_data(snapshot: Any) -> dict[str, Any]:
    if snapshot is None or not getattr(snapshot, "exists", False):
        return {}
    return dict(snapshot.to_dict() or {})


def load_previous_anchor(
    db: firestore.Client,
    collections: FirestoreCollections,
    chain_key: str,
) -> PreviousAnchor:
    """Read at most one state document and its one latest final row."""

    state_snapshot = db.collection(collections.state).document(chain_key).get()
    state = _snapshot_data(state_snapshot)
    if not state:
        return PreviousAnchor()
    row_id = str(state.get("latest_row_id", "")).strip()
    if not row_id:
        raise RuntimeError("LEGO state exists without latest_row_id")
    row_snapshot = db.collection(collections.rows).document(row_id).get()
    document = _snapshot_data(row_snapshot)
    if not document:
        raise RuntimeError("LEGO latest-row pointer references a missing document")
    row = dict(document.get("row") or {})
    metadata = dict(document.get("metadata") or {})
    try:
        dna_step = int(row["DNA step"])
        price = float(row["ราคา Pₙ (USD)"])
        p0 = float(metadata["p0"])
        actual = float(
            metadata.get("actual_cumulative_full", row["Aₙ สะสม (USD)"])
        )
        version = int(state.get("version", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("latest LEGO row is missing recurrence metadata") from exc
    if (
        dna_step < 0
        or not all(math.isfinite(value) for value in (price, p0, actual))
        or price <= 0
        or p0 <= 0
        or version <= 0
    ):
        raise RuntimeError("latest LEGO anchor contains invalid recurrence values")
    return PreviousAnchor(
        row_id=row_id,
        version=version,
        dna_step=dna_step,
        price=price,
        p0=p0,
        actual_cumulative=actual,
    )


def _json_safe(value: Any) -> Any:
    if value is pd.NA:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        numeric = float(value)
        return numeric if math.isfinite(numeric) else None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def build_final_document(
    *,
    context: PipelineContext,
    final: pd.DataFrame,
    stage_result: StageResult,
    environment: str,
    account_fingerprint: str,
    strategy_hash: str,
    snapshot_summary: Mapping[str, Any],
) -> dict[str, Any]:
    if len(final) != 1 or tuple(final.columns) != FINAL_COLUMNS:
        raise ValueError("Step 18 requires one row with the exact 17-column contract")
    row = _json_safe(final.iloc[0].to_dict())
    price = float(row["ราคา Pₙ (USD)"])
    p0 = price if context.anchor.p0 is None else float(context.anchor.p0)
    return {
        "schema_version": 1,
        "run_id": context.run_id,
        "chain_key": context.chain_key,
        "environment": str(environment),
        "account_fingerprint": str(account_fingerprint),
        "strategy_config_hash": str(strategy_hash),
        "row": row,
        "metadata": {
            "p0": p0,
            "previous_row_id": context.anchor.row_id,
            "previous_anchor_version": int(context.anchor.version),
            "source_hash": context.source_hash,
            "stage_17_input_hash": stage_result.input_hash,
            "stage_17_output_hash": stage_result.output_hash,
            "completed_at": stage_result.completed_at,
            "reference_full": float(
                stage_result.frame["Rₙ อ้างอิง (USD)"].iloc[0]
            ),
            "delta_full": float(
                stage_result.frame["ΔAₙ ต่อสเต็ป (USD)"].iloc[0]
            ),
            "actual_cumulative_full": float(
                stage_result.frame["Aₙ สะสม (USD)"].iloc[0]
            ),
            "excess_full": float(
                stage_result.frame["Eₙ ส่วนเกินสะสม (USD)"].iloc[0]
            ),
            "snapshot": _json_safe(dict(snapshot_summary)),
        },
    }


def _commit_final_row(
    transaction: Any,
    *,
    state_ref: Any,
    row_ref: Any,
    context: PipelineContext,
    document: Mapping[str, Any],
) -> PersistResult:
    existing = row_ref.get(transaction=transaction)
    if getattr(existing, "exists", False):
        existing_data = dict(existing.to_dict() or {})
        if (
            existing_data.get("run_id") != context.run_id
            or existing_data.get("chain_key") != context.chain_key
        ):
            raise RuntimeError("run_id collision with a different LEGO document")
        existing_version = int(
            dict(existing_data.get("metadata") or {}).get(
                "committed_version", context.anchor.version + 1
            )
        )
        return PersistResult(
            run_id=context.run_id,
            chain_key=context.chain_key,
            version=existing_version,
            created=False,
        )

    state_snapshot = state_ref.get(transaction=transaction)
    state = _snapshot_data(state_snapshot)
    current_version = int(state.get("version", 0)) if state else 0
    current_row_id = state.get("latest_row_id") if state else None
    if (
        current_version != int(context.anchor.version)
        or current_row_id != context.anchor.row_id
    ):
        raise StaleAnchorError(
            "latest LEGO anchor changed after Step 0; restart the run"
        )
    next_version = current_version + 1
    committed_document = dict(document)
    committed_document["metadata"] = {
        **dict(document.get("metadata") or {}),
        "committed_version": next_version,
    }
    transaction.create(row_ref, committed_document)
    transaction.set(
        state_ref,
        {
            "chain_key": context.chain_key,
            "latest_row_id": context.run_id,
            "version": next_version,
            "updated_at": committed_document["metadata"]["completed_at"],
        },
    )
    return PersistResult(
        run_id=context.run_id,
        chain_key=context.chain_key,
        version=next_version,
        created=True,
    )


def persist_final_row(
    db: firestore.Client,
    collections: FirestoreCollections,
    *,
    context: PipelineContext,
    document: Mapping[str, Any],
) -> PersistResult:
    state_ref = db.collection(collections.state).document(context.chain_key)
    row_ref = db.collection(collections.rows).document(context.run_id)
    transaction = db.transaction()

    @firestore.transactional
    def commit(transaction: Any) -> PersistResult:
        return _commit_final_row(
            transaction,
            state_ref=state_ref,
            row_ref=row_ref,
            context=context,
            document=document,
        )

    return commit(transaction)
