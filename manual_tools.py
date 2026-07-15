"""Pure logic and Webull helpers for the Streamlit Manual page.

Credentials are accepted only as runtime values. This module never writes
them to disk, logs them, or includes them in returned dictionaries. 
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable

import numpy as np


WEBULL_ENDPOINTS: dict[str, str] = {
    "Test (UAT)": "th-api.uat.webullbroker.com",
    "Production": "api.webull.co.th",
}
US_STOCK_CATEGORY = "US_STOCK"
DEFAULT_ORDER_DECIMAL_PRECISION = 5
MAX_BENCHMARK_ITERATIONS = 100_000


class ManualToolError(RuntimeError):
    """Base error surfaced safely in the Manual page."""


class WebullResponseError(ManualToolError):
    """Webull returned a non-successful HTTP response."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"Webull HTTP {status_code}: {body}")


@dataclass(frozen=True)
class ConnectionSettings:
    environment: str
    account_id: str = field(repr=False)
    app_key: str = field(repr=False)
    app_secret: str = field(repr=False)
    region: str = "th"

    @property
    def endpoint(self) -> str:
        try:
            return WEBULL_ENDPOINTS[self.environment]
        except KeyError as exc:
            valid = ", ".join(WEBULL_ENDPOINTS)
            raise ValueError(f"environment must be one of: {valid}") from exc

    @property
    def is_production(self) -> bool:
        return self.environment == "Production"

    def validate(self) -> None:
        missing = [
            name
            for name, value in (
                ("Account ID", self.account_id),
                ("App Key", self.app_key),
                ("App Secret", self.app_secret),
            )
            if not value.strip()
        ]
        if missing:
            raise ValueError(f"Missing required input: {', '.join(missing)}")
        _ = self.endpoint
        if not self.region.strip():
            raise ValueError("region is required")


@dataclass(frozen=True)
class DnaSpec:
    length: int
    mutation_rate: float
    dna_seed: int
    mutation_seeds: tuple[int, ...]
    raw_numbers: tuple[int, ...]

    @property
    def seeds(self) -> tuple[int, ...]:
        return (self.dna_seed, *self.mutation_seeds)


@dataclass(frozen=True)
class ShannonDecision:
    action: str
    side: str | None
    order_quantity: float
    value_now_usd: float
    rebalance_amount: float
    baseline_pnl: float
    reason: str

    def to_dict(self) -> dict[str, float | str | None]:
        payload = asdict(self)
        payload["order_qty"] = self.order_quantity
        payload["rebalance"] = self.rebalance_amount
        payload["baseline"] = self.baseline_pnl
        return payload


MAX_REBALANCING_STEPS = 500
REFERENCE_CURVE_PRICE_FLOOR_RATIO = 0.002


def rebalancing_cashflow_from_prices(
    prices: Iterable[float],
    fix_c: float,
    p0: float,
) -> list[dict[str, float]]:
    """Cash-flow table of the corrected Learning Guide 101.

    Step 0 anchors at ``P0``. For every observed price ``Pᵢ`` afterwards:
    ``ΔAᵢ = Fix_c × (Pᵢ/Pᵢ₋₁ − 1)`` accumulates into the actual rebalancing
    line ``Aₙ``, the theoretical reference is ``Rₙ = Fix_c × ln(Pₙ/P₀)``,
    and the cumulative excess is ``Eₙ = Aₙ − Rₙ``. Positive values are cash
    received from selling; negative values are cash spent buying.
    """
    if not math.isfinite(float(fix_c)) or not math.isfinite(float(p0)):
        raise ValueError("fix_c and p0 must be finite")
    if fix_c <= 0 or p0 <= 0:
        raise ValueError("fix_c and p0 must be greater than 0")

    rows: list[dict[str, float]] = [{
        "step": 0,
        "price": float(p0),
        "delta_actual": 0.0,
        "actual_cumulative": 0.0,
        "ln_reference": 0.0,
        "excess": 0.0,
    }]
    previous = float(p0)
    actual = 0.0
    for step, raw_price in enumerate(prices, start=1):
        price = float(raw_price)
        if not math.isfinite(price) or price <= 0:
            raise ValueError("Every price must be finite and greater than 0")
        delta = fix_c * (price / previous - 1.0)
        actual += delta
        reference = fix_c * math.log(price / p0)
        rows.append({
            "step": step,
            "price": price,
            "delta_actual": float(delta),
            "actual_cumulative": float(actual),
            "ln_reference": float(reference),
            "excess": float(actual - reference),
        })
        previous = price
    return rows


def simulate_rebalancing_prices(
    p0: float,
    vol: float,
    drift: float,
    steps: int,
    seed: int,
) -> list[float]:
    """Random price path of the guide's Testing Lab (geometric Brownian step).

    ``Pᵢ = Pᵢ₋₁ × exp((drift − vol²/2) + vol × Z)`` with a deterministic
    seed, floored at ``P0 × 1e-8`` so the log reference stays defined.
    """
    numeric_values = (p0, vol, drift)
    if not all(math.isfinite(float(value)) for value in numeric_values):
        raise ValueError("p0, vol, and drift must be finite")
    if p0 <= 0:
        raise ValueError("p0 must be greater than 0")
    if vol < 0:
        raise ValueError("vol cannot be negative")
    if steps < 2 or steps > MAX_REBALANCING_STEPS:
        raise ValueError(f"steps must be between 2 and {MAX_REBALANCING_STEPS}")

    rng = np.random.default_rng(int(seed))
    prices: list[float] = []
    price = float(p0)
    for _ in range(int(steps)):
        shock = (drift - 0.5 * vol * vol) + vol * float(rng.standard_normal())
        price = max(price * math.exp(shock), p0 * 1e-8)
        prices.append(price)
    return prices


def simulate_rebalancing_cashflow(
    fix_c: float,
    p0: float,
    vol: float,
    drift: float,
    steps: int,
    seed: int,
) -> list[dict[str, float]]:
    """Run the guide's Testing Lab: random prices + cash-flow table."""
    prices = simulate_rebalancing_prices(p0, vol, drift, steps, seed)
    return rebalancing_cashflow_from_prices(prices, fix_c, p0)


def rebalancing_reference_curve(
    fix_c: float,
    p0: float,
    excess: float,
    points: int = 200,
) -> list[dict[str, float]]:
    """Chart 2 of the corrected guide: capital versus price level.

    ``Y₁(x) = Fix_c × ln(x/P0)`` is the reference line and
    ``Y₂(x) = Y₁(x) + Eₙ`` places the cumulative excess on top of it as a
    constant vertical gap. The price axis conceptually starts at 0 but the
    curve starts at a small positive price because ``ln(0)`` diverges.
    """
    numeric_values = (fix_c, p0, excess)
    if not all(math.isfinite(float(value)) for value in numeric_values):
        raise ValueError("fix_c, p0, and excess must be finite")
    if fix_c <= 0 or p0 <= 0:
        raise ValueError("fix_c and p0 must be greater than 0")
    if points < 2 or points > 2000:
        raise ValueError("points must be between 2 and 2000")

    start = p0 * REFERENCE_CURVE_PRICE_FLOOR_RATIO
    rows: list[dict[str, float]] = []
    for x in np.linspace(start, 2.0 * p0, int(points)):
        y_reference = fix_c * math.log(float(x) / p0)
        rows.append({
            "price": float(x),
            "y_reference": float(y_reference),
            "y_rebalanced": float(y_reference + excess),
        })
    return rows


def decode_number_stream(encoded: str) -> list[int]:
    """Decode the compact ``[width][value]`` number stream."""
    if not encoded or not encoded.isdigit():
        raise ValueError("DNA string must be a non-empty digit string")

    values: list[int] = []
    index = 0
    while index < len(encoded):
        token_width = int(encoded[index])
        index += 1
        if token_width <= 0:
            raise ValueError("DNA token width must be greater than 0")

        next_index = index + token_width
        if next_index > len(encoded):
            raise ValueError("DNA string ended before a full token was decoded")
        values.append(int(encoded[index:next_index]))
        index = next_index
    return values


def normalize_mutation_rate(raw_rate: int | float) -> float:
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
    length = int(numbers[0])
    if length <= 0:
        raise ValueError("DNA length must be greater than 0")
    return DnaSpec(
        length=length,
        mutation_rate=normalize_mutation_rate(numbers[1]),
        dna_seed=int(numbers[2]),
        mutation_seeds=tuple(int(seed) for seed in numbers[3:]),
        raw_numbers=tuple(numbers),
    )


def parse_bypass_dna_code(encoded: str) -> int | None:
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
            values = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError("Bypass DNA array format must be [1, length]") from exc
        if (
            not isinstance(values, list)
            or len(values) != 2
            or type(values[0]) is not int
            or type(values[1]) is not int
            or values[0] != 1
            or values[1] <= 0
        ):
            raise ValueError("Bypass DNA array format must be [1, length]")
        return values[1]
    return None


def decode_dna(encoded: str) -> np.ndarray:
    bypass_length = parse_bypass_dna_code(encoded)
    if bypass_length is not None:
        return np.ones(bypass_length, dtype=np.int8)

    spec = parse_dna_spec(encoded)
    rng = np.random.default_rng(seed=spec.dna_seed)
    dna = rng.integers(0, 2, size=spec.length).astype(np.int8)
    dna[0] = 1
    for seed in spec.mutation_seeds:
        mutation_rng = np.random.default_rng(seed=seed)
        mutation_mask = mutation_rng.random(spec.length) < spec.mutation_rate
        dna[mutation_mask] = 1 - dna[mutation_mask]
        dna[0] = 1
    return dna


def encode_dna(length: int, mutation_rate: int, seeds: Iterable[int]) -> str:
    values = [int(length), int(mutation_rate), *[int(seed) for seed in seeds]]
    if len(values) < 3:
        raise ValueError("At least one seed is required")
    if values[0] <= 0:
        raise ValueError("length must be greater than 0")
    if not 0 <= values[1] <= 100:
        raise ValueError("mutation_rate must be between 0 and 100")
    if any(value < 0 for value in values):
        raise ValueError("DNA values cannot be negative")
    if any(len(str(value)) > 9 for value in values):
        raise ValueError("Each DNA value must contain at most 9 digits")
    return "".join(f"{len(str(value))}{value}" for value in values)


def dna_summary(encoded: str) -> dict[str, Any]:
    dna = decode_dna(encoded)
    raw = dna.tobytes()
    return {
        "length": int(len(dna)),
        "ones": int(np.count_nonzero(dna)),
        "zeros": int(len(dna) - np.count_nonzero(dna)),
        "ones_ratio": float(np.mean(dna)),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "output": dna.astype(int).tolist(),
    }


def calculate_shannon_decision(
    quantity: float,
    last_price: float,
    fix_c: float,
    p0: float,
    diff: float,
    decimal_precision: int = DEFAULT_ORDER_DECIMAL_PRECISION,
) -> ShannonDecision:
    """Run the same Logical FIX_C calculation as the trading bot."""
    numeric_values = (quantity, last_price, fix_c, p0, diff)
    if not all(math.isfinite(float(value)) for value in numeric_values):
        raise ValueError("All numeric inputs must be finite")
    if quantity < 0:
        raise ValueError("Negative positions are not supported")
    if last_price <= 0 or fix_c <= 0 or p0 <= 0:
        raise ValueError("last_price, fix_c, and p0 must be greater than 0")
    if diff < 0 or decimal_precision < 0:
        raise ValueError("diff and decimal_precision cannot be negative")

    value_now_usd = quantity * last_price
    rebalance_amount = abs(fix_c - value_now_usd)
    baseline_pnl = fix_c * math.log(last_price / p0)

    if rebalance_amount <= diff:
        return ShannonDecision(
            "PASS", None, 0.0, value_now_usd, rebalance_amount,
            baseline_pnl, "WITHIN_THRESHOLD",
        )

    order_quantity = round(rebalance_amount / last_price, decimal_precision)
    if value_now_usd < fix_c - diff:
        if order_quantity <= 0:
            return ShannonDecision(
                "PASS", None, 0.0, value_now_usd, rebalance_amount,
                baseline_pnl, "BUY_QTY_ZERO_AFTER_ROUND",
            )
        return ShannonDecision(
            "BUY", "BUY", float(order_quantity), value_now_usd,
            rebalance_amount, baseline_pnl, "BELOW_TARGET",
        )
    if value_now_usd > fix_c + diff:
        if order_quantity <= 0:
            return ShannonDecision(
                "PASS", None, 0.0, value_now_usd, rebalance_amount,
                baseline_pnl, "SELL_QTY_ZERO_AFTER_ROUND",
            )
        return ShannonDecision(
            "SELL", "SELL", float(order_quantity), value_now_usd,
            rebalance_amount, baseline_pnl, "ABOVE_TARGET",
        )
    return ShannonDecision(
        "PASS", None, 0.0, value_now_usd, rebalance_amount,
        baseline_pnl, "NO_RULE_MATCH",
    )


def generate_client_order_id(strategy_id: str, symbol: str, *parts: object) -> str:
    raw = ":".join([strategy_id, symbol.upper(), *[str(part) for part in parts]])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def format_order_quantity(quantity: float) -> str:
    value = float(quantity)
    if not math.isfinite(value) or value <= 0:
        raise ValueError("quantity must be a finite number greater than 0")
    return f"{value:.{DEFAULT_ORDER_DECIMAL_PRECISION}f}".rstrip("0").rstrip(".")


def build_market_order_payload(
    symbol: str,
    side: str,
    quantity: float,
    client_order_id: str,
    trading_session: str = "CORE",
) -> list[dict[str, str]]:
    normalized_symbol = symbol.strip().upper()
    normalized_side = side.strip().upper()
    normalized_session = trading_session.strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol is required")
    if normalized_side not in {"BUY", "SELL"}:
        raise ValueError("side must be BUY or SELL")
    if not client_order_id.strip() or len(client_order_id) > 40:
        raise ValueError("client_order_id must contain 1-40 characters")
    if normalized_session not in {"CORE", "PRE", "AFTER", "OVERNIGHT"}:
        raise ValueError("Unsupported trading session")
    return [{
        "combo_type": "NORMAL",
        "client_order_id": client_order_id.strip(),
        "symbol": normalized_symbol,
        "instrument_type": "EQUITY",
        "market": "US",
        "order_type": "MARKET",
        "quantity": format_order_quantity(quantity),
        "support_trading_session": normalized_session,
        "side": normalized_side,
        "time_in_force": "DAY",
        "entrust_type": "QTY",
    }]


def response_json_or_raise(response: Any) -> Any:
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        return response
    if not 200 <= int(status_code) < 300:
        body = str(getattr(response, "text", repr(response)))[:2000]
        raise WebullResponseError(int(status_code), body)
    try:
        return response.json()
    except Exception as exc:
        raise WebullResponseError(int(status_code), "Invalid JSON response") from exc


def iter_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list, tuple)):
                yield from iter_dicts(child)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from iter_dicts(item)


def first_value(obj: Any, *names: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        for name in names:
            value = obj.get(name)
            if value not in (None, ""):
                return value
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value not in (None, ""):
                return value
    return default


def extract_quantity(response: Any, symbol: str) -> float:
    normalized = symbol.upper()
    for position in iter_dicts(response):
        position_symbol = first_value(
            position, "symbol", "ticker", "instrument_symbol", "instrumentSymbol"
        )
        if position_symbol is None or str(position_symbol).upper() != normalized:
            continue
        quantity = first_value(
            position, "quantity", "qty", "position", "position_qty",
            "positionQty", "available_qty", "availableQty",
        )
        if quantity not in (None, ""):
            return float(quantity)
    return 0.0


def extract_last_price(response: Any, symbol: str) -> float:
    normalized = symbol.upper()
    for quote in iter_dicts(response):
        price = first_value(
            quote, "last_price", "lastPrice", "last", "price", "close",
            "close_price", "closePrice", "pPrice",
        )
        if price in (None, ""):
            continue
        quote_symbol = first_value(quote, "symbol", "ticker", default=normalized)
        if str(quote_symbol).upper() == normalized:
            return float(price)
    return 0.0


class WebullManualClient:
    """Short-lived Webull client for explicit Manual-page actions."""

    def __init__(self, settings: ConnectionSettings):
        settings.validate()
        try:
            from webull.core.client import ApiClient
            from webull.data.data_client import DataClient
            from webull.trade.trade_client import TradeClient
        except ImportError as exc:
            raise ManualToolError(
                "webull-openapi-python-sdk is not installed"
            ) from exc

        api_client = ApiClient(
            settings.app_key.strip(),
            settings.app_secret.strip(),
            settings.region.strip().lower(),
        )
        api_client.add_endpoint(settings.region.strip().lower(), settings.endpoint)
        self.settings = settings
        self.data_client = DataClient(api_client)
        self.trade_client = TradeClient(api_client)

    def get_position_and_price(self, symbol: str) -> dict[str, Any]:
        normalized = symbol.strip().upper()
        if not normalized:
            raise ValueError("symbol is required")
        position_raw = response_json_or_raise(
            self.trade_client.account_v2.get_account_position(
                self.settings.account_id.strip()
            )
        )
        quote_raw = response_json_or_raise(
            self.data_client.market_data.get_snapshot(
                normalized,
                US_STOCK_CATEGORY,
                extend_hour_required=False,
                overnight_required=False,
            )
        )
        quantity = extract_quantity(position_raw, normalized)
        last_price = extract_last_price(quote_raw, normalized)
        if not math.isfinite(last_price) or last_price <= 0:
            raise ManualToolError(f"Invalid last price returned for {normalized}")
        return {
            "environment": self.settings.environment,
            "endpoint": self.settings.endpoint,
            "symbol": normalized,
            "quantity": quantity,
            "last_price": last_price,
            "position_response": position_raw,
            "quote_response": quote_raw,
        }

    def get_account_list(self) -> Any:
        return response_json_or_raise(
            self.trade_client.account_v2.get_account_list()
        )

    def get_account_balance(self) -> Any:
        return response_json_or_raise(
            self.trade_client.account_v2.get_account_balance(
                self.settings.account_id.strip()
            )
        )

    def get_positions(self) -> Any:
        return response_json_or_raise(
            self.trade_client.account_v2.get_account_position(
                self.settings.account_id.strip()
            )
        )

    def get_open_orders(self, page_size: int = 20) -> Any:
        _validate_page_size(page_size)
        return response_json_or_raise(
            self.trade_client.order_v2.get_order_open(
                self.settings.account_id.strip(), page_size=int(page_size)
            )
        )

    def get_order_history(
        self,
        page_size: int = 20,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> Any:
        _validate_page_size(page_size)
        if start_date and end_date and start_date > end_date:
            raise ValueError("start_date cannot be after end_date")
        return response_json_or_raise(
            self.trade_client.order_v2.get_order_history(
                self.settings.account_id.strip(),
                page_size=int(page_size),
                start_date=start_date,
                end_date=end_date,
            )
        )

    def get_order_detail(self, client_order_id: str) -> Any:
        normalized = _validate_existing_client_order_id(client_order_id)
        return response_json_or_raise(
            self.trade_client.order_v2.get_order_detail(
                self.settings.account_id.strip(), normalized
            )
        )

    def cancel_order(self, client_order_id: str) -> Any:
        normalized = _validate_existing_client_order_id(client_order_id)
        return response_json_or_raise(
            self.trade_client.order_v2.cancel_order(
                self.settings.account_id.strip(), normalized
            )
        )

    def preview_market_order(self, payload: list[dict[str, str]]) -> Any:
        return response_json_or_raise(
            self.trade_client.order_v3.preview_order(
                self.settings.account_id.strip(), payload
            )
        )

    def place_market_order(self, payload: list[dict[str, str]]) -> Any:
        return response_json_or_raise(
            self.trade_client.order_v3.place_order(
                self.settings.account_id.strip(), payload
            )
        )


def _validate_page_size(page_size: int) -> None:
    if not 1 <= int(page_size) <= 100:
        raise ValueError("page_size must be between 1 and 100")


def _validate_existing_client_order_id(client_order_id: str) -> str:
    normalized = client_order_id.strip()
    if not normalized or len(normalized) > 40:
        raise ValueError("client_order_id must contain 1-40 characters")
    return normalized


def run_benchmark(
    dna_code: str,
    quantity: float,
    last_price: float,
    fix_c: float,
    p0: float,
    diff: float,
    iterations: int,
) -> dict[str, Any]:
    if not 1 <= int(iterations) <= MAX_BENCHMARK_ITERATIONS:
        raise ValueError(
            f"iterations must be between 1 and {MAX_BENCHMARK_ITERATIONS:,}"
        )
    count = int(iterations)

    start = time.perf_counter()
    for _ in range(count):
        calculate_shannon_decision(quantity, last_price, fix_c, p0, diff)
    decision_seconds = time.perf_counter() - start

    start = time.perf_counter()
    for _ in range(count):
        decode_dna(dna_code)
    dna_seconds = time.perf_counter() - start

    return {
        "iterations": count,
        "logical_fix_c": _benchmark_metrics(decision_seconds, count),
        "decode_dna": _benchmark_metrics(dna_seconds, count),
        "runtime": {
            "numpy_version": np.__version__,
        },
    }


def _benchmark_metrics(seconds: float, iterations: int) -> dict[str, float]:
    safe_seconds = max(seconds, 1e-12)
    return {
        "total_seconds": seconds,
        "mean_microseconds": seconds / iterations * 1_000_000,
        "operations_per_second": iterations / safe_seconds,
    }
