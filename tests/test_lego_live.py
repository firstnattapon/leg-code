from __future__ import annotations

from unittest.mock import Mock

import lego_live
from lego_pipeline import PreviousAnchor
from lego_store import FirestoreCollections
from manual_tools import ConnectionSettings


def test_step_zero_builds_one_snapshot_and_uses_latest_anchor_only(monkeypatch):
    client = Mock()
    client.get_account_list.return_value = {"accounts": []}
    client.get_account_balance.return_value = {"cash": "100"}
    client.get_position_and_price.return_value = {
        "quantity": 2.0,
        "last_price": 125.0,
        "position_response": {"positions": []},
        "quote_response": {"symbol": "AAPL", "price": "125"},
    }
    db = object()
    anchor = PreviousAnchor(
        row_id="latest",
        version=3,
        dna_step=7,
        price=120.0,
        p0=100.0,
        actual_cumulative=10.0,
    )
    monkeypatch.setattr(lego_live, "WebullManualClient", lambda settings: client)
    monkeypatch.setattr(lego_live, "firestore_client", lambda info: db)
    load_anchor = Mock(return_value=anchor)
    monkeypatch.setattr(lego_live, "load_previous_anchor", load_anchor)

    result = lego_live.load_step_zero_snapshot(
        ConnectionSettings("Test (UAT)", "account", "key", "secret"),
        firebase_info={"project_id": "project"},
        collections=FirestoreCollections(),
        symbol="aapl",
        dna_code="bypass:100",
        fix_c=1500,
        diff=30,
    )

    assert len(result.raw) == 1
    assert result.raw.loc[0, "symbol"] == "AAPL"
    assert result.raw.loc[0, "last_price"] == 125
    assert result.raw.loc[0, "quantity"] == 2
    assert result.context.anchor == anchor
    assert result.safe_summary["old_trade_log_reads"] == 0
    load_anchor.assert_called_once_with(db, FirestoreCollections(), result.context.chain_key)
