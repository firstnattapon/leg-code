"""Pure order-submission logic shared by every LEGO tab.

This module never touches Streamlit, the network, or credentials on disk.  It
only turns reviewed inputs into a deterministic confirmation contract and turns
raw Webull responses into a safe, non-misleading status summary, so the UI in
``lego_dashboard`` and the tests can reason about order safety without a broker
connection.

Design rules encoded here:

* A live submit is only ever *allowed* by :func:`evaluate_submit_gate`, which is
  fail-closed: every missing safeguard is reported and any one of them blocks.
* Production adds a mandatory safety switch on top of the Preview + typed
  confirmation phrase that UAT already requires.  The phrase itself re-states the
  environment, side, symbol, quantity, and a non-reversible account fingerprint
  so the user must re-verify the account before a real-money order.
* A broker ``SUBMITTED``/``PENDING`` acknowledgement is never reported as
  ``FILLED``.  Only an explicit filled status is eligible to be called realized.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lego_uat import account_fingerprint
from manual_tools import first_value, format_order_quantity, iter_dicts


UAT_ENVIRONMENT = "Test (UAT)"
PRODUCTION_ENVIRONMENT = "Production"


# Canonical order-status categories.  Anything the broker reports that is not a
# known alias stays ``UNKNOWN`` so the UI shows the raw text and never guesses.
FILLED = "FILLED"
PARTIALLY_FILLED = "PARTIALLY_FILLED"
PENDING = "PENDING"
SUBMITTED = "SUBMITTED"
WORKING = "WORKING"
CANCELLED = "CANCELLED"
REJECTED = "REJECTED"
FAILED = "FAILED"
EXPIRED = "EXPIRED"
UNKNOWN = "UNKNOWN"

TERMINAL_CATEGORIES = frozenset({FILLED, CANCELLED, REJECTED, FAILED, EXPIRED})

_STATUS_ALIASES: dict[str, str] = {
    "FILLED": FILLED,
    "FULL_FILLED": FILLED,
    "FULLY_FILLED": FILLED,
    "ALL_FILLED": FILLED,
    "COMPLETE": FILLED,
    "COMPLETED": FILLED,
    "PARTIAL_FILLED": PARTIALLY_FILLED,
    "PARTIALLY_FILLED": PARTIALLY_FILLED,
    "PARTIAL": PARTIALLY_FILLED,
    "PENDING": PENDING,
    "PENDING_SUBMIT": PENDING,
    "PENDING_NEW": PENDING,
    "PENDING_CANCEL": PENDING,
    "CANCELLING": PENDING,
    "CANCELING": PENDING,
    "QUEUED": PENDING,
    "SUBMITTED": SUBMITTED,
    "NEW": SUBMITTED,
    "ACCEPTED": SUBMITTED,
    "RECEIVED": SUBMITTED,
    "WORKING": WORKING,
    "OPEN": WORKING,
    "LIVE": WORKING,
    "CANCELLED": CANCELLED,
    "CANCELED": CANCELLED,
    "PARTIAL_CANCELLED": CANCELLED,
    "PARTIAL_CANCELED": CANCELLED,
    "REJECTED": REJECTED,
    "DENIED": REJECTED,
    "FAILED": FAILED,
    "FAIL": FAILED,
    "ERROR": FAILED,
    "EXPIRED": EXPIRED,
}

_ORDER_ID_FIELDS = ("order_id", "orderId", "orderNo", "order_no")
_CLIENT_ORDER_ID_FIELDS = (
    "client_order_id",
    "clientOrderId",
    "client_order_no",
    "clientOrderNo",
)
_STATUS_FIELDS = (
    "order_status",
    "orderStatus",
    "status",
    "state",
    "order_state",
    "orderState",
)


def classify_status(status_text: Any) -> str:
    """Map a raw broker status onto a canonical category (fail to ``UNKNOWN``)."""

    text = str(status_text or "").strip().upper().replace("-", "_").replace(" ", "_")
    if not text:
        return UNKNOWN
    return _STATUS_ALIASES.get(text, UNKNOWN)


def is_filled_status(status_text: Any) -> bool:
    """Only an explicit filled status is ever eligible to be called realized."""

    return classify_status(status_text) == FILLED


def is_terminal_status(status_text: Any) -> bool:
    return classify_status(status_text) in TERMINAL_CATEGORIES


def order_confirmation_phrase(
    environment: str,
    account_id: str,
    side: str,
    symbol: str,
    quantity: float,
) -> str:
    """Build the exact phrase the user must retype before a live submit.

    UAT: ``PLACE UAT BUY AAPL 1.5``.  Production adds the account fingerprint so
    the account itself has to be re-verified: ``PLACE PROD BUY AAPL 1.5 ACCT
    <fingerprint>``.  The raw account id is never placed in the phrase.
    """

    token = "PROD" if environment == PRODUCTION_ENVIRONMENT else "UAT"
    normalized_side = str(side).strip().upper()
    normalized_symbol = str(symbol).strip().upper()
    quantity_text = format_order_quantity(float(quantity))
    base = f"PLACE {token} {normalized_side} {normalized_symbol} {quantity_text}"
    if environment == PRODUCTION_ENVIRONMENT:
        return f"{base} ACCT {account_fingerprint(account_id)}"
    return base


@dataclass(frozen=True)
class SubmitGate:
    """Fail-closed decision for whether a live submit may proceed."""

    allowed: bool
    reasons: tuple[str, ...]


def evaluate_submit_gate(
    *,
    environment: str,
    payload_valid: bool,
    preview_matches: bool,
    confirmation_ok: bool,
    safety_switch: bool,
) -> SubmitGate:
    """Return every reason a submit is blocked; ``allowed`` only when none remain."""

    reasons: list[str] = []
    if not payload_valid:
        reasons.append("payload ยังไม่ผ่าน validation")
    if not preview_matches:
        reasons.append("ต้อง Preview payload เดิมก่อน Submit")
    if not confirmation_ok:
        reasons.append("confirmation phrase ยังไม่ตรง")
    if environment == PRODUCTION_ENVIRONMENT and not safety_switch:
        reasons.append("Production ต้องเปิด safety switch")
    return SubmitGate(allowed=not reasons, reasons=tuple(reasons))


def summarize_order_result(result: Any) -> dict[str, Any]:
    """Extract order id / status from an arbitrary Webull response, safely.

    Never claims a fill: ``is_filled`` is only true for an explicit filled
    status, and ``realized_eligible`` mirrors it so pending/rejected/submitted
    acknowledgements can never be counted as realized cash by a caller.
    """

    order_id: Any = None
    client_order_id: Any = None
    status: Any = None
    for node in iter_dicts(result):
        if order_id in (None, ""):
            order_id = first_value(node, *_ORDER_ID_FIELDS)
        if client_order_id in (None, ""):
            client_order_id = first_value(node, *_CLIENT_ORDER_ID_FIELDS)
        if status in (None, ""):
            status = first_value(node, *_STATUS_FIELDS)
    category = classify_status(status)
    return {
        "order_id": order_id,
        "client_order_id": client_order_id,
        "status": status,
        "status_category": category,
        "is_filled": category == FILLED,
        "is_terminal": category in TERMINAL_CATEGORIES,
        "realized_eligible": category == FILLED,
    }
