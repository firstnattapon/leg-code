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


def test_production_mutations_are_guarded_in_source():
    source = Path("lego_dashboard.py").read_text(encoding="utf-8")

    assert 'settings.environment == "Test (UAT)"' in source
    assert "Production เป็น read-only" in source
    assert 'action="PREVIEW"' in source
    assert 'action="SUBMIT"' in source
    assert 'action="CANCEL"' in source


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
