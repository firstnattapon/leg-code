from __future__ import annotations

from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np
from pandas.testing import assert_frame_equal

import webull_lego_single_file as single
from lego_pipeline import PipelineContext, PreviousAnchor, build_snapshot_frame, final_dataframe, run_stage


def response(payload):
    value = Mock(status_code=200)
    value.json.return_value = payload
    return value


def test_encoded_dna_uses_seed_and_every_mutation_seed():
    encoded = "1822524217"
    actual, metadata = single.decode_dna(encoded)
    expected = np.random.default_rng(42).integers(0, 2, size=8).astype(np.int8)
    expected[0] = 1
    mask = np.random.default_rng(7).random(8) < 0.25
    expected[mask] = 1 - expected[mask]
    expected[0] = 1
    assert np.array_equal(actual, expected)
    assert metadata["mode"] == "encoded"


def test_bypass_formats_are_explicit_all_ones():
    for code in ("bypass:4", "[1,4]"):
        dna, metadata = single.decode_dna(code)
        assert dna.tolist() == [1, 1, 1, 1]
        assert metadata["mode"] == "bypass"


def test_real_sdk_adapter_calls_read_endpoints_for_both_environments():
    for environment, endpoint in single.ENDPOINTS.items():
        settings = single.WebullSettings(environment, "account", "key", "secret")
        account_v2 = Mock()
        market_data = Mock()
        account_v2.get_account_list.return_value = response({"accounts": []})
        account_v2.get_account_balance.return_value = response({"balance": "100"})
        account_v2.get_account_position.return_value = response({"positions": []})
        market_data.get_snapshot.return_value = response({"symbol": "AAPL", "price": "200"})
        client = single.WebullReadOnlyClient.__new__(single.WebullReadOnlyClient)
        client.settings = settings
        client.trade = SimpleNamespace(account_v2=account_v2)
        client.data = SimpleNamespace(market_data=market_data)
        assert client.account_list() == {"accounts": []}
        assert client.balance() == {"balance": "100"}
        assert client.positions() == {"positions": []}
        assert client.quote("aapl") == {"symbol": "AAPL", "price": "200"}
        assert settings.endpoint == endpoint


class Snapshot:
    exists = False

    def to_dict(self):
        return None


class Ref:
    def get(self):
        return Snapshot()


class DB:
    def collection(self, name):
        assert name in {"webull_lego_state", "webull_lego_rows"}
        return self

    def document(self, doc_id):
        return Ref()


def test_step_zero_reads_one_snapshot_and_no_trade_history(monkeypatch):
    fake = Mock()
    fake.account_list.return_value = {"accounts": []}
    fake.balance.return_value = {"cash": "100"}
    fake.positions.return_value = {
        "positions": [{"symbol": "AAPL", "positionQty": "2"}]
    }
    fake.quote.return_value = {"symbol": "AAPL", "price": "200"}
    monkeypatch.setattr(single, "WebullReadOnlyClient", lambda settings: fake)
    monkeypatch.setattr(single, "_firestore_client", lambda firebase_info: DB())

    live = single.load_live_inputs(
        single.WebullSettings("Production", "account", "key", "secret"),
        firebase_info={"project_id": "project"},
        symbol="AAPL",
        fix_c=1500,
        diff=30,
        dna_code="bypass:10",
    )

    assert len(live.raw) == 1
    assert live.raw.loc[0, "last_price"] == 200
    assert live.raw.loc[0, "quantity"] == 2
    assert live.anchor == single.PreviousAnchor()
    assert live.safe_summary["old_trade_log_reads"] == 0
    assert live.safe_summary["snapshot_rows"] == 1


def test_single_file_has_no_local_imports_or_order_mutation_methods():
    source = Path("webull_lego_single_file.py").read_text(encoding="utf-8")
    for module in (
        "lego_pipeline",
        "manual_tools",
        "trade_log",
        "lego_store",
        "lego_live",
    ):
        assert f"import {module}" not in source
        assert f"from {module}" not in source
    assert "place_order(" not in source
    assert "cancel_order(" not in source
    assert "load_firestore_rows" not in source


def test_single_file_help_runs_on_windows_legacy_console():
    completed = subprocess.run(
        [sys.executable, "webull_lego_single_file.py", "--help"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0
    assert b"Test (UAT)" in completed.stdout
    assert b"--persist" in completed.stdout


def test_redaction_removes_nested_credentials():
    safe = single._redact(
        {
            "securities_account_id": "123",
            "nested": {"appSecret": "secret", "symbol": "AAPL"},
        }
    )
    assert safe["securities_account_id"] == "[REDACTED]"
    assert safe["nested"]["appSecret"] == "[REDACTED]"


def test_single_file_final_matches_manual_stage_engine():
    raw = build_snapshot_frame(
        snapshot_at="2026-07-16T12:00:00Z",
        symbol="AAPL",
        price=120,
        holdings=10,
    )
    anchor = PreviousAnchor(
        row_id="previous",
        version=1,
        dna_step=0,
        price=100,
        p0=80,
        actual_cumulative=25,
    )
    standalone = single.run_dataframe_chain(
        raw,
        1500,
        "bypass:10",
        30,
        single.PreviousAnchor(**anchor.__dict__),
        5,
    ).final
    context = PipelineContext(
        fix_c=1500,
        diff=30,
        dna_code="bypass:10",
        anchor=anchor,
    )
    previous = None
    for number in range(1, 18):
        previous = run_stage(number, raw, previous, context)
    manual = final_dataframe(previous)
    assert_frame_equal(standalone, manual, check_dtype=False)
