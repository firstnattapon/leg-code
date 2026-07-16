from __future__ import annotations

from lego_orders import (
    PRODUCTION_ENVIRONMENT,
    UAT_ENVIRONMENT,
    classify_status,
    evaluate_submit_gate,
    is_filled_status,
    is_terminal_status,
    order_confirmation_phrase,
    summarize_order_result,
)
from lego_uat import account_fingerprint


def test_pending_and_submitted_are_never_classified_as_filled():
    for status in ("SUBMITTED", "PENDING", "Working", "queued", "accepted", "partial_filled"):
        assert not is_filled_status(status)
    assert is_filled_status("FILLED")
    assert is_filled_status("fully_filled")
    assert classify_status("PARTIAL_FILLED") == "PARTIALLY_FILLED"


def test_unknown_or_blank_status_stays_unknown_and_not_filled():
    for status in ("banana", "", None, "   "):
        assert classify_status(status) == "UNKNOWN"
        assert not is_filled_status(status)
        assert not is_terminal_status(status)


def test_terminal_statuses_are_flagged_without_implying_a_fill():
    assert is_terminal_status("REJECTED")
    assert is_terminal_status("CANCELLED")
    assert is_terminal_status("FILLED")
    assert not is_filled_status("REJECTED")
    assert not is_terminal_status("PENDING")


def test_confirmation_phrase_uat_vs_production_reverification():
    uat = order_confirmation_phrase(UAT_ENVIRONMENT, "acct-1", "buy", "aapl", 1.5)
    assert uat == "PLACE UAT BUY AAPL 1.5"

    prod = order_confirmation_phrase(PRODUCTION_ENVIRONMENT, "acct-1", "SELL", "AAPL", 2)
    assert prod == f"PLACE PROD SELL AAPL 2 ACCT {account_fingerprint('acct-1')}"
    # The raw account id is re-verified only via its fingerprint, never in clear.
    assert "acct-1" not in prod


def test_submit_gate_is_fail_closed_and_reports_every_missing_safeguard():
    blocked = evaluate_submit_gate(
        environment=PRODUCTION_ENVIRONMENT,
        payload_valid=False,
        preview_matches=False,
        confirmation_ok=False,
        safety_switch=False,
    )
    assert not blocked.allowed
    assert len(blocked.reasons) == 4

    # Production stays blocked without the safety switch even if all else passes.
    prod_no_switch = evaluate_submit_gate(
        environment=PRODUCTION_ENVIRONMENT,
        payload_valid=True,
        preview_matches=True,
        confirmation_ok=True,
        safety_switch=False,
    )
    assert not prod_no_switch.allowed

    # UAT needs no safety switch.
    uat_ready = evaluate_submit_gate(
        environment=UAT_ENVIRONMENT,
        payload_valid=True,
        preview_matches=True,
        confirmation_ok=True,
        safety_switch=False,
    )
    assert uat_ready.allowed

    prod_ready = evaluate_submit_gate(
        environment=PRODUCTION_ENVIRONMENT,
        payload_valid=True,
        preview_matches=True,
        confirmation_ok=True,
        safety_switch=True,
    )
    assert prod_ready.allowed


def test_summarize_extracts_ids_and_never_marks_pending_realized():
    pending = summarize_order_result(
        {"data": {"orderId": "O1", "clientOrderId": "C1", "orderStatus": "PENDING"}}
    )
    assert pending["order_id"] == "O1"
    assert pending["client_order_id"] == "C1"
    assert pending["status_category"] == "PENDING"
    assert pending["is_filled"] is False
    assert pending["realized_eligible"] is False

    filled = summarize_order_result([{"order_id": "O2", "status": "Filled"}])
    assert filled["order_id"] == "O2"
    assert filled["is_filled"] is True
    assert filled["realized_eligible"] is True
