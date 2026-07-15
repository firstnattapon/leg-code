from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
import pandas as pd
from pandas.testing import assert_frame_equal
import webull_lego_single_file as single_file

from lego_pipeline import (
    PipelineContext,
    final_dataframe,
    prepare_raw_frame,
    run_stage,
)

from webull_lego_single_file import (
    ENDPOINTS,
    FINAL_COLUMNS,
    WebullReadOnlyClient,
    WebullSettings,
    _redact,
    decode_dna,
    parse_dna_spec,
    run_dataframe_chain,
)


def _response(payload):
    response = Mock(status_code=200)
    response.json.return_value = payload
    return response


def test_encoded_dna_uses_width_value_seed_and_every_mutation_seed():
    # Encodes [length=8, rate=25%, dna_seed=42, mutation_seed=7].
    encoded = "1822524217"
    spec = parse_dna_spec(encoded)
    actual, metadata = decode_dna(encoded)

    expected = np.random.default_rng(42).integers(0, 2, size=8).astype(np.int8)
    expected[0] = 1
    mask = np.random.default_rng(7).random(8) < 0.25
    expected[mask] = 1 - expected[mask]
    expected[0] = 1

    assert spec.length == 8
    assert spec.mutation_rate == 0.25
    assert spec.dna_seed == 42
    assert spec.mutation_seeds == (7,)
    assert np.array_equal(actual, expected)
    assert metadata["mode"] == "encoded"
    assert metadata["seeds"] == [42, 7]


def test_bypass_formats_are_explicit_all_ones_sequences():
    for code in ("bypass:4", "[1,4]"):
        dna, metadata = decode_dna(code)
        assert dna.tolist() == [1, 1, 1, 1]
        assert metadata["mode"] == "bypass"


def test_chain_prefers_logged_signal_then_decodes_only_missing_rows():
    raw = pd.DataFrame(
        [
            {"created_at": "2026-01-01T00:00:00Z", "dna_step": 0, "dna_signal": 0},
            {"created_at": "2026-01-01T00:05:00Z", "dna_step": 1},
            {"created_at": "2026-01-01T00:10:00Z", "dna_step": 1, "dna_signal": 2},
        ]
    )
    result = run_dataframe_chain(raw, 1500.0, "bypass:2")

    chronological = result.accumulated[5]["DNA signal"].tolist()
    assert chronological[:2] == [0, 1]
    assert pd.isna(chronological[2])
    assert tuple(result.final.columns) == FINAL_COLUMNS


def test_real_sdk_adapter_calls_read_endpoints_for_test_and_production():
    for environment, endpoint in ENDPOINTS.items():
        settings = WebullSettings(environment, "account", "key", "secret")
        account_v2 = Mock()
        market_data = Mock()
        account_v2.get_account_list.return_value = _response({"accounts": []})
        account_v2.get_account_balance.return_value = _response({"balance": "100"})
        account_v2.get_account_position.return_value = _response({"positions": []})
        market_data.get_snapshot.return_value = _response({"symbol": "AAPL", "price": "200"})
        client = WebullReadOnlyClient.__new__(WebullReadOnlyClient)
        client.settings = settings
        client.trade = SimpleNamespace(account_v2=account_v2)
        client.data = SimpleNamespace(market_data=market_data)

        assert client.account_list() == {"accounts": []}
        assert client.balance() == {"balance": "100"}
        assert client.positions() == {"positions": []}
        assert client.quote("aapl") == {"symbol": "AAPL", "price": "200"}
        assert settings.endpoint == endpoint
        account_v2.get_account_balance.assert_called_once_with("account")
        account_v2.get_account_position.assert_called_once_with("account")
        market_data.get_snapshot.assert_called_once_with(
            "AAPL",
            "US_STOCK",
            extend_hour_required=False,
            overnight_required=False,
        )


def test_live_step_zero_orchestrates_every_real_read_contract(monkeypatch):
    fake = Mock()
    fake.account_list.return_value = {"account_id": "secret-account"}
    fake.balance.return_value = {"cash": "100"}
    fake.positions.return_value = {"positions": [{"symbol": "AAPL", "quantity": "2"}]}
    fake.quote.return_value = {"symbol": "AAPL", "price": "200"}
    db = object()
    raw = pd.DataFrame([{"symbol": "AAPL", "created_at": "2026-01-01T00:00:00Z"}])
    monkeypatch.setattr(single_file, "WebullReadOnlyClient", lambda settings: fake)
    monkeypatch.setattr(single_file, "_firestore_client", lambda firebase_info: db)
    monkeypatch.setattr(
        single_file,
        "load_firestore_rows",
        lambda actual_db, collection, limit: raw,
    )

    live = single_file.load_live_inputs(
        WebullSettings("Production", "account", "key", "secret"),
        firebase_info={"project_id": "project"},
        collection="trades",
        limit=10,
    )

    fake.account_list.assert_called_once_with()
    fake.balance.assert_called_once_with()
    fake.positions.assert_called_once_with()
    fake.quote.assert_called_once_with("AAPL")
    assert live.firestore_client is db
    assert live.raw.equals(raw)
    assert live.safe_summary["environment"] == "Production"
    assert live.safe_summary["api_reads"] == [
        "account_list", "account_balance", "positions", "market_snapshot"
    ]
    assert live.safe_summary["account_list"]["account_id"] == "[REDACTED]"


def test_single_file_has_no_project_local_imports_or_mutation_methods():
    source = Path("webull_lego_single_file.py").read_text(encoding="utf-8")

    for local_module in ("lego_pipeline", "manual_tools", "trade_log", "lego_uat"):
        assert f"import {local_module}" not in source
        assert f"from {local_module}" not in source
    assert "place_order(" not in source
    assert "cancel_order(" not in source
    assert "get_account_list" in source
    assert "get_account_balance" in source
    assert "get_account_position" in source
    assert "get_snapshot" in source


def test_single_file_help_runs_on_windows_legacy_console():
    completed = subprocess.run(
        [sys.executable, "webull_lego_single_file.py", "--help"],
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0
    assert b"Test (UAT)" in completed.stdout
    assert b"Production" in completed.stdout


def test_single_file_redacts_nested_account_and_credentials():
    safe = _redact(
        {
            "securities_account_id": "123",
            "account_no": "456",
            "nested": {"appSecret": "secret", "symbol": "AAPL"},
        }
    )

    assert safe["securities_account_id"] == "[REDACTED]"
    assert safe["account_no"] == "[REDACTED]"
    assert safe["nested"] == {"appSecret": "[REDACTED]", "symbol": "AAPL"}


def test_single_file_final_contract_matches_the_manual_stage_engine():
    raw = pd.DataFrame(
        [
            {
                "created_at": "2026-01-01T00:00:00Z",
                "symbol": "aapl",
                "status": "ORDER_FILLED",
                "dna_step": 0,
                "dna_signal": 1,
                "last_price": 100.0,
                "quantity": 10.0,
                "decision_action": "SELL",
                "decision_side": "SELL",
                "decision_reason": "ABOVE_TARGET",
                "decision_order_qty": 1.0,
                "decision_value_now_usd": 1000.0,
                "client_order_id": "order-1",
                "filled_quantity": 1.0,
                "execution_price": 101.0,
                "position_reconciled": True,
                "transaction_fee": 0.25,
            }
        ]
    )
    single = run_dataframe_chain(raw, 1000.0, "bypass:1").final

    prepared = prepare_raw_frame(raw)
    context = PipelineContext(fix_c=1000.0, dna_code="bypass:1")
    previous = None
    for step in range(1, 18):
        previous = run_stage(step, prepared, previous, context)
    manual = final_dataframe(previous)

    assert_frame_equal(single, manual, check_dtype=False)
