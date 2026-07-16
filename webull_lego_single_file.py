"""Webull LEGO 0→18 — one immutable snapshot and one new calculated row.

Quick Start (PowerShell)
------------------------
    python -m pip install pandas numpy google-cloud-firestore google-auth webull-openapi-python-sdk
    $env:WEBULL_ACCOUNT_ID="..."
    $env:WEBULL_APP_KEY="..."
    $env:WEBULL_APP_SECRET="..."
    $env:GOOGLE_APPLICATION_CREDENTIALS="C:\\safe\\firebase.json"
    python webull_lego_single_file.py --environment "Test (UAT)" --symbol AAPL --dna-code "bypass:100"

This standalone file has no order mutation methods.  Step 0 reads Account,
Balance, Positions, Quote and only the latest finalized LEGO anchor.  Steps
1→17 calculate one row; Step 18 validates/exports it.  Use ``--persist`` to
append the row transactionally to Firestore.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd


ENDPOINTS = {
    "Test (UAT)": "th-api.uat.webullbroker.com",
    "Production": "api.webull.co.th",
}
FINAL_COLUMNS = (
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


@dataclass(frozen=True)
class DnaSpec:
    length: int
    mutation_rate: float
    dna_seed: int
    mutation_seeds: tuple[int, ...]


@dataclass(frozen=True)
class PreviousAnchor:
    row_id: str | None = None
    version: int = 0
    dna_step: int | None = None
    price: float | None = None
    p0: float | None = None
    actual_cumulative: float = 0.0


@dataclass(frozen=True)
class WebullSettings:
    environment: str
    account_id: str = field(repr=False)
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    region: str = "th"

    @property
    def endpoint(self) -> str:
        if self.environment not in ENDPOINTS:
            raise ValueError(f"environment must be one of: {', '.join(ENDPOINTS)}")
        return ENDPOINTS[self.environment]

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("WEBULL_ACCOUNT_ID", self.account_id),
                ("WEBULL_APP_KEY", self.app_key),
                ("WEBULL_APP_SECRET", self.app_secret),
            )
            if not str(value).strip()
        ]
        if missing:
            raise ValueError(f"Missing credential: {', '.join(missing)}")
        _ = self.endpoint


@dataclass
class LiveInputs:
    raw: pd.DataFrame
    anchor: PreviousAnchor
    firestore_client: Any
    chain_key: str
    run_id: str
    strategy_hash: str
    safe_summary: dict[str, Any]


@dataclass
class AllInResult:
    final: pd.DataFrame
    what_if: pd.DataFrame
    accumulated: dict[int, pd.DataFrame]
    diagnostics: dict[int, list[str]]


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
        raise ValueError("DNA must encode length, rate, and seed")
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


def decode_dna(encoded: str) -> tuple[np.ndarray, dict[str, Any]]:
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


def _response_json(response: Any) -> Any:
    status = getattr(response, "status_code", None)
    if status is None:
        return response
    if not 200 <= int(status) < 300:
        error = RuntimeError(f"Webull HTTP {status}")
        setattr(error, "status_code", int(status))
        raise error
    return response.json()


def _read_with_retry(label: str, call: Callable[[], Any], attempts: int = 3) -> Any:
    for attempt in range(attempts):
        try:
            return _response_json(call())
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            transient = status is None or status in (417, 429) or (
                status is not None and status >= 500
            )
            if not transient or attempt == attempts - 1:
                raise RuntimeError(f"{label} failed ({exc.__class__.__name__})") from exc
            time.sleep(0.25 * (2**attempt))
    raise AssertionError("unreachable")


class WebullReadOnlyClient:
    def __init__(self, settings: WebullSettings):
        settings.validate()
        try:
            from webull.core.client import ApiClient
            from webull.data.data_client import DataClient
            from webull.trade.trade_client import TradeClient
        except ImportError as exc:
            raise RuntimeError("Install webull-openapi-python-sdk") from exc
        api = ApiClient(
            settings.app_key.strip(),
            settings.app_secret.strip(),
            settings.region,
        )
        api.add_endpoint(settings.region, settings.endpoint)
        self.settings = settings
        self.data = DataClient(api)
        self.trade = TradeClient(api)

    def account_list(self) -> Any:
        return _read_with_retry("account list", self.trade.account_v2.get_account_list)

    def balance(self) -> Any:
        return _read_with_retry(
            "account balance",
            lambda: self.trade.account_v2.get_account_balance(self.settings.account_id),
        )

    def positions(self) -> Any:
        return _read_with_retry(
            "account positions",
            lambda: self.trade.account_v2.get_account_position(self.settings.account_id),
        )

    def quote(self, symbol: str) -> Any:
        normalized = symbol.strip().upper()
        return _read_with_retry(
            "market snapshot",
            lambda: self.data.market_data.get_snapshot(
                normalized,
                "US_STOCK",
                extend_hour_required=False,
                overnight_required=False,
            ),
        )


def _iter_dicts(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _iter_dicts(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_dicts(item)


def _first_value(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = mapping.get(name)
        if value not in (None, ""):
            return value
    return None


def extract_quantity(response: Any, symbol: str) -> float:
    normalized = symbol.strip().upper()
    for node in _iter_dicts(response):
        node_symbol = _first_value(node, "symbol", "ticker", "instrument_id")
        if node_symbol and str(node_symbol).upper() != normalized:
            continue
        value = _first_value(
            node,
            "quantity",
            "position",
            "position_qty",
            "positionQty",
            "total_quantity",
            "totalQuantity",
        )
        if value is not None:
            return float(value)
    return 0.0


def extract_last_price(response: Any, symbol: str) -> float:
    normalized = symbol.strip().upper()
    for node in _iter_dicts(response):
        node_symbol = _first_value(node, "symbol", "ticker")
        if node_symbol and str(node_symbol).upper() != normalized:
            continue
        value = _first_value(node, "last_price", "lastPrice", "last", "price", "close")
        if value is not None:
            price = float(value)
            if math.isfinite(price) and price > 0:
                return price
    raise ValueError(f"positive quote not found for {normalized}")


def _redact(value: Any) -> Any:
    sensitive = {
        "account",
        "accountid",
        "accountno",
        "accountnumber",
        "appkey",
        "appsecret",
        "token",
        "accesstoken",
        "refreshtoken",
        "secret",
        "secretkey",
    }
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            normalized = "".join(char for char in str(key).lower() if char.isalnum())
            output[str(key)] = (
                "[REDACTED]"
                if normalized in sensitive
                or normalized.endswith(
                    (
                        "accountid",
                        "accountno",
                        "accountnumber",
                        "appkey",
                        "appsecret",
                        "accesstoken",
                        "refreshtoken",
                        "secretkey",
                    )
                )
                else _redact(item)
            )
        return output
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _firestore_client(firebase_info: dict[str, Any] | None = None):
    try:
        from google.cloud import firestore
        from google.oauth2 import service_account
    except ImportError as exc:
        raise RuntimeError("Install google-cloud-firestore and google-auth") from exc
    if firebase_info:
        credentials = service_account.Credentials.from_service_account_info(
            firebase_info
        )
        return firestore.Client(
            credentials=credentials,
            project=firebase_info["project_id"],
        )
    return firestore.Client()


def _strategy_hash(fix_c: float, diff: float, dna_code: str, precision: int) -> str:
    payload = json.dumps(
        {
            "fix_c": float(fix_c),
            "diff": float(diff),
            "dna": hashlib.sha256(dna_code.strip().encode("utf-8")).hexdigest(),
            "precision": int(precision),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _chain_key(settings: WebullSettings, symbol: str, strategy_hash: str) -> str:
    account_hash = hashlib.sha256(settings.account_id.encode("utf-8")).hexdigest()[:12]
    payload = "\x00".join(
        (settings.environment, account_hash, symbol.upper(), strategy_hash)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def load_latest_anchor(
    db: Any,
    *,
    rows_collection: str,
    state_collection: str,
    chain_key: str,
) -> PreviousAnchor:
    state_snapshot = db.collection(state_collection).document(chain_key).get()
    if not getattr(state_snapshot, "exists", False):
        return PreviousAnchor()
    state = dict(state_snapshot.to_dict() or {})
    row_id = str(state.get("latest_row_id", ""))
    row_snapshot = db.collection(rows_collection).document(row_id).get()
    if not getattr(row_snapshot, "exists", False):
        raise RuntimeError("latest LEGO row is missing")
    document = dict(row_snapshot.to_dict() or {})
    row = dict(document.get("row") or {})
    metadata = dict(document.get("metadata") or {})
    return PreviousAnchor(
        row_id=row_id,
        version=int(state["version"]),
        dna_step=int(row["DNA step"]),
        price=float(row["ราคา Pₙ (USD)"]),
        p0=float(metadata["p0"]),
        actual_cumulative=float(
            metadata.get("actual_cumulative_full", row["Aₙ สะสม (USD)"])
        ),
    )


def load_live_inputs(
    settings: WebullSettings,
    *,
    firebase_info: dict[str, Any] | None,
    rows_collection: str = "webull_lego_rows",
    state_collection: str = "webull_lego_state",
    symbol: str,
    fix_c: float,
    diff: float,
    dna_code: str,
    decimal_precision: int = 5,
) -> LiveInputs:
    settings.validate()
    normalized = symbol.strip().upper()
    if not normalized:
        raise ValueError("symbol is required")
    client = WebullReadOnlyClient(settings)
    account_list = client.account_list()
    balance = client.balance()
    positions = client.positions()
    quote = client.quote(normalized)
    quantity = extract_quantity(positions, normalized)
    price = extract_last_price(quote, normalized)
    db = _firestore_client(firebase_info)
    strategy_hash = _strategy_hash(fix_c, diff, dna_code, decimal_precision)
    chain_key = _chain_key(settings, normalized, strategy_hash)
    anchor = load_latest_anchor(
        db,
        rows_collection=rows_collection,
        state_collection=state_collection,
        chain_key=chain_key,
    )
    snapshot_at = datetime.now(timezone.utc).isoformat()
    raw = pd.DataFrame(
        [
            {
                "snapshot_at": snapshot_at,
                "symbol": normalized,
                "last_price": price,
                "quantity": quantity,
            }
        ]
    )
    snapshot_hash = hashlib.sha256(
        raw.to_json(orient="split", date_format="iso").encode("utf-8")
    ).hexdigest()
    run_id = hashlib.sha256(
        f"{chain_key}\x00{snapshot_hash}\x00{anchor.version}".encode("utf-8")
    ).hexdigest()[:32]
    account_hash = hashlib.sha256(settings.account_id.encode("utf-8")).hexdigest()[:12]
    summary = {
        "environment": settings.environment,
        "endpoint": settings.endpoint,
        "account_fingerprint": account_hash,
        "api_reads": [
            "account_list",
            "account_balance",
            "account_positions",
            "market_snapshot",
        ],
        "old_trade_log_reads": 0,
        "snapshot_rows": 1,
        "symbol": normalized,
        "price": price,
        "holdings": quantity,
        "fix_c": float(fix_c),
        "diff": float(diff),
        "decimal_precision": int(decimal_precision),
        "run_id": run_id,
        "chain_key": chain_key,
        "anchor": asdict(anchor),
        "account_list": _redact(account_list),
        "balance": _redact(balance),
        "positions": _redact(positions),
        "quote": _redact(quote),
    }
    return LiveInputs(
        raw=raw,
        anchor=anchor,
        firestore_client=db,
        chain_key=chain_key,
        run_id=run_id,
        strategy_hash=strategy_hash,
        safe_summary=summary,
    )


def run_dataframe_chain(
    raw_input: pd.DataFrame,
    fix_c: float,
    dna_code: str = "",
    diff: float = 0.0,
    anchor: PreviousAnchor = PreviousAnchor(),
    decimal_precision: int = 5,
) -> AllInResult:
    if len(raw_input) != 1:
        raise ValueError("one-new-row chain requires exactly one snapshot row")
    raw = raw_input.reset_index(drop=True)
    timestamp = pd.to_datetime(raw.loc[0, "snapshot_at"], utc=True)
    symbol = str(raw.loc[0, "symbol"]).strip().upper()
    price = float(raw.loc[0, "last_price"])
    holdings = float(raw.loc[0, "quantity"])
    if not symbol or price <= 0 or holdings < 0 or fix_c <= 0 or diff < 0:
        raise ValueError("invalid snapshot or strategy parameters")
    step = 0 if anchor.dna_step is None else int(anchor.dna_step) + 1
    dna, dna_metadata = decode_dna(dna_code)
    if not 0 <= step < len(dna):
        raise ValueError("DNA step is outside the decoded sequence")
    signal = int(dna[step])
    value = holdings * price
    gap = float(fix_c) - value
    if signal == 0:
        action, side, reason, status = "PASS", None, "DNA_ZERO", "PASS_DNA_ZERO"
    elif abs(gap) <= float(diff):
        action, side, reason, status = (
            "PASS",
            None,
            "WITHIN_THRESHOLD",
            "PASS_THRESHOLD",
        )
    elif gap > 0:
        action, side, reason, status = "BUY", "BUY", "BELOW_TARGET", "READY_BUY"
    else:
        action, side, reason, status = "SELL", "SELL", "ABOVE_TARGET", "READY_SELL"
    quantity = (
        0.0
        if action == "PASS"
        else round(abs(gap) / price, int(decimal_precision))
    )
    if action != "PASS" and quantity <= 0:
        raise ValueError("calculated order quantity became zero")
    p0 = price if anchor.p0 is None else float(anchor.p0)
    reference_full = float(fix_c) * math.log(price / p0)
    delta_full = (
        0.0
        if anchor.price is None
        else float(fix_c) * (price / float(anchor.price) - 1.0)
    )
    actual_full = float(anchor.actual_cumulative) + delta_full
    excess_full = actual_full - reference_full
    row = {
        "เวลา (UTC)": timestamp.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        ),
        "สินทรัพย์": symbol,
        "สถานะ": status,
        "DNA step": step,
        "DNA signal": signal,
        "ราคา Pₙ (USD)": price,
        "จำนวนถือครอง (หุ้น)": holdings,
        "คำสั่ง": action,
        "ฝั่ง": side,
        "เหตุผล": reason,
        "จำนวนสั่ง (หุ้น)": quantity,
        "มูลค่าพอร์ต (USD)": round(value, 2),
        "ส่วนต่างเป้าหมาย (USD)": round(gap, 2),
        "Rₙ อ้างอิง (USD)": round(reference_full, 2),
        "ΔAₙ ต่อสเต็ป (USD)": round(delta_full, 2),
        "Aₙ สะสม (USD)": round(actual_full, 2),
        "Eₙ ส่วนเกินสะสม (USD)": round(excess_full, 2),
    }
    final = pd.DataFrame([row], columns=FINAL_COLUMNS)
    accumulated = {
        step_number: final.loc[:, FINAL_COLUMNS[:step_number]].copy()
        for step_number in range(1, 18)
    }
    diagnostics = {
        0: ["immutable Webull snapshot", "old_trade_log_reads=0"],
        4: [f"DNA step={step}"],
        5: [f"DNA signal={signal}", f"decoder={dna_metadata['mode']}"],
        8: [f"gap={gap}", f"status={status}"],
        14: [f"P0={p0}"],
        18: ["exactly one final row"],
    }
    what_if = pd.DataFrame(
        [
            {
                "เวลา (UTC)": row["เวลา (UTC)"],
                "ราคา Pₙ (USD)": price,
                "Rₙ what-if (USD)": row["Rₙ อ้างอิง (USD)"],
                "ΔAₙ what-if (USD)": row["ΔAₙ ต่อสเต็ป (USD)"],
                "Aₙ what-if สะสม (USD)": row["Aₙ สะสม (USD)"],
                "Eₙ what-if สะสม (USD)": row["Eₙ ส่วนเกินสะสม (USD)"],
            }
        ]
    )
    return AllInResult(final, what_if, accumulated, diagnostics)


def persist_final(
    live: LiveInputs,
    result: AllInResult,
    *,
    rows_collection: str,
    state_collection: str,
) -> dict[str, Any]:
    from google.cloud import firestore

    db = live.firestore_client
    row_ref = db.collection(rows_collection).document(live.run_id)
    state_ref = db.collection(state_collection).document(live.chain_key)
    transaction = db.transaction()
    row = result.final.iloc[0].to_dict()
    current_price = float(result.final.loc[0, "ราคา Pₙ (USD)"])
    p0 = current_price if live.anchor.p0 is None else float(live.anchor.p0)
    delta_full = (
        0.0
        if live.anchor.price is None
        else float(live.safe_summary["fix_c"])
        * (current_price / float(live.anchor.price) - 1.0)
    )
    actual_full = float(live.anchor.actual_cumulative) + delta_full
    reference_full = float(live.safe_summary["fix_c"]) * math.log(
        current_price / p0
    )
    metadata = {
        "p0": p0,
        "reference_full": reference_full,
        "delta_full": delta_full,
        "actual_cumulative_full": actual_full,
        "excess_full": actual_full - reference_full,
        "previous_row_id": live.anchor.row_id,
        "previous_anchor_version": live.anchor.version,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }

    @firestore.transactional
    def commit(transaction):
        existing = row_ref.get(transaction=transaction)
        if existing.exists:
            return {"created": False, "version": live.anchor.version + 1}
        state_snapshot = state_ref.get(transaction=transaction)
        state = dict(state_snapshot.to_dict() or {}) if state_snapshot.exists else {}
        if (
            int(state.get("version", 0)) != live.anchor.version
            or state.get("latest_row_id") != live.anchor.row_id
        ):
            raise RuntimeError("stale LEGO anchor; rerun Step 0")
        version = live.anchor.version + 1
        transaction.create(
            row_ref,
            {
                "schema_version": 1,
                "run_id": live.run_id,
                "chain_key": live.chain_key,
                "strategy_config_hash": live.strategy_hash,
                "row": row,
                "metadata": {**metadata, "committed_version": version},
            },
        )
        transaction.set(
            state_ref,
            {
                "chain_key": live.chain_key,
                "latest_row_id": live.run_id,
                "version": version,
                "updated_at": metadata["completed_at"],
            },
        )
        return {"created": True, "version": version}

    return commit(transaction)


def run_live_all_in(
    settings: WebullSettings,
    *,
    firebase_info: dict[str, Any] | None,
    rows_collection: str,
    state_collection: str,
    fix_c: float,
    diff: float,
    dna_code: str,
    symbol: str,
    decimal_precision: int = 5,
    persist: bool = False,
) -> tuple[LiveInputs, AllInResult, dict[str, Any] | None]:
    live = load_live_inputs(
        settings,
        firebase_info=firebase_info,
        rows_collection=rows_collection,
        state_collection=state_collection,
        symbol=symbol,
        fix_c=fix_c,
        diff=diff,
        dna_code=dna_code,
        decimal_precision=decimal_precision,
    )
    result = run_dataframe_chain(
        live.raw,
        fix_c,
        dna_code,
        diff,
        live.anchor,
        decimal_precision,
    )
    persisted = (
        persist_final(
            live,
            result,
            rows_collection=rows_collection,
            state_collection=state_collection,
        )
        if persist
        else None
    )
    return live, result, persisted


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one-new-row Webull LEGO 0→18")
    parser.add_argument("--environment", choices=ENDPOINTS, default="Test (UAT)")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--dna-code", default=os.getenv("DNA_CODE", "bypass:100"))
    parser.add_argument("--fix-c", type=float, default=float(os.getenv("FIX_C", "1500")))
    parser.add_argument("--diff", type=float, default=float(os.getenv("DIFF", "30")))
    parser.add_argument("--precision", type=int, default=5)
    parser.add_argument("--rows-collection", default="webull_lego_rows")
    parser.add_argument("--state-collection", default="webull_lego_state")
    parser.add_argument("--persist", action="store_true")
    parser.add_argument("--output-dir", type=Path, default=Path("lego_output"))
    return parser.parse_args()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    args = _arguments()
    settings = WebullSettings(
        args.environment,
        os.getenv("WEBULL_ACCOUNT_ID", ""),
        os.getenv("WEBULL_APP_KEY", ""),
        os.getenv("WEBULL_APP_SECRET", ""),
    )
    live, result, persisted = run_live_all_in(
        settings,
        firebase_info=None,
        rows_collection=args.rows_collection,
        state_collection=args.state_collection,
        fix_c=args.fix_c,
        diff=args.diff,
        dna_code=args.dna_code,
        symbol=args.symbol,
        decimal_precision=args.precision,
        persist=args.persist,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result.final.to_csv(
        args.output_dir / "webull_lego_final_row.csv",
        index=False,
        encoding="utf-8-sig",
    )
    (args.output_dir / "webull_lego_safe_summary.json").write_text(
        json.dumps(
            {
                **live.safe_summary,
                "persisted": persisted,
                "final_row": result.final.iloc[0].to_dict(),
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "run_id": live.run_id,
                "rows": len(result.final),
                "persisted": persisted,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
