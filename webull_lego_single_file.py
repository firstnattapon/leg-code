"""Webull LEGO 0→18 — real read-only API + DNA + DataFrame in one file.

Quick Start (PowerShell)
------------------------
1. Install dependencies::

       python -m pip install pandas numpy google-cloud-firestore google-auth webull-openapi-python-sdk

2. Keep credentials out of this file and shell history::

       $env:WEBULL_ACCOUNT_ID="..."
       $env:WEBULL_APP_KEY="..."
       $env:WEBULL_APP_SECRET="..."
       $env:GOOGLE_APPLICATION_CREDENTIALS="C:\\safe\\firebase.json"

3. Run every read-only LEGO step against Test/UAT::

       python webull_lego_single_file.py --environment "Test (UAT)" --symbol AAPL --dna-code "bypass:100"

Use ``--environment Production`` for real production reads.  This file has no
place/cancel method, so the all-in loop cannot mutate either environment.

The 0→18 chain is intentionally explicit:

0. authenticate and read account list, balance, positions, quote, and Firestore
1–17. build the exact 17-column accumulated DataFrame oldest→newest
18. validate/export newest→oldest Final DataFrame + a separate what-if ledger

No credential, raw account response, or Firebase key is written to output.
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
import pandas as pd


# 1) Contract -----------------------------------------------------------------
# These names are the only final output contract.  Broker/API payloads never
# enter this list, which prevents account metadata from leaking into CSV.
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
PRICE_FIELDS = (
    "last_price", "price", "market_state_last_price", "decision_last_price",
    "fill_price", "filled_price", "avg_price", "executed_price",
)
HOLDING_FIELDS = ("position_after", "market_state_quantity", "quantity")
EXECUTION_PRICE_FIELDS = (
    "average_filled_price", "average_fill_price", "avg_filled_price",
    "avg_fill_price", "filled_price", "fill_price", "executed_price",
    "execution_price",
)
FILLED_QUANTITY_FIELDS = (
    "filled_quantity", "cumulative_filled_quantity", "filled_qty",
)
FEE_FIELDS = (
    "transaction_fee", "filled_fee", "execution_fee", "commission",
)
TERMINAL_FILL_STATUSES = {
    "ORDER_FILLED", "ORDER_PARTIAL_FILLED_TERMINAL", "FILLED",
}


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
            name for name, value in (
                ("WEBULL_ACCOUNT_ID", self.account_id),
                ("WEBULL_APP_KEY", self.app_key),
                ("WEBULL_APP_SECRET", self.app_secret),
            ) if not value.strip()
        ]
        if missing:
            raise ValueError(f"Missing credential environment variable: {', '.join(missing)}")
        _ = self.endpoint


@dataclass
class LiveInputs:
    """Stage-0 values kept in memory; only ``safe_summary`` may be exported."""

    raw: pd.DataFrame
    firestore_client: Any
    account_list: Any = field(repr=False)
    balance: Any = field(repr=False)
    positions: Any = field(repr=False)
    quote: Any = field(repr=False)
    safe_summary: dict[str, Any]


@dataclass
class AllInResult:
    final: pd.DataFrame
    what_if: pd.DataFrame
    accumulated: dict[int, pd.DataFrame]
    diagnostics: dict[int, list[str]]


# 2) DNA decoder ---------------------------------------------------------------
def decode_number_stream(encoded: str) -> list[int]:
    """Decode the Learning Guide's ``[width][value]...`` number stream."""

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
    """Decode encoded, ``bypass:N``, or ``[1,N]`` DNA deterministically."""

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
    dna = np.random.default_rng(spec.dna_seed).integers(
        0, 2, size=spec.length
    ).astype(np.int8)
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
    """Retry idempotent reads only; authentication/validation failures fail fast."""

    for attempt in range(attempts):
        try:
            return _response_json(call())
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            # 417 is Webull's OPENAPI_REPEAT_REQUEST throttle response.
            transient = (
                status is None
                or status in (417, 429)
                or (status is not None and status >= 500)
            )
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
        return _read_with_retry(
            "account list", self.trade.account_v2.get_account_list
        )

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
                normalized,
                "US_STOCK",
                extend_hour_required=False,
                overnight_required=False,
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
                (
                    "accountid", "accountno", "accountnumber", "appkey",
                    "appsecret", "accesstoken", "refreshtoken", "secretkey",
                )
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
    return firestore.Client()  # Application Default Credentials


def load_firestore_rows(db: Any, collection: str, limit: int) -> pd.DataFrame:
    """Read the bot's real trade/DNA ledger from Firestore newest-first."""

    try:
        from google.cloud import firestore
    except ImportError as exc:
        raise RuntimeError("Install google-cloud-firestore") from exc
    documents = (
        db.collection(collection)
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(max(1, min(1000, int(limit))))
        .stream()
    )
    rows = [document.to_dict() for document in documents]
    return pd.json_normalize(rows, sep="_") if rows else pd.DataFrame()


def _first_symbol(raw: pd.DataFrame) -> str:
    if "symbol" not in raw:
        return ""
    symbols = raw["symbol"].dropna().astype(str).str.strip()
    symbols = symbols[symbols.ne("")]
    return symbols.iloc[0].upper() if not symbols.empty else ""


def load_live_inputs(
    settings: WebullSettings,
    *,
    firebase_info: dict[str, Any] | None,
    collection: str,
    limit: int,
    symbol: str = "",
) -> LiveInputs:
    """Run Step 0 using real Webull reads and a real Firestore query."""

    started = time.perf_counter()
    client = WebullReadOnlyClient(settings)
    account_list = client.account_list()
    balance = client.balance()
    positions = client.positions()
    db = _firestore_client(firebase_info)
    raw = load_firestore_rows(db, collection, limit)
    chosen_symbol = symbol.strip().upper() or _first_symbol(raw)
    quote = client.quote(chosen_symbol) if chosen_symbol else None
    elapsed = time.perf_counter() - started
    fingerprint = hashlib.sha256(settings.account_id.encode("utf-8")).hexdigest()[:12]
    safe_summary = {
        "environment": settings.environment,
        "endpoint": settings.endpoint,
        "account_fingerprint": fingerprint,
        "api_reads": ["account_list", "account_balance", "positions"]
        + (["market_snapshot"] if chosen_symbol else []),
        "symbol": chosen_symbol or None,
        "trade_collection": collection,
        "trade_rows": len(raw),
        "elapsed_seconds": round(elapsed, 3),
        "account_list": _redact(account_list),
        "balance": _redact(balance),
        "positions": _redact(positions),
        "quote": _redact(quote),
    }
    return LiveInputs(raw, db, account_list, balance, positions, quote, safe_summary)


# 4) Pure LEGO DataFrame engine ------------------------------------------------
def _object(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    result = pd.Series(pd.NA, index=frame.index, dtype="object")
    for name in names:
        if name not in frame:
            continue
        values = frame[name]
        usable = values.notna() & values.astype(str).str.strip().ne("")
        result = result.where(result.notna() | ~usable, values)
    return result


def _text(frame: pd.DataFrame, names: tuple[str, ...]) -> pd.Series:
    values = _object(frame, names).astype("string").str.strip()
    return values.mask(values.eq(""), pd.NA)


def _candidates(frame: pd.DataFrame, names: tuple[str, ...]) -> tuple[str, ...]:
    found = [name for name in names if name in frame]
    for name in names:
        for column in frame.columns:
            if str(column).endswith(f"_{name}") and column not in found:
                found.append(str(column))
    return tuple(found)


def _number(frame: pd.DataFrame, names: tuple[str, ...], suffixes: bool = False) -> pd.Series:
    result = pd.Series(np.nan, index=frame.index, dtype=float)
    fields = _candidates(frame, names) if suffixes else names
    for name in fields:
        if name in frame:
            result = result.where(result.notna(), pd.to_numeric(frame[name], errors="coerce"))
    return result


def prepare_raw(frame: pd.DataFrame) -> pd.DataFrame:
    """Drop export indexes and sort the real log oldest→newest."""

    raw = frame.copy()
    drop = [
        column for column in raw.columns
        if str(column).strip() == "" or str(column).startswith("Unnamed:") or str(column) == "H1"
    ]
    if drop:
        raw = raw.drop(columns=drop)
    raw["__row_id"] = np.arange(len(raw), dtype=int)
    parsed = pd.to_datetime(_object(raw, ("created_at", "เวลา (UTC)")), errors="coerce", utc=True)
    raw["__sort_time"] = parsed
    raw = raw.sort_values(["__sort_time", "__row_id"], kind="stable", na_position="last")
    return raw.drop(columns="__sort_time").reset_index(drop=True)


def _realized_ledger(raw: pd.DataFrame, fix_c: float) -> pd.DataFrame:
    """Broker-confirmed cash only: terminal fill + price + reconciled position."""

    status = _text(raw, ("status",)).fillna("").str.upper()
    side = _text(raw, ("side", "decision_side")).fillna("").str.upper()
    order_id = _text(raw, ("client_order_id", "order_id"))
    filled = _number(raw, FILLED_QUANTITY_FIELDS, suffixes=True)
    price = _number(raw, EXECUTION_PRICE_FIELDS, suffixes=True)
    fee = _number(raw, FEE_FIELDS, suffixes=True).fillna(0.0)
    reconciled = (
        raw["position_reconciled"].map(
            lambda value: isinstance(value, (bool, np.bool_)) and bool(value)
        )
        if "position_reconciled" in raw
        else pd.Series(False, index=raw.index)
    )
    eligible = status.isin(TERMINAL_FILL_STATUSES) & side.isin(("BUY", "SELL"))
    eligible &= filled.gt(0) & price.gt(0) & reconciled
    p0 = float(price[eligible].iloc[0]) if eligible.any() else math.nan
    output = pd.DataFrame(
        {"reference": np.nan, "delta": np.nan, "actual": np.nan}, index=raw.index
    )
    counted: dict[str, tuple[float, float, float]] = {}
    actual = 0.0
    reference = math.nan
    has_fill = False
    for index in raw.index:
        if eligible.loc[index]:
            key = str(order_id.loc[index]) if pd.notna(order_id.loc[index]) else f"row:{index}"
            quantity = float(filled.loc[index])
            execution = float(price.loc[index])
            notional = quantity * execution
            total_fee = max(0.0, float(fee.loc[index]))
            old_quantity, old_notional, old_fee = counted.get(key, (0.0, 0.0, 0.0))
            if quantity > old_quantity + 1e-12:
                increment = notional - old_notional
                if increment > 0:
                    sign = 1.0 if side.loc[index] == "SELL" else -1.0
                    output.loc[index, "delta"] = sign * increment - max(0.0, total_fee - old_fee)
                    actual += float(output.loc[index, "delta"])
                    reference = float(fix_c) * math.log(execution / p0)
                    counted[key] = (quantity, notional, total_fee)
                    has_fill = True
        if has_fill:
            output.loc[index, "delta"] = 0.0 if pd.isna(output.loc[index, "delta"]) else output.loc[index, "delta"]
            output.loc[index, "actual"] = actual
            output.loc[index, "reference"] = reference
    return output


def run_dataframe_chain(raw_input: pd.DataFrame, fix_c: float, dna_code: str = "") -> AllInResult:
    """Run Steps 1–18 locally; no network call occurs in this pure function."""

    if not math.isfinite(float(fix_c)) or float(fix_c) <= 0:
        raise ValueError("fix_c must be finite and greater than 0")
    raw = prepare_raw(raw_input)
    previous = pd.DataFrame(index=raw.index)
    results: dict[int, pd.DataFrame] = {}
    diagnostics: dict[int, list[str]] = {}

    parsed = pd.to_datetime(_object(raw, ("created_at", "เวลา (UTC)")), errors="coerce", utc=True)
    values = pd.Series(pd.NA, index=raw.index, dtype="string")
    values.loc[parsed.notna()] = parsed[parsed.notna()].map(
        lambda value: value.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )
    previous[FINAL_COLUMNS[0]] = values
    diagnostics[1] = [f"valid UTC {int(values.notna().sum())}/{len(raw)}"]
    results[1] = previous.copy()

    previous[FINAL_COLUMNS[1]] = _text(raw, ("symbol", "สินทรัพย์")).str.upper()
    diagnostics[2] = ["symbol comes from the real trade log"]
    results[2] = previous.copy()

    previous[FINAL_COLUMNS[2]] = _text(raw, ("status", "สถานะ")).str.upper()
    diagnostics[3] = ["missing status stays blank"]
    results[3] = previous.copy()

    steps = _number(raw, ("dna_step", "DNA step"))
    valid_steps = steps.notna() & steps.ge(0) & np.isclose(steps % 1, 0)
    previous[FINAL_COLUMNS[3]] = steps.where(valid_steps).astype("Int64")
    diagnostics[4] = ["DNA step must be a non-negative integer"]
    results[4] = previous.copy()

    logged_raw = _object(raw, ("dna_signal", "DNA signal"))
    logged_present = logged_raw.notna()
    logged_signal = _number(raw, ("dna_signal", "DNA signal"))
    signals = logged_signal.where(logged_signal.isin((0, 1))).astype("Int8")
    decoded_count = 0
    dna_summary: dict[str, Any] = {"mode": "logged-only"}
    if dna_code.strip():
        dna, dna_summary = decode_dna(dna_code)
        missing_logged = ~logged_present & signals.isna()
        for index in raw.index[missing_logged & previous[FINAL_COLUMNS[3]].notna()]:
            step = int(previous.loc[index, FINAL_COLUMNS[3]])
            if 0 <= step < len(dna):
                signals.loc[index] = int(dna[step])
                decoded_count += 1
    previous[FINAL_COLUMNS[4]] = signals
    diagnostics[5] = [f"decoded missing signals: {decoded_count}", json.dumps(dna_summary, default=str)]
    results[5] = previous.copy()

    price = _number(raw, (*PRICE_FIELDS, FINAL_COLUMNS[5]), suffixes=True)
    previous[FINAL_COLUMNS[5]] = price.where(np.isfinite(price) & price.gt(0))
    diagnostics[6] = ["positive decision-time quote; never a fill substitute"]
    results[6] = previous.copy()

    holdings = _number(raw, HOLDING_FIELDS)
    csv_holdings = _number(raw, (FINAL_COLUMNS[6],))
    holdings = holdings.where(holdings.notna(), csv_holdings)
    previous[FINAL_COLUMNS[6]] = holdings.where(np.isfinite(holdings) & holdings.ge(0))
    diagnostics[7] = ["Webull-observed holdings; expected_position_after excluded"]
    results[7] = previous.copy()

    action = _text(raw, ("decision_action", "action", "คำสั่ง")).str.upper()
    previous[FINAL_COLUMNS[7]] = action.where(action.isin(("BUY", "SELL", "PASS")))
    diagnostics[8] = ["BUY, SELL, or PASS"]
    results[8] = previous.copy()

    side = _text(raw, ("side", "decision_side", "ฝั่ง")).str.upper()
    previous[FINAL_COLUMNS[8]] = side.where(side.isin(("BUY", "SELL")))
    diagnostics[9] = ["PASS intentionally has no side"]
    results[9] = previous.copy()

    previous[FINAL_COLUMNS[9]] = _text(raw, ("decision_reason", "reason", "เหตุผล")).str.upper()
    diagnostics[10] = ["decision reason is preserved"]
    results[10] = previous.copy()

    order_quantity = _number(raw, (
        "decision_order_qty", "decision_order_quantity", "order_quantity", FINAL_COLUMNS[10],
    ))
    previous[FINAL_COLUMNS[10]] = order_quantity.where(
        np.isfinite(order_quantity) & order_quantity.ge(0)
    )
    diagnostics[11] = ["finite quantity >= 0; read-only"]
    results[11] = previous.copy()

    logged_value = _number(raw, ("decision_value_now_usd", "value_now_usd", FINAL_COLUMNS[11]))
    before = _number(raw, ("position_before", "pre_order_market_state_quantity"))
    before = before.where(before.notna(), previous[FINAL_COLUMNS[6]])
    derived = before * previous[FINAL_COLUMNS[5]]
    portfolio = logged_value.where(logged_value.notna(), derived)
    previous[FINAL_COLUMNS[11]] = portfolio.where(np.isfinite(portfolio) & portfolio.ge(0)).round(2)
    diagnostics[12] = ["logged value first; decision quantity × quote fallback"]
    results[12] = previous.copy()

    previous[FINAL_COLUMNS[12]] = (float(fix_c) - previous[FINAL_COLUMNS[11]]).round(2)
    diagnostics[13] = ["FIX_C - portfolio value; positive=BUY, negative=SELL"]
    results[13] = previous.copy()

    ledger = _realized_ledger(raw, float(fix_c))
    previous[FINAL_COLUMNS[13]] = ledger["reference"].round(2)
    diagnostics[14] = ["R_n uses confirmed execution price only"]
    results[14] = previous.copy()

    previous[FINAL_COLUMNS[14]] = ledger["delta"].round(2)
    diagnostics[15] = ["incremental fill notional, fee-aware, deduplicated by order id"]
    results[15] = previous.copy()

    previous[FINAL_COLUMNS[15]] = ledger["actual"].round(2)
    diagnostics[16] = ["broker-confirmed cumulative cash"]
    results[16] = previous.copy()

    previous[FINAL_COLUMNS[16]] = (
        previous[FINAL_COLUMNS[15]] - previous[FINAL_COLUMNS[13]]
    ).round(2)
    diagnostics[17] = ["E_n = A_n - R_n"]
    results[17] = previous.copy()

    final = previous.loc[:, FINAL_COLUMNS].iloc[::-1].reset_index(drop=True)
    what_if = _what_if(previous, float(fix_c))
    return AllInResult(final, what_if, results, diagnostics)


def _what_if(accumulated: pd.DataFrame, fix_c: float) -> pd.DataFrame:
    price = pd.to_numeric(accumulated[FINAL_COLUMNS[5]], errors="coerce")
    valid = list(price.index[price.notna() & np.isfinite(price) & price.gt(0)])
    reference = pd.Series(np.nan, index=price.index)
    delta = pd.Series(np.nan, index=price.index)
    actual = pd.Series(np.nan, index=price.index)
    excess = pd.Series(np.nan, index=price.index)
    if valid:
        p0 = previous = float(price.loc[valid[0]])
        cumulative = 0.0
        for offset, index in enumerate(valid):
            current = float(price.loc[index])
            step_delta = 0.0 if offset == 0 else fix_c * (current / previous - 1.0)
            cumulative += step_delta
            step_reference = fix_c * math.log(current / p0)
            reference.loc[index] = step_reference
            delta.loc[index] = step_delta
            actual.loc[index] = cumulative
            excess.loc[index] = cumulative - step_reference
            previous = current
    output = pd.DataFrame({
        "เวลา (UTC)": accumulated[FINAL_COLUMNS[0]],
        "ราคา Pₙ (USD)": price,
        "Rₙ what-if (USD)": reference.round(2),
        "ΔAₙ what-if (USD)": delta.round(2),
        "Aₙ what-if สะสม (USD)": actual.round(2),
        "Eₙ what-if สะสม (USD)": excess.round(2),
    })
    return output.iloc[::-1].reset_index(drop=True)


# 5) All-in orchestration ------------------------------------------------------
def run_live_all_in(
    settings: WebullSettings,
    *,
    firebase_info: dict[str, Any] | None,
    collection: str,
    limit: int,
    fix_c: float,
    dna_code: str = "",
    symbol: str = "",
) -> tuple[LiveInputs, AllInResult]:
    """Execute real Step 0, then pure Steps 1→18."""

    live = load_live_inputs(
        settings,
        firebase_info=firebase_info,
        collection=collection,
        limit=limit,
        symbol=symbol,
    )
    return live, run_dataframe_chain(live.raw, fix_c, dna_code)


# 6) Beginner CLI --------------------------------------------------------------
def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run real read-only Webull LEGO 0→18")
    parser.add_argument("--environment", choices=ENDPOINTS, default="Test (UAT)")
    parser.add_argument("--symbol", default="", help="Optional US symbol for a real quote read")
    parser.add_argument("--dna-code", default=os.getenv("DNA_CODE", ""))
    parser.add_argument("--collection", default=os.getenv("TRADE_COLLECTION", "shannon_demon_trades"))
    parser.add_argument("--limit", type=int, default=int(os.getenv("TRADE_LIMIT", "100")))
    parser.add_argument("--fix-c", type=float, default=float(os.getenv("FIX_C", "1500")))
    parser.add_argument("--output-dir", type=Path, default=Path("lego_output"))
    return parser.parse_args()


def main() -> None:
    # Windows installations using a Thai legacy code page cannot print the
    # Unicode arrows/Thai diagnostics in argparse help or JSON.  Reconfigure
    # only this process; the application and system locale remain untouched.
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
    live, result = run_live_all_in(
        settings,
        firebase_info=None,
        collection=args.collection,
        limit=args.limit,
        fix_c=args.fix_c,
        dna_code=args.dna_code,
        symbol=args.symbol,
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    result.final.to_csv(args.output_dir / "webull_lego_final.csv", index=False, encoding="utf-8-sig")
    result.what_if.to_csv(args.output_dir / "webull_lego_what_if.csv", index=False, encoding="utf-8-sig")
    summary = {
        **live.safe_summary,
        "completed_steps": list(range(19)),
        "final_rows": len(result.final),
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    (args.output_dir / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
