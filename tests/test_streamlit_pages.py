from __future__ import annotations

import re
from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_dashboard_loads_without_firestore_secrets():
    app = AppTest.from_file("streamlit_dashboard.py").run(timeout=30)

    assert not app.exception
    assert app.title[0].value == "Shannon Demon Dashboard"
    assert app.info


def test_manual_page_loads_with_uat_and_blank_credentials():
    app = AppTest.from_file("pages/Manual.py").run(timeout=30)

    assert not app.exception
    assert app.title[0].value == "🧪 Manual Test Lab"
    assert [tab.label for tab in app.tabs] == [
        "Connection / Quote",
        "Order Test",
        "Account / Orders",
        "DNA",
        "Logical FIX_C",
        "Rebalancing 101",
        "🌐 Web Apps",
        "⚡ Cheat Sheet",
        "Benchmark",
    ]
    assert app.radio[0].value == "Rebalancing 101"
    assert app.sidebar.selectbox[0].value == "Test (UAT)"
    assert app.sidebar.text_input[0].value == ""
    assert app.sidebar.text_input[1].value == ""
    assert app.sidebar.text_input[2].value == ""

    app.radio[0].set_value("Rebalancing Playground").run(timeout=30)
    assert not app.exception
    assert app.radio[0].value == "Rebalancing Playground"


def test_manual_source_cannot_regress_to_hard_coded_credential_defaults():
    source = Path("pages/Manual.py").read_text(encoding="utf-8")

    for label in ("Account ID", "App Key", "App Secret"):
        widget = re.search(
            rf'st\.text_input\(\s*"{re.escape(label)}",(?P<body>.*?)\n\s*\)',
            source,
            flags=re.DOTALL,
        )
        assert widget is not None
        assert re.search(r'\bvalue\s*=\s*""', widget.group("body"))


def test_cheat_sheet_is_a_self_contained_local_asset():
    html = Path("web_apps/cheat_sheet.html").read_text(encoding="utf-8")

    assert "<title>Cheat Sheet — รู้ทันสมการ Rebalancing</title>" in html
    assert 'id="decision-form"' in html
    assert 'id="path-form"' in html
    assert "V = quantity × price" in html
    assert "Rₙ = FIX_C × ln(Pₙ/P₀)" in html
    assert "Eₙ = Aₙ − Rₙ" in html
    assert "MAX_PATH_POINTS = 100" in html
    assert "window.CheatSheetMath" in html
    assert "@media (max-width: 560px)" in html
    assert "@media (prefers-color-scheme: dark)" in html
    assert "@media (prefers-reduced-motion: reduce)" in html
    assert "http://" not in html
    assert "https://" not in html
    assert "fetch(" not in html
    assert "localStorage" not in html


def test_clear_credentials_callback_clears_all_secret_widgets():
    app = AppTest.from_file("pages/Manual.py").run(timeout=30)
    app.sidebar.text_input[0].set_value("account")
    app.sidebar.text_input[1].set_value("key")
    app.sidebar.text_input[2].set_value("secret")
    app.run(timeout=30)

    app.sidebar.button[0].click().run(timeout=30)

    assert not app.exception
    assert [field.value for field in app.sidebar.text_input[:3]] == ["", "", ""]
