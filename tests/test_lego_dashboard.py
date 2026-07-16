from __future__ import annotations

from pathlib import Path
import re

from streamlit.testing.v1 import AppTest

import lego_pipeline
from lego_pipeline import (
    PipelineContext,
    PreviousAnchor,
    build_snapshot_frame,
    run_stage,
)
from lego_store import PersistResult
from manual_tools import ConnectionSettings


def completed_results(
    *,
    environment: str = "Test (UAT)",
    price: float = 100.0,
    holdings: float = 10.0,
):
    raw = build_snapshot_frame(
        snapshot_at="2026-07-16T12:00:00Z",
        symbol="AAPL",
        price=price,
        holdings=holdings,
    )
    context = PipelineContext(
        fix_c=1500,
        diff=30,
        dna_code="bypass:100",
        source_hash=lego_pipeline.dataframe_fingerprint(raw),
        run_id="run-final",
        chain_key="chain-final",
        anchor=PreviousAnchor(),
    )
    results = {}
    previous = None
    for number in range(1, 18):
        previous = run_stage(number, raw, previous, context)
        results[number] = previous
    settings = ConnectionSettings(environment, "acct-123", "key", "secret")
    return raw, context, results, settings


def app_with_step_zero():
    raw = build_snapshot_frame(
        snapshot_at="2026-07-16T12:00:00Z",
        symbol="AAPL",
        price=100,
        holdings=10,
    )
    context = PipelineContext(
        fix_c=1500,
        diff=30,
        dna_code="bypass:100",
        source_hash=lego_pipeline.dataframe_fingerprint(raw),
        run_id="run-draft",
        chain_key="chain-draft",
    )
    app = AppTest.from_file("lego_dashboard.py")
    app.session_state["lego_raw"] = raw
    app.session_state["lego_context"] = context
    app.session_state["lego_results"] = {}
    return app


def app_with_persisted_final(environment: str = "Test (UAT)", **kwargs):
    raw, context, results, settings = completed_results(
        environment=environment, **kwargs
    )
    app = AppTest.from_file("lego_dashboard.py")
    app.session_state["lego_raw"] = raw
    app.session_state["lego_context"] = context
    app.session_state["lego_results"] = results
    app.session_state["lego_settings"] = settings
    app.session_state["lego_final_persisted"] = PersistResult(
        run_id=context.run_id,
        chain_key=context.chain_key,
        version=1,
        created=True,
    )
    app.session_state["lego_final_document"] = {"run_id": context.run_id}
    return app


class FakeClient:
    def __init__(self, settings):
        self.settings = settings

    def preview_market_order(self, payload):
        return {"data": {"status": "OK"}}

    def place_market_order(self, payload):
        return {"data": {"orderId": "OID-1", "orderStatus": "PENDING"}}


def test_dashboard_loads_without_secrets_and_has_19_tabs():
    app = AppTest.from_file("lego_dashboard.py").run(timeout=30)
    assert not app.exception
    assert app.title[0].value == "🧱 Webull LEGO Chain"
    assert len(app.tabs) == 19
    assert app.tabs[0].label == "0 · Authenticated connection"
    assert app.tabs[-1].label == "18 · Final DataFrame"
    assert app.selectbox[0].value == "Test (UAT)"
    assert [field.value for field in app.text_input[:3]] == ["", "", ""]


def test_hot_reload_recovers_stale_pipeline_contract(monkeypatch):
    monkeypatch.delattr(lego_pipeline.StageSpec, "goal")
    monkeypatch.setattr(lego_pipeline, "PIPELINE_SCHEMA_VERSION", 1)
    app = AppTest.from_file("lego_dashboard.py").run(timeout=30)
    assert not app.exception
    assert any(item.value.startswith("**Goal:**") for item in app.markdown)


def test_stages_are_locked_before_step_zero():
    app = AppTest.from_file("lego_dashboard.py").run(timeout=30)
    run_buttons = [
        button for button in app.button if button.label.startswith("Run LEGO Step")
    ]
    assert len(run_buttons) == 17
    assert all(button.disabled for button in run_buttons)


def test_first_stage_unlocks_then_only_next_stage_unlocks():
    app = app_with_step_zero().run(timeout=30)
    run_buttons = [
        button for button in app.button if button.label.startswith("Run LEGO Step")
    ]
    assert not run_buttons[0].disabled
    assert all(button.disabled for button in run_buttons[1:])

    run_buttons[0].click().run(timeout=30)
    run_buttons = [
        button for button in app.button if button.label.startswith("Run LEGO Step")
    ]
    assert not run_buttons[0].disabled
    assert not run_buttons[1].disabled
    assert all(button.disabled for button in run_buttons[2:])
    assert len(app.session_state["lego_results"][1].frame) == 1


def test_credentials_never_gain_hard_coded_defaults():
    source = Path("lego_dashboard.py").read_text(encoding="utf-8")
    for label in ("Account ID", "App Key", "App Secret"):
        widget = re.search(
            rf'st\.text_input\(\s*"{re.escape(label)}",(?P<body>.*?)\n\s*\)',
            source,
            flags=re.DOTALL,
        )
        assert widget is not None
        assert re.search(r'\bvalue\s*=\s*""', widget.group("body"))


def test_no_order_panel_before_step18_persistence():
    app = app_with_step_zero().run(timeout=30)
    assert not any(button.key == "order_final_preview" for button in app.button)
    assert not any(button.key == "order_final_submit" for button in app.button)


def test_completed_unpersisted_row_shows_finalize_not_order():
    raw, context, results, settings = completed_results()
    app = AppTest.from_file("lego_dashboard.py")
    app.session_state["lego_raw"] = raw
    app.session_state["lego_context"] = context
    app.session_state["lego_results"] = results
    app.session_state["lego_settings"] = settings
    app.run(timeout=30)
    assert any(button.key == "lego_finalize_button" for button in app.button)
    assert not any(button.key == "order_final_preview" for button in app.button)


def test_uat_order_unlocks_only_after_preview_and_phrase(monkeypatch):
    import lego_orders
    import manual_tools

    monkeypatch.setattr(manual_tools, "WebullManualClient", FakeClient)
    app = app_with_persisted_final("Test (UAT)").run(timeout=30)
    preview = next(button for button in app.button if button.key == "order_final_preview")
    submit = next(button for button in app.button if button.key == "order_final_submit")
    assert submit.disabled

    preview.click().run(timeout=30)
    submit = next(button for button in app.button if button.key == "order_final_submit")
    assert submit.disabled
    phrase = lego_orders.order_confirmation_phrase(
        "Test (UAT)", "acct-123", "BUY", "AAPL", 5.0
    )
    next(
        field for field in app.text_input if field.key == "order_final_confirm"
    ).set_value(phrase).run(timeout=30)
    submit = next(button for button in app.button if button.key == "order_final_submit")
    assert not submit.disabled
    submit.click().run(timeout=30)
    output = app.session_state["order_final_output"]
    assert output["action"] == "SUBMIT"
    assert output["summary"]["status_category"] == "PENDING"
    assert output["summary"]["realized_eligible"] is False


def test_production_is_read_only_after_persist():
    app = app_with_persisted_final("Production").run(timeout=30)
    assert not any(button.key == "order_final_preview" for button in app.button)
    assert any("Production เป็น read-only" in item.value for item in app.error)


def test_pass_row_never_exposes_submit():
    app = app_with_persisted_final(
        "Test (UAT)",
        price=100.0,
        holdings=15.1,
    ).run(timeout=30)
    assert not any(button.key == "order_final_preview" for button in app.button)
    assert any("PASS_THRESHOLD" in item.value for item in app.info)


def test_source_has_one_row_and_no_legacy_trade_reader():
    source = Path("lego_dashboard.py").read_text(encoding="utf-8")
    live_source = Path("lego_live.py").read_text(encoding="utf-8")
    assert "load_step_zero_snapshot" in source
    assert "Finalize Step 18 + Append New Row" in source
    assert "Run ALL 0 → 18 (NEW ROW)" in source
    assert "load_firestore_rows" not in source
    assert "shannon_demon_trades" not in live_source


def test_all_in_unlocks_after_step_zero_session():
    app = AppTest.from_file("lego_dashboard.py")
    app.session_state["lego_settings"] = ConnectionSettings(
        "Production", "account", "key", "secret"
    )
    app.run(timeout=30)
    all_in = next(button for button in app.button if button.label.startswith("Run ALL"))
    assert not all_in.disabled


def test_five_step_prompt_is_complete_json():
    import json

    prompt = json.loads(
        Path("webull_dashboard_overhaul_five_step_prompt.json").read_text(
            encoding="utf-8"
        )
    )
    assert prompt["framework"] == "5-step-process"
    assert len(prompt["step_2_problems"]) == 5
    assert prompt["step_5_do_it"]["measurement"]["loop"]["epsilon"] == 0
