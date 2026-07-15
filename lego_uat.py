"""Pure redaction and sanitized audit helpers for LEGO UAT actions."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import uuid
from typing import Any

import pandas as pd


SENSITIVE_NORMALIZED_KEYS = {
    "account",
    "accountid",
    "accountno",
    "accountnumber",
    "appkey",
    "appsecret",
    "accesstoken",
    "refreshtoken",
    "token",
    "secret",
    "secretkey",
}


def _normalized_key(key: object) -> str:
    return "".join(character for character in str(key).lower() if character.isalnum())


def _is_sensitive_key(key: object) -> bool:
    normalized = _normalized_key(key)
    if normalized in SENSITIVE_NORMALIZED_KEYS:
        return True
    return normalized.endswith(
        (
            "accountid",
            "accountnumber",
            "accesstoken",
            "refreshtoken",
            "appkey",
            "appsecret",
            "secretkey",
        )
    )


def account_fingerprint(account_id: str) -> str:
    normalized = account_id.strip()
    if not normalized:
        return "missing"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def redact_payload(value: Any) -> Any:
    """Recursively redact credentials while preserving useful order fields."""

    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if _is_sensitive_key(key) else redact_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_payload(item) for item in value]
    if isinstance(value, (datetime, pd.Timestamp)):
        return value.isoformat()
    return value


def _result_status(result: Any) -> str:
    if isinstance(result, dict):
        for key in ("status", "code", "msg", "message"):
            if result.get(key) not in (None, ""):
                return str(result[key])[:120]
    return "OK"


def build_audit_event(
    *,
    action: str,
    environment: str,
    account_id: str,
    session_run_id: str,
    request_summary: dict[str, Any],
    result: Any | None,
    elapsed_ms: float,
    error: Exception | None = None,
) -> dict[str, Any]:
    """Create the only event shape permitted in the Firestore audit collection."""

    summary = redact_payload(request_summary)
    safe_result = redact_payload(result)
    order_id = None
    if isinstance(safe_result, dict):
        order_id = safe_result.get("order_id") or safe_result.get("orderId")
    return {
        "event_id": uuid.uuid4().hex,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "session_run_id": session_run_id,
        "account_fingerprint": account_fingerprint(account_id),
        "environment": environment,
        "action": action,
        "symbol": summary.get("symbol"),
        "side": summary.get("side"),
        "quantity": summary.get("quantity"),
        "client_order_id": summary.get("client_order_id"),
        "request_summary": summary,
        "result_status": _result_status(safe_result) if error is None else "ERROR",
        "order_id": order_id,
        "latency_ms": round(float(elapsed_ms), 2),
        "error_type": error.__class__.__name__ if error is not None else None,
    }
