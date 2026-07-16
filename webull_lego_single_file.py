"""Webull LEGO 0→18 (one new row) — real read-only API + DNA + recurrence.

Quick Start (PowerShell)
------------------------
1. Install dependencies::

       python -m pip install pandas numpy google-cloud-firestore google-auth webull-openapi-python-sdk

2. Keep credentials out of this file and shell history::

       $env:WEBULL_ACCOUNT_ID="..."
       $env:WEBULL_APP_KEY="..."
       $env:WEBULL_APP_SECRET="..."
       $env:GOOGLE_APPLICATION_CREDENTIALS="C:\\safe\\firebase.json"

3. Compute the single new row against Test/UAT::

       python webull_lego_single_file.py --environment "Test (UAT)" --symbol AAPL --dna-code "bypass:100"

This file is intentionally read-only: it reads one Webull snapshot
(positions + quote), reads the chain's latest recurrence anchor from
``webull_lego_state`` (never the trade log), and computes exactly one new
17-column row.  It has no place/cancel method and never appends to Firestore,
so the standalone runner cannot mutate either environment.  The Streamlit
dashboard performs the transactional Step-18 append; ``compute_one_row`` here
uses the identical formulas so both produce the same row (a parity test
enforces this).
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
from typing import Any, Callable

import numpy as np


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
FINANCIAL_COLUMNS = frozenset(FINAL_COLUMNS[5:6]) | frozenset(FINAL_COLUMNS[11:])
STATE_COLLECTION = "webull_lego_state"

STATUS_PASS_DNA_ZERO = "PASS_DNA_ZERO"
STATUS_PASS_THRESHOLD = "PASS_THRESHOLD"
STATUS_READY_BUY = "READY_BUY"
STATUS_READY_SELL = "READY_SELL"


@dataclass(frozen=True)
class DnaSpec:
    length: int
    mutation_rate: float
    dna_seed: int
    mutation_seeds: tuple[int, ...]
    raw_numbers: tuple[int, ...]


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
            if not value.strip()
        ]
        if missing:
            raise ValueError(f"Missing credential environment variable: {', '.join(missing)}")
        _ = self.endpoint


# 2) DNA decoder ---------------------------------------------------------------
def decode_number_stream(encoded: str) -> list[int]:
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


def parse_dna_spec(encoded: str) -> DnaSpec:
    numbers = decode_number_stream(encoded)
    if len(numbers) < 3:
        raise ValueError("DNA string must encode length, rate, and at least one seed")
    length = int(numbers[0])
    rate = float(numbers[1])
    if length <= 0:
        raise ValueError("DNA length must be greater than 0")
    if rate < 0:
        raise ValueError("DNA mutation rate cannot be negative")
    if rate > 1:
        rate /= 100.0
    if rate > 1:
        raise ValueError("DNA mutation rate cannot be greater than 100%")
    return DnaSpec(
        length=length,
        mutation_rate=rate,
        dna_seed=int(numbers[2]),
        mutation_seeds=tuple(int(seed) for seed in numbers[3:]),
        raw_numbers=tuple(numbers),
    )


def decode_dna(encoded: str) -> tuple[np.ndarray, dict[str, Any]]:
    text = encoded.strip()
    bypass: int | None = None
    if text.lower().startswith("bypass:"):
        try:
            bypass = int(text.split(":", 1)[1].strip())
        except ValueError as exc:
            raise ValueError("Bypass DNA length must be an integer") from exc
    elif text.startswith("["):
        try:
            value = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("Bypass DNA array format must be [1, length]") from exc
        if (
            not isinstance(value, list) or len(value) != 2
            or type(value[0]) is not int or type(value[1]) is not int
            or value[0] != 1
        ):
            raise ValueError("Bypass DNA array format must be [1, length]")
        bypass = value[1]
    if bypass is not None:
        if bypass <= 0:
            raise ValueError("Bypass DNA length must be greater than 0")
        return np.ones(bypass, dtype=np.int8), {
            "mode": "bypass", "length": bypass, "mutation_rate": 0.0, "seeds": [],
        }

    spec = parse_dna_spec(text)
    dna = np.random.default_rng(spec.dna_seed).integers(0, 2, size=spec.length).astype(np.int8)
    dna[0] = 1
    for seed in spec.mutation_seeds:
        mask = np.random.default_rng(seed).random(spec.length) < spec.mutation_rate
        dna[mask] = 1 - dna[mask]
        dna[0] = 1
    summary = asdict(spec)
    summary.update({"mode": "encoded", "seeds": [spec.dna_seed, *spec.mutation_seeds]})
    return dna, summary


# 3) Real read-only Webull + Firestore ----------------------------------------
def _response_json(response: Any) -> Any:
    status = getattr(response, "status_code", None)
    if status is None:
        return response
    if not 200 <= int(status) < 300:
        error = RuntimeError(f"Webull HTTP {status}")
        setattr(error, "status_code", int(status))
        raise error
    try:
        return response.json()
    except Exception as exc:
        raise RuntimeError("Webull returned invalid JSON") from exc


def _read_with_retry(label: str, call: Callable[[], Any], attempts: int = 3) -> Any:
    for attempt in range(attempts):
        try:
            return _response_json(call())
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            transient = status is None or status in (417, 429) or (status is not None and status >= 500)
            if not transient or attempt == attempts - 1:
                raise RuntimeError(f"{label} failed ({exc.__class__.__name__})") from exc
            time.sleep(0.25 * (2**attempt))
    raise AssertionError("unreachable")


class WebullReadOnlyClient:
    """Small real SDK adapter.  It intentionally exposes no mutation method."""

    def __init__(self, settings: WebullSettings):
        settings.validate()
        try:
            from webull.core.client import ApiClient
            from webull.data.data_client import DataClient
            from webull.trade.trade_client import TradeClient
        except ImportError as exc:
            raise RuntimeError("Install webull-openapi-python-sdk") from exc
        api = ApiClient(settings.app_key.strip(), settings.app_secret.strip(), settings.region)
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
        if not normalized:
            return None
        return _read_with_retry(
            "market snapshot",
            lambda: self.data.market_data.get_snapshot(
                normalized, "US_STOCK", extend_hour_required=False, overnight_required=False
            ),
        )


def _redact(value: Any) -> Any:
    sensitive = {
        "account", "accountid", "accountno", "accountnumber", "appkey", "appsecret",
        "token", "accesstoken", "refreshtoken", "secret", "secretkey",
    }
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            normalized = "".join(char for char in str(key).lower() if char.isalnum())
            is_sensitive = normalized in sensitive or normalized.endswith(
                ("accountid", "accountno", "accountnumber", "appkey", "appsecret", "accesstoken", "refreshtoken", "secretkey")
            )
            output[str(key)] = "[REDACTED]" if is_sensitive else _redact(item)
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
        credentials = service_account.Credentials.from_service_account_info(firebase_info)
        return firestore.Client(credentials=credentials, project=firebase_info["project_id"])
    return firestore.Client()


def read_latest_anchor(db: Any, chain_key: str) -> dict[str, Any] | None:
    """Read the chain's latest recurrence anchor (read-only, never the log)."""

    snapshot = db.collection(STATE_COLLECTION).document(chain_key).get()
    return snapshot.to_dict() if snapshot.exists else None


# 4) Snapshot extraction -------------------------------------------------------
def _iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list, tuple)):
                yield from _iter_dicts(child)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_dicts(item)


def _first(node: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = node.get(name)
        if value not in (None, ""):
            return value
    return None


def extract_holdings(positions_response: Any, symbol: str) -> float:
    normalized = symbol.upper()
    for node in _iter_dicts(positions_response):
        node_symbol = _first(node, "symbol", "ticker", "instrument_symbol", "instrumentSymbol")
        if node_symbol is None or str(node_symbol).upper() != normalized:
            continue
        quantity = _first(node, "quantity", "qty", "position", "position_qty", "positionQty", "available_qty", "availableQty")
        if quantity not in (None, ""):
            return float(quantity)
    return 0.0


def extract_price(quote_response: Any, symbol: str) -> float:
    normalized = symbol.upper()
    for node in _iter_dicts(quote_response):
        price = _first(node, "last_price", "lastPrice", "last", "price", "close", "close_price", "closePrice", "pPrice")
        if price in (None, ""):
            continue
        node_symbol = _first(node, "symbol", "ticker") or normalized
        if str(node_symbol).upper() == normalized:
            return float(price)
    return 0.0


# 5) Pure one-row engine (parity with lego_one_row.compute_row) ----------------
def compute_one_row(
    *,
    symbol: str,
    price: float,
    holdings: float,
    captured_at: str,
    anchor: dict[str, Any] | None,
    fix_c: float,
    diff: float = 0.0,
    dna_code: str = "bypass:100",
    decimal_precision: int = 5,
) -> dict[str, Any]:
    """Compute the single new 17-column row (full precision) + recurrence metadata.

    Mirrors ``lego_one_row.compute_row`` exactly so the standalone runner and the
    dashboard produce identical rows.
    """

    if not math.isfinite(float(fix_c)) or float(fix_c) <= 0:
        raise ValueError("fix_c must be finite and greater than 0")
    if not math.isfinite(float(price)) or float(price) <= 0:
        raise ValueError("price must be finite and greater than 0")
    if float(holdings) < 0:
        raise ValueError("holdings cannot be negative")

    dna, _ = decode_dna(dna_code)
    exists = bool(anchor)
    dna_step = 0 if not exists else int(anchor["dna_step"]) + 1
    if dna_step < 0 or dna_step >= len(dna):
        raise ValueError(f"DNA exhausted: step {dna_step} outside length {len(dna)}")
    dna_signal = int(dna[dna_step])

    value_now = float(holdings) * float(price)
    gap = float(fix_c) - value_now
    if dna_signal == 0:
        status, action, side, quantity = STATUS_PASS_DNA_ZERO, "PASS", None, 0.0
    elif abs(gap) <= float(diff):
        status, action, side, quantity = STATUS_PASS_THRESHOLD, "PASS", None, 0.0
    elif gap > 0:
        status, action, side = STATUS_READY_BUY, "BUY", "BUY"
        quantity = round(abs(gap) / float(price), int(decimal_precision))
    else:
        status, action, side = STATUS_READY_SELL, "SELL", "SELL"
        quantity = round(abs(gap) / float(price), int(decimal_precision))

    if not exists:
        p0, reference, delta_actual, actual_cumulative, excess = float(price), 0.0, 0.0, 0.0, 0.0
    else:
        p0 = float(anchor["p0"])
        prev_price = float(anchor["prev_price"])
        prev_actual = float(anchor["prev_actual"])
        reference = float(fix_c) * math.log(float(price) / p0)
        delta_actual = float(fix_c) * (float(price) / prev_price - 1.0)
        actual_cumulative = prev_actual + delta_actual
        excess = actual_cumulative - reference

    columns = {
        "เวลา (UTC)": captured_at,
        "สินทรัพย์": symbol.strip().upper(),
        "สถานะ": status,
        "DNA step": dna_step,
        "DNA signal": dna_signal,
        "ราคา Pₙ (USD)": float(price),
        "จำนวนถือครอง (หุ้น)": float(holdings),
        "คำสั่ง": action,
        "ฝั่ง": side,
        "เหตุผล": status,
        "จำนวนสั่ง (หุ้น)": float(quantity),
        "มูลค่าพอร์ต (USD)": value_now,
        "ส่วนต่างเป้าหมาย (USD)": gap,
        "Rₙ อ้างอิง (USD)": reference,
        "ΔAₙ ต่อสเต็ป (USD)": delta_actual,
        "Aₙ สะสม (USD)": actual_cumulative,
        "Eₙ ส่วนเกินสะสม (USD)": excess,
    }
    metadata = {
        "dna_step": dna_step,
        "p0": p0,
        "prev_price": float(price),
        "prev_actual": actual_cumulative,
        "anchor_exists": exists,
    }
    return {"columns": columns, "metadata": metadata}


def present_row(columns: dict[str, Any]) -> dict[str, Any]:
    """Round financial columns to 2 dp for display/export only."""

    presented: dict[str, Any] = {}
    for name in FINAL_COLUMNS:
        value = columns.get(name)
        if value is not None and name in FINANCIAL_COLUMNS:
            value = round(float(value), 2)
        presented[name] = value
    return presented


# 6) All-in orchestration ------------------------------------------------------
def run_live_one_row(
    settings: WebullSettings,
    *,
    firebase_info: dict[str, Any] | None,
    fix_c: float,
    diff: float,
    dna_code: str,
    symbol: str,
    decimal_precision: int = 5,
) -> dict[str, Any]:
    """Execute real Step 0 reads, then compute the single new row (no writes)."""

    started = time.perf_counter()
    client = WebullReadOnlyClient(settings)
    account_list = client.account_list()
    balance = client.balance()
    positions = client.positions()
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise ValueError("a symbol is required — price comes from a live quote")
    quote = client.quote(normalized_symbol)

    price = extract_price(quote, normalized_symbol)
    holdings = extract_holdings(positions, normalized_symbol)
    if not math.isfinite(price) or price <= 0:
        raise ValueError(f"no positive live quote for {normalized_symbol}")

    fingerprint = hashlib.sha256(settings.account_id.encode("utf-8")).hexdigest()[:12]
    config_payload = json.dumps(
        {
            "strategy_id": "shannon_demon_lego",
            "fix_c": float(fix_c),
            "diff": float(diff),
            "decimal_precision": int(decimal_precision),
            "dna_hash": hashlib.sha256(decode_dna(dna_code)[0].tobytes()).hexdigest(),
        },
        sort_keys=True,
    )
    config_hash = hashlib.sha256(config_payload.encode("utf-8")).hexdigest()
    chain_key = hashlib.sha256(
        "\x00".join((settings.environment, fingerprint, normalized_symbol, config_hash)).encode("utf-8")
    ).hexdigest()

    anchor = None
    if firebase_info is not None:
        db = _firestore_client(firebase_info)
        anchor = read_latest_anchor(db, chain_key)

    captured_at = datetime.now(timezone.utc).isoformat()
    row = compute_one_row(
        symbol=normalized_symbol,
        price=price,
        holdings=holdings,
        captured_at=captured_at,
        anchor=anchor,
        fix_c=fix_c,
        diff=diff,
        dna_code=dna_code,
        decimal_precision=decimal_precision,
    )
    elapsed = time.perf_counter() - started
    return {
        "environment": settings.environment,
        "endpoint": settings.endpoint,
        "account_fingerprint": fingerprint,
        "symbol": normalized_symbol,
        "chain_key": chain_key,
        "anchor_exists": bool(anchor),
        "old_trade_log_reads": 0,
        "final_row": present_row(row["columns"]),
        "recurrence_metadata": row["metadata"],
        "elapsed_seconds": round(elapsed, 3),
        "account_list": _redact(account_list),
        "balance": _redact(balance),
        "positions": _redact(positions),
        "quote": _redact(quote),
    }


# 7) Beginner CLI --------------------------------------------------------------
def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute the single new read-only Webull LEGO row 0→18")
    parser.add_argument("--environment", choices=ENDPOINTS, default="Test (UAT)")
    parser.add_argument("--symbol", required=True, help="US symbol for the live quote")
    parser.add_argument("--dna-code", default=os.getenv("DNA_CODE", "bypass:100"))
    parser.add_argument("--fix-c", type=float, default=float(os.getenv("FIX_C", "1500")))
    parser.add_argument("--diff", type=float, default=float(os.getenv("DIFF", "0")))
    parser.add_argument("--decimal-precision", type=int, default=int(os.getenv("DECIMAL_PRECISION", "5")))
    parser.add_argument("--output-dir", type=Path, default=Path("lego_output"))
    return parser.parse_args()


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    args = _arguments()
    settings = WebullSettings(
        environment=args.environment,
        account_id=os.getenv("WEBULL_ACCOUNT_ID", ""),
        app_key=os.getenv("WEBULL_APP_KEY", ""),
        app_secret=os.getenv("WEBULL_APP_SECRET", ""),
    )
    result = run_live_one_row(
        settings,
        firebase_info=None,
        fix_c=args.fix_c,
        diff=args.diff,
        dna_code=args.dna_code,
        symbol=args.symbol,
        decimal_precision=args.decimal_precision,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summary = {**result, "completed_at_utc": datetime.now(timezone.utc).isoformat()}
    (args.output_dir / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
