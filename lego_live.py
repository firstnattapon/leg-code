"""Real Step-0 snapshot orchestration without historical trade-log reads."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

import pandas as pd

from lego_pipeline import (
    PipelineContext,
    build_snapshot_frame,
    dataframe_fingerprint,
)
from lego_store import (
    FirestoreCollections,
    build_chain_key,
    deterministic_run_id,
    firestore_client,
    load_previous_anchor,
    strategy_config_hash,
)
from lego_uat import account_fingerprint, redact_payload
from manual_tools import ConnectionSettings, WebullManualClient


@dataclass(frozen=True)
class StepZeroResult:
    raw: pd.DataFrame
    context: PipelineContext
    firestore_client: Any
    safe_summary: dict[str, Any]
    account_fingerprint: str
    strategy_hash: str


def load_step_zero_snapshot(
    settings: ConnectionSettings,
    *,
    firebase_info: Mapping[str, Any],
    collections: FirestoreCollections,
    symbol: str,
    dna_code: str,
    fix_c: float,
    diff: float,
    decimal_precision: int = 5,
    strategy_id: str = "SHANNON_DEMON_DNA",
) -> StepZeroResult:
    """Read one immutable broker snapshot and one latest LEGO anchor."""

    settings.validate()
    normalized_symbol = str(symbol).strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol is required for the one-new-row snapshot")
    client = WebullManualClient(settings)
    account_list = client.get_account_list()
    balance = client.get_account_balance()
    market = client.get_position_and_price(normalized_symbol)
    db = firestore_client(firebase_info)
    fingerprint = account_fingerprint(settings.account_id)
    config_hash = strategy_config_hash(
        strategy_id=strategy_id,
        fix_c=fix_c,
        diff=diff,
        dna_code=dna_code,
        decimal_precision=decimal_precision,
    )
    chain_key = build_chain_key(
        environment=settings.environment,
        account_fingerprint=fingerprint,
        symbol=normalized_symbol,
        strategy_hash=config_hash,
    )
    anchor = load_previous_anchor(db, collections, chain_key)
    raw = build_snapshot_frame(
        snapshot_at=datetime.now(timezone.utc),
        symbol=normalized_symbol,
        price=float(market["last_price"]),
        holdings=float(market["quantity"]),
    )
    source_hash = dataframe_fingerprint(raw)
    run_id = deterministic_run_id(
        chain_key=chain_key,
        snapshot_hash=source_hash,
        anchor_version=anchor.version,
    )
    context = PipelineContext(
        fix_c=float(fix_c),
        diff=float(diff),
        dna_code=str(dna_code),
        decimal_precision=int(decimal_precision),
        source_hash=source_hash,
        run_id=run_id,
        chain_key=chain_key,
        anchor=anchor,
    )
    summary = {
        "environment": settings.environment,
        "endpoint": settings.endpoint,
        "account_fingerprint": fingerprint,
        "symbol": normalized_symbol,
        "api_reads": [
            "account_list",
            "account_balance",
            "account_positions",
            "market_snapshot",
        ],
        "old_trade_log_reads": 0,
        "snapshot_rows": 1,
        "snapshot_at": raw.loc[0, "snapshot_at"],
        "price": float(raw.loc[0, "last_price"]),
        "holdings": float(raw.loc[0, "quantity"]),
        "chain_key": chain_key,
        "run_id": run_id,
        "anchor": {
            "row_id": anchor.row_id,
            "version": anchor.version,
            "dna_step": anchor.dna_step,
            "price": anchor.price,
            "p0": anchor.p0,
            "actual_cumulative": anchor.actual_cumulative,
        },
        "account_list": redact_payload(account_list),
        "balance": redact_payload(balance),
        "positions": redact_payload(market.get("position_response")),
        "quote": redact_payload(market.get("quote_response")),
    }
    return StepZeroResult(
        raw=raw,
        context=context,
        firestore_client=db,
        safe_summary=summary,
        account_fingerprint=fingerprint,
        strategy_hash=config_hash,
    )
