"""Tests for the one-new-row Streamlit dashboard."""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

import lego_dashboard
from lego_dashboard import (
    LegoDashboardConfig,
    connect_and_prepare_run,
    draft_row_frame,
    final_row_frame,
)
from lego_one_row import (
    DECISION_STAGE,
    STATUS_SNAPSHOT_READY,
    compute_row,
    present_row,
)
from lego_state import InMemoryStateStore, finalize_row
from manual_tools import ConnectionSettings


class FakeWebullClient:
    """Deterministic stand-in for WebullManualClient (no network)."""

    holdings = "3"
    price = "100"

    def __init__(self, settings):
        self.settings = settings

    def get_account_list(self):
        return {"accounts": [{"account_id": "ACCT-REDACT-ME"}]}

    def get_account_balance(self):
        return {"account_id": "ACCT-REDACT-ME", "cash_balance": "5000"}

    def get_position_and_price(self, symbol):
        return {
            "position_response": {"positions": [{"symbol": symbol.upper(), "quantity": self.holdings}]},
            "quote_response": {"symbol": symbol.upper(), "last_price": self.price},
        }


@pytest.fixture
def patched_step0(monkeypatch):
    """Patch Step 0 dependencies to a fake client + a shared in-memory store."""

    store = InMemoryStateStore()
    monkeypatch.setattr(lego_dashboard, "WebullManualClient", FakeWebullClient)
    monkeypatch.setattr(lego_dashboard, "_make_firestore_client", lambda info: object())
    monkeypatch.setattr(lego_dashboard, "FirestoreStateStore", lambda db: store)
    return store


def make_config(**overrides) -> LegoDashboardConfig:
    base = dict(firebase_info={"project_id": "unit-test"}, fix_c=1500.0, diff=0.0, dna_code="bypass:100")
    base.update(overrides)
    return LegoDashboardConfig(**base)


def make_settings(environment="Test (UAT)") -> ConnectionSettings:
    return ConnectionSettings(
        environment=environment, account_id="ACCT-XYZ", app_key="k", app_secret="s", region="th"
    )


# --------------------------------------------------------------------------- #
# App loads
# --------------------------------------------------------------------------- #
def test_dashboard_loads_without_secrets_and_has_19_tabs():
    app = AppTest.from_file("lego_dashboard.py").run(timeout=30)
    assert not app.exception
    assert app.title[0].value.startswith("🧱 Webull LEGO Chain")
    assert len(app.tabs) == 19
    assert app.tabs[0].label == "0 · Snapshot + anchor"
    assert app.tabs[-1].label == "18 · Final row + append"


def test_run_buttons_disabled_before_connect():
    app = AppTest.from_file("lego_dashboard.py").run(timeout=30)
    run_buttons = [b for b in app.button if b.label.startswith("Run LEGO Step")]
    assert len(run_buttons) == 17
    assert all(b.disabled for b in run_buttons)


# --------------------------------------------------------------------------- #
# Step 0 — snapshot + anchor, no trade-log read
# --------------------------------------------------------------------------- #
def test_connect_builds_genesis_run_without_trade_log(patched_step0):
    ctx, store, computed, summary = connect_and_prepare_run(
        make_settings(), make_config(), symbol="AAPL", dna_code="bypass:100"
    )
    assert summary["old_trade_log_reads"] == 0
    assert ctx.anchor.exists is False
    assert computed.columns["DNA step"] == 0
    assert computed.columns["ราคา Pₙ (USD)"] == 100.0
    assert computed.columns["จำนวนถือครอง (หุ้น)"] == 3.0
    # gap 1500 - 300 = 1200 -> READY_BUY
    assert computed.columns["สถานะ"] == "READY_BUY"
    # No raw account id leaks into the redacted summary.
    assert "ACCT-XYZ" not in str(summary["account_fingerprint"])


def test_symbol_is_required(patched_step0):
    with pytest.raises(ValueError):
        connect_and_prepare_run(make_settings(), make_config(), symbol="", dna_code="bypass:100")


# --------------------------------------------------------------------------- #
# Step 18 — commit, idempotency, Manual/All-in equality
# --------------------------------------------------------------------------- #
def test_commit_appends_one_row_then_is_idempotent(patched_step0):
    ctx, store, computed, _ = connect_and_prepare_run(
        make_settings(), make_config(), symbol="AAPL", dna_code="bypass:100"
    )
    first = finalize_row(store, ctx, computed)
    second = finalize_row(store, ctx, computed)
    assert first.created is True
    assert second.idempotent is True
    assert len(store.rows) == 1


def test_manual_and_all_in_produce_the_same_row(patched_step0):
    settings, config = make_settings(), make_config()
    # "Manual" path — connect then commit.
    ctx_m, store, computed_m, _ = connect_and_prepare_run(settings, config, symbol="AAPL", dna_code="bypass:100")
    # "All-in" path recomputes from the same engine for the same context.
    computed_engine = compute_row(ctx_m)
    assert present_row(computed_m.columns) == present_row(computed_engine.columns)


# --------------------------------------------------------------------------- #
# Draft-row presentation
# --------------------------------------------------------------------------- #
def test_draft_status_is_snapshot_ready_before_decision_stage(patched_step0):
    ctx, store, computed, _ = connect_and_prepare_run(
        make_settings(), make_config(), symbol="AAPL", dna_code="bypass:100"
    )
    early = draft_row_frame(computed, DECISION_STAGE - 1)
    assert early["สถานะ"].iloc[0] == STATUS_SNAPSHOT_READY
    late = draft_row_frame(computed, 17)
    assert late["สถานะ"].iloc[0] == "READY_BUY"


def test_final_row_frame_has_17_columns(patched_step0):
    ctx, store, computed, _ = connect_and_prepare_run(
        make_settings(), make_config(), symbol="AAPL", dna_code="bypass:100"
    )
    frame = final_row_frame(computed)
    assert list(frame.columns) == list(lego_dashboard.FINAL_COLUMNS)
    assert len(frame) == 1


# --------------------------------------------------------------------------- #
# Order panel gating (via committed session state)
# --------------------------------------------------------------------------- #
def _seed_committed_session(app, *, environment, store, ctx, computed, config):
    app.session_state["lego_settings"] = make_settings(environment)
    app.session_state["lego_store"] = store
    app.session_state["lego_ctx"] = ctx
    app.session_state["lego_computed"] = computed
    app.session_state["lego_config"] = config
    app.session_state["lego_revealed"] = 17
    app.session_state["lego_commit_result"] = finalize_row(store, ctx, computed)
    app.session_state["lego_auth_summary"] = {"chain": {"next_dna_step": 0, "anchor_exists": False},
                                              "environment": environment,
                                              "snapshot": {"price": 100.0, "holdings": 3.0},
                                              "old_trade_log_reads": 0}


def test_production_order_panel_is_locked(patched_step0):
    config = make_config()
    ctx, store, computed, _ = connect_and_prepare_run(
        make_settings("Production"), config, symbol="AAPL", dna_code="bypass:100"
    )
    app = AppTest.from_file("lego_dashboard.py")
    _seed_committed_session(app, environment="Production", store=store, ctx=ctx, computed=computed, config=config)
    app.run(timeout=30)
    assert not app.exception
    assert any("read-only" in e.value for e in app.error)
    # No UAT submit button is rendered in Production.
    assert not any(b.label.startswith("🚀 Submit order") for b in app.button)


def test_uat_order_panel_exposes_preview_submit_query(patched_step0):
    config = make_config()
    ctx, store, computed, _ = connect_and_prepare_run(
        make_settings("Test (UAT)"), config, symbol="AAPL", dna_code="bypass:100"
    )
    app = AppTest.from_file("lego_dashboard.py")
    _seed_committed_session(app, environment="Test (UAT)", store=store, ctx=ctx, computed=computed, config=config)
    app.run(timeout=30)
    assert not app.exception
    labels = [b.label for b in app.button]
    assert "Preview order" in labels
    assert "Query order status" in labels  # lets the user verify delivery to Webull
    submit = next(b for b in app.button if b.label.startswith("🚀 Submit order"))
    # Submit stays disabled until Preview + a matching confirmation phrase.
    assert submit.disabled
