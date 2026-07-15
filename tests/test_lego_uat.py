from __future__ import annotations

import json

from lego_uat import account_fingerprint, build_audit_event, redact_payload


def test_recursive_redaction_removes_account_and_credentials_but_keeps_order_ids():
    raw = {
        "accountId": "123456789",
        "credentials": {
            "app_key": "public-looking-key",
            "appSecret": "very-secret",
            "accessToken": "token-value",
        },
        "client_order_id": "lego-order-1",
        "order_id": "broker-order-2",
        "items": [{"secAccountId": "999", "symbol": "AAPL"}],
    }

    safe = redact_payload(raw)
    serialized = json.dumps(safe)

    assert "123456789" not in serialized
    assert "public-looking-key" not in serialized
    assert "very-secret" not in serialized
    assert "token-value" not in serialized
    assert "999" not in serialized
    assert safe["client_order_id"] == "lego-order-1"
    assert safe["order_id"] == "broker-order-2"


def test_audit_event_contains_only_sanitized_contract_fields():
    event = build_audit_event(
        action="SUBMIT",
        environment="Test (UAT)",
        account_id="123456789",
        session_run_id="session-1",
        request_summary={
            "symbol": "AAPL",
            "side": "BUY",
            "quantity": 0.5,
            "client_order_id": "client-1",
            "account_id": "123456789",
        },
        result={"status": "ACCEPTED", "order_id": "order-1", "token": "x"},
        elapsed_ms=12.345,
    )

    assert event["account_fingerprint"] == account_fingerprint("123456789")
    assert event["request_summary"]["account_id"] == "[REDACTED]"
    assert event["result_status"] == "ACCEPTED"
    assert event["order_id"] == "order-1"
    assert event["latency_ms"] == 12.35
    assert "result" not in event
    assert "token" not in event
