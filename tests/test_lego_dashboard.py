from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
from streamlit.testing.v1 import AppTest

import lego_pipeline
from lego_pipeline import prepare_raw_frame
from manual_tools import ConnectionSettings


def test_lego_dashboard_loads_without_secrets_and_has_19_tabs():
    app = AppTest.from_file("lego_dashboard.py").run(timeout=30)

    assert not app.exception
    assert app.title[0].value == "🧱 Webull LEGO Chain"
    assert len(app.tabs) == 19
    assert app.tabs[0].label == "0 · Authenticated connection"
    assert app.tabs[-1].label == "18 · Final DataFrame"
    assert app.selectbox[0].value == "Test (UAT)"
    assert [field.value for field in app.text_input[:3]] == ["", "", ""]


def test_hot_reload_recovers_a_stale_pipeline_stage_contract(monkeypatch):
    """Reproduce Cloud loading the new app while retaining old StageSpec."""

    monkeypatch.delattr(lego_pipeline.StageSpec, "goal")
    monkeypatch.setattr(lego_pipeline, "PIPELINE_SCHEMA_VERSION", 1)

    app = AppTest.from_file("lego_dashboard.py").run(timeout=30)

    assert not app.exception
    assert any(item.value.startswith("**Goal:**") for item in app.markdown)


def test_all_17_run_buttons_are_disabled_before_authentication():
    app = AppTest.from_file("lego_dashboard.py").run(timeout=30)

    run_buttons = [
        button for button in app.button if button.label.startswith("Run LEGO Step")
    ]
    assert len(run_buttons) == 17
    assert all(button.disabled for button in run_buttons)

    all_in = next(button for button in app.button if button.label.startswith("Run ALL"))
    assert all_in.disabled


def test_first_run_unlocks_only_the_next_lego_stage():
    app = AppTest.from_file("lego_dashboard.py")
    app.session_state["lego_raw"] = prepare_raw_frame(
        pd.DataFrame(
            [
                {
                    "created_at": "2026-07-13T14:20:00Z",
                    "symbol": "AAPL",
                    "status": "PASS_THRESHOLD",
                    "dna_step": 0,
                    "dna_signal": 1,
                    "last_price": 100.0,
                    "quantity": 10.0,
                    "decision_action": "PASS",
                    "decision_reason": "WITHIN_THRESHOLD",
                    "decision_order_qty": 0.0,
                    "decision_value_now_usd": 1000.0,
                }
            ]
        )
    )
    app.session_state["lego_results"] = {}
    app.run(timeout=30)

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


def test_credential_widgets_cannot_gain_hard_coded_defaults():
    source = Path("lego_dashboard.py").read_text(encoding="utf-8")

    for label in ("Account ID", "App Key", "App Secret"):
        widget = re.search(
            rf'st\.text_input\(\s*"{re.escape(label)}",(?P<body>.*?)\n\s*\)',
            source,
            flags=re.DOTALL,
        )
        assert widget is not None
        assert re.search(r'\bvalue\s*=\s*""', widget.group("body"))


def test_order_actions_are_guarded_in_source():
    source = Path("lego_dashboard.py").read_text(encoding="utf-8")

    # The submit path is fail-closed through the shared gate + confirmation phrase.
    assert "evaluate_submit_gate" in source
    assert "order_confirmation_phrase" in source
    assert "Production safety switch" in source
    # Full Preview -> Place -> Query -> Cancel lifecycle is audited.
    assert 'action="PREVIEW"' in source
    assert 'action="SUBMIT"' in source
    assert 'action="QUERY"' in source
    assert 'action="CANCEL"' in source
    # No hard-coded real credentials may ever return to the source.
    assert "1251161573554425856" not in source


def _authenticated_app(environment: str = "Test (UAT)"):
    app = AppTest.from_file("lego_dashboard.py")
    app.session_state["lego_settings"] = ConnectionSettings(
        environment, "acct-123", "key", "secret"
    )
    app.session_state["lego_symbol"] = "AAPL"
    app.session_state["lego_results"] = {}
    return app


class _FakeClient:
    """Stand-in Webull client that acknowledges but never auto-fills an order."""

    def __init__(self, settings):
        self.settings = settings

    def preview_market_order(self, payload):
        return {"data": {"status": "OK"}}

    def place_market_order(self, payload):
        return {"data": {"orderId": "OID-1", "orderStatus": "PENDING"}}

    def get_order_detail(self, client_order_id):
        return {"data": {"orderId": "OID-1", "orderStatus": "FILLED"}}


def test_order_panel_is_locked_before_authentication():
    app = AppTest.from_file("lego_dashboard.py").run(timeout=30)

    assert not app.exception
    assert not [button for button in app.button if button.label == "Preview order"]


def test_every_tab_0_to_18_exposes_a_real_order_panel_after_auth():
    app = _authenticated_app().run(timeout=30)

    assert not app.exception
    previews = [b for b in app.button if b.label == "Preview order"]
    submits = [b for b in app.button if b.label.startswith("🚀 Submit order")]
    queries = [b for b in app.button if b.label == "Query order status"]
    assert len(previews) == 19
    assert len(submits) == 19
    assert len(queries) == 19
    # Nothing is submittable until the user previews and confirms.
    assert all(button.disabled for button in submits)


def test_uat_submit_unlocks_only_after_preview_and_confirmation(monkeypatch):
    import lego_orders
    import manual_tools

    monkeypatch.setattr(manual_tools, "WebullManualClient", _FakeClient)

    app = _authenticated_app("Test (UAT)").run(timeout=30)
    assert next(b for b in app.button if b.key == "order_auth_submit").disabled

    next(b for b in app.button if b.key == "order_auth_preview").click().run(timeout=30)
    # Preview alone is not enough — the confirmation phrase is still required.
    assert next(b for b in app.button if b.key == "order_auth_submit").disabled

    phrase = lego_orders.order_confirmation_phrase(
        "Test (UAT)", "acct-123", "BUY", "AAPL", 1.0
    )
    next(t for t in app.text_input if t.key == "order_auth_confirm").set_value(
        phrase
    ).run(timeout=30)
    assert not next(b for b in app.button if b.key == "order_auth_submit").disabled

    next(b for b in app.button if b.key == "order_auth_submit").click().run(timeout=30)
    output = app.session_state["order_auth_output"]
    assert output["action"] == "SUBMIT"
    assert output["result"]["summary"]["order_id"] == "OID-1"
    # A PENDING acknowledgement is never reported or counted as a fill.
    assert output["result"]["summary"]["is_filled"] is False
    assert output["result"]["summary"]["realized_eligible"] is False


def test_production_submit_requires_the_safety_switch(monkeypatch):
    import lego_orders
    import manual_tools

    monkeypatch.setattr(manual_tools, "WebullManualClient", _FakeClient)

    app = _authenticated_app("Production").run(timeout=30)
    next(b for b in app.button if b.key == "order_auth_preview").click().run(timeout=30)
    phrase = lego_orders.order_confirmation_phrase(
        "Production", "acct-123", "BUY", "AAPL", 1.0
    )
    next(t for t in app.text_input if t.key == "order_auth_confirm").set_value(
        phrase
    ).run(timeout=30)
    # Preview + correct phrase but the safety switch is still off -> blocked.
    assert next(b for b in app.button if b.key == "order_auth_submit").disabled

    next(c for c in app.checkbox if c.key == "order_auth_safety").set_value(True).run(
        timeout=30
    )
    assert not next(b for b in app.button if b.key == "order_auth_submit").disabled


def _completed_results():
    from lego_pipeline import PipelineContext, dataframe_fingerprint, run_stage

    raw = prepare_raw_frame(
        pd.DataFrame(
            [
                {
                    "created_at": "2026-07-13T14:20:00Z",
                    "symbol": "AAPL",
                    "status": "FILLED",
                    "dna_step": 0,
                    "dna_signal": 1,
                    "last_price": 100.0,
                    "quantity": 10.0,
                    "decision_action": "BUY",
                    "side": "BUY",
                    "decision_reason": "BELOW_TARGET",
                    "decision_order_qty": 1.5,
                    "decision_value_now_usd": 1000.0,
                }
            ]
        )
    )
    context = PipelineContext(
        fix_c=1500.0, source_hash=dataframe_fingerprint(raw), dna_code=""
    )
    results = {}
    previous = None
    for number in range(1, 18):
        results[number] = run_stage(number, raw, previous, context)
        previous = results[number]
    return raw, results


def test_all_in_sidebar_exposes_the_same_guarded_order_panel_after_chain():
    raw, results = _completed_results()

    app = AppTest.from_file("lego_dashboard.py")
    app.session_state["lego_settings"] = ConnectionSettings(
        "Test (UAT)", "acct-1", "key", "secret"
    )
    app.session_state["lego_symbol"] = "AAPL"
    app.session_state["lego_raw"] = raw
    app.session_state["lego_results"] = results
    app.run(timeout=30)

    assert not app.exception
    # The All-in section now carries its own real-order panel, fired only through
    # the same Preview + confirmation gate as the per-tab Manual Run.
    assert any(button.key == "order_allin_preview" for button in app.button)
    submit = next(b for b in app.button if b.key == "order_allin_submit")
    assert submit.disabled


def test_all_in_order_panel_is_hidden_until_the_chain_is_complete():
    app = _authenticated_app().run(timeout=30)

    assert not app.exception
    # lego_results is empty, so no all-in order panel is offered yet.
    assert not any(button.key == "order_allin_preview" for button in app.button)


class _AuditDeniedDB:
    """Firestore stand-in whose writes fail like a 403 PermissionDenied."""

    def collection(self, name):
        return self

    def document(self, doc_id):
        return self

    def set(self, event):
        raise RuntimeError("403 Missing or insufficient permissions.")


def test_order_survives_a_denied_firestore_audit_write(monkeypatch):
    import manual_tools

    monkeypatch.setattr(manual_tools, "WebullManualClient", _FakeClient)

    import types

    app = _authenticated_app("Test (UAT)")
    app.session_state["lego_db"] = _AuditDeniedDB()
    app.session_state["lego_config"] = types.SimpleNamespace(
        audit_collection="webull_lego_uat_audit", audit_to_firestore=True
    )
    app.run(timeout=30)

    next(b for b in app.button if b.key == "order_auth_preview").click().run(timeout=30)

    # The order action still produced its result despite the audit-write failure.
    assert not app.exception
    assert app.session_state["order_auth_output"]["action"] == "PREVIEW"
    # The audit event is retained in the session for download.
    assert len(app.session_state["lego_audit_events"]) == 1
    # Firestore persistence is disabled for the rest of the session (warn once).
    assert app.session_state["lego_audit_firestore_off"] is True
    # The notice makes clear the order was unaffected.
    assert any("ไม่ได้รับผลกระทบ" in warning.value for warning in app.warning)

    # A second action does not raise a second Firestore warning.
    next(b for b in app.button if b.key == "order_auth_preview").click().run(timeout=30)
    assert not any(
        "บันทึก audit ลง Firestore ไม่ได้" in warning.value for warning in app.warning
    )


def test_read_only_deployment_can_opt_out_of_firestore_audit(monkeypatch):
    import types

    import manual_tools

    monkeypatch.setattr(manual_tools, "WebullManualClient", _FakeClient)

    app = _authenticated_app("Test (UAT)")
    app.session_state["lego_db"] = _AuditDeniedDB()  # would 403 if ever written to
    app.session_state["lego_config"] = types.SimpleNamespace(
        audit_collection="webull_lego_uat_audit", audit_to_firestore=False
    )
    app.run(timeout=30)

    next(b for b in app.button if b.key == "order_auth_preview").click().run(timeout=30)

    assert not app.exception
    # The order ran and the audit is retained in session, with no Firestore write.
    assert app.session_state["order_auth_output"]["action"] == "PREVIEW"
    assert len(app.session_state["lego_audit_events"]) == 1
    assert not any("Firestore" in warning.value for warning in app.warning)


def test_audit_to_firestore_flag_parses_from_secrets():
    from lego_dashboard import LegoDashboardConfig, _coerce_bool

    # Firestore is read-only by default (matches the original dashboard/Manual).
    assert LegoDashboardConfig(firebase_info={}).audit_to_firestore is False
    assert _coerce_bool(False) is False
    assert _coerce_bool(True) is True
    assert _coerce_bool("false") is False
    assert _coerce_bool("true") is True
    assert _coerce_bool("no") is False


def test_sidebar_exposes_real_read_only_all_in_single_file():
    source = Path("lego_dashboard.py").read_text(encoding="utf-8")
    single_file = Path("webull_lego_single_file.py").read_text(encoding="utf-8")

    assert "All-in Loop 0→18" in source
    assert "Run ALL 0 → 18 (REAL READ)" in source
    assert "authenticate_and_load" in source
    assert "load_live_inputs" in source
    assert "run_all_pipeline_stages" in source
    assert "Production" in single_file
    assert "WebullReadOnlyClient" in single_file


def test_sidebar_all_in_unlocks_after_authenticated_session():
    app = AppTest.from_file("lego_dashboard.py")
    app.session_state["lego_settings"] = ConnectionSettings(
        "Production", "account", "key", "secret"
    )
    app.run(timeout=30)

    all_in = next(button for button in app.button if button.label.startswith("Run ALL"))
    assert not all_in.disabled
    assert any("Production" in item.value for item in app.info)


def test_five_step_artifacts_are_machine_readable_and_offline():
    import json

    plan = json.loads(Path("webull_lego_chain_plan.json").read_text(encoding="utf-8"))
    html = Path("webull_lego_chain_guide.html").read_text(encoding="utf-8")

    assert plan["framework"] == "5-step-process"
    assert plan["step_5_do_it"]["measurement"]["loop"]["epsilon"] == 0.01
    assert not re.search(r"\{\{[A-Z_]", html)
    assert "http://" not in html
    assert "https://" not in html
    assert "fetch(" not in html
