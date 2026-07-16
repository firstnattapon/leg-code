"""Webull LEGO Chain — a manually-run, 17-stage Streamlit learning app."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import importlib
import json
from pathlib import Path
import time
import uuid
from typing import Any, Callable

import pandas as pd
import streamlit as st
from google.cloud import firestore

# Streamlit Cloud can hot-reload this entry point without evicting an older
# lego_pipeline module.  Reload it before importing names when the StageSpec
# contract is stale, so ``stage.goal/source_code/file_name`` always move as one
# compatible unit.
import lego_pipeline as _lego_pipeline

EXPECTED_PIPELINE_SCHEMA_VERSION = 3
if (
    getattr(_lego_pipeline, "PIPELINE_SCHEMA_VERSION", 0)
    != EXPECTED_PIPELINE_SCHEMA_VERSION
):
    _lego_pipeline = importlib.reload(_lego_pipeline)

from lego_pipeline import (
    FINAL_COLUMNS,
    INTERNAL_ROW_ID,
    PipelineContext,
    STAGES,
    StageResult,
    dataframe_fingerprint,
    final_dataframe,
    invalidate_from,
    prepare_raw_frame,
    run_stage,
    what_if_dataframe,
)
from manual_tools import (
    WEBULL_ENDPOINTS,
    ConnectionSettings,
    WebullManualClient,
    build_market_order_payload,
    generate_client_order_id,
)
from lego_uat import (
    account_fingerprint,
    build_audit_event,
    redact_payload,
)
from lego_orders import (
    PRODUCTION_ENVIRONMENT,
    evaluate_submit_gate,
    order_confirmation_phrase,
    summarize_order_result,
)
from webull_lego_single_file import (
    WebullSettings,
    decode_dna as decode_guide_dna,
    load_live_inputs,
)


st.set_page_config(
    page_title="Webull LEGO Chain",
    page_icon="🧱",
    layout="wide",
)


@dataclass(frozen=True)
class LegoDashboardConfig:
    firebase_info: dict[str, Any]
    trade_collection: str = "shannon_demon_trades"
    audit_collection: str = "webull_lego_uat_audit"
    trade_limit: int = 100
    fix_c: float = 1500.0
    # Firestore is read-only by default, exactly like the rest of the app (the
    # original dashboard/Manual only ever `.get()` the trade log).  Audit stays
    # session-only and downloadable; opt in to Firestore writes only when the
    # service account actually has write permission on the audit collection.
    audit_to_firestore: bool = False


def _secret_section(name: str) -> dict[str, Any]:
    try:
        return dict(st.secrets[name])
    except (KeyError, FileNotFoundError, TypeError):
        return {}


def load_dashboard_config() -> LegoDashboardConfig:
    firebase_info = _secret_section("firebase_service_account")
    lego = _secret_section("lego_dashboard")
    try:
        trade_limit = max(1, min(1000, int(lego.get("trade_limit", 100))))
        fix_c = float(lego.get("fix_c", 1500.0))
    except (TypeError, ValueError) as exc:
        raise ValueError("lego_dashboard.trade_limit/fix_c must be numeric") from exc
    if fix_c <= 0:
        raise ValueError("lego_dashboard.fix_c must be greater than 0")
    return LegoDashboardConfig(
        firebase_info=firebase_info,
        trade_collection=str(
            lego.get("trade_collection", "shannon_demon_trades")
        ).strip(),
        audit_collection=str(
            lego.get("audit_collection", "webull_lego_uat_audit")
        ).strip(),
        trade_limit=trade_limit,
        fix_c=fix_c,
        audit_to_firestore=_coerce_bool(lego.get("audit_to_firestore", False)),
    )


def _coerce_bool(value: Any) -> bool:
    """Accept native TOML booleans and common string spellings."""

    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def connection_fingerprint(
    settings: ConnectionSettings, *, symbol: str = "", dna_code: str = ""
) -> str:
    payload = "\x00".join(
        (
            settings.environment,
            settings.account_id,
            settings.app_key,
            settings.app_secret,
            settings.region,
            symbol.strip().upper(),
            dna_code.strip(),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def make_audit_event(
    *,
    action: str,
    settings: ConnectionSettings,
    request_summary: dict[str, Any],
    result: Any | None,
    elapsed_ms: float,
    error: Exception | None = None,
) -> dict[str, Any]:
    return build_audit_event(
        action=action,
        environment=settings.environment,
        account_id=settings.account_id,
        session_run_id=st.session_state.get("lego_session_run_id", "unknown"),
        request_summary=request_summary,
        result=result,
        elapsed_ms=elapsed_ms,
        error=error,
    )


def record_audit(event: dict[str, Any]) -> str | None:
    """Write sanitized audit best-effort and always keep a session download copy.

    The session copy (``lego_audit_events``) is the source of truth and is always
    retained for download.  Persisting to Firestore is a bonus: if the service
    account lacks write permission on the audit collection it fails **once**, we
    fall back to session-only for the rest of the session, and — crucially — the
    order API result is never affected or hidden.
    """

    st.session_state.setdefault("lego_audit_events", []).append(event)
    # Session-only audit is a valid, non-error state (kept + downloadable).
    if st.session_state.get("lego_audit_firestore_off"):
        return None
    db = st.session_state.get("lego_db")
    config = st.session_state.get("lego_config")
    if db is None or config is None:
        return None
    # Read-only deployments opt out entirely: session audit only, no write, no noise.
    if not getattr(config, "audit_to_firestore", True):
        return None
    try:
        db.collection(config.audit_collection).document(event["event_id"]).set(event)
    except Exception as exc:  # audit failure must not hide an API result
        # Stop retrying this session so the notice appears once, not per action.
        st.session_state["lego_audit_firestore_off"] = True
        return (
            "บันทึก audit ลง Firestore ไม่ได้ — order **ไม่ได้รับผลกระทบ** (ดูผลด้านบน) และ "
            "audit ถูกเก็บใน session แล้ว ดาวน์โหลดได้ที่ Tab 18. เหตุผล: "
            f"{exc.__class__.__name__}: {exc} · แก้โดยให้ service account เขียน "
            f"collection `{config.audit_collection}` ได้ หรือปรับ Firestore security rules"
        )
    return None


def clear_connection_state(*, clear_widgets: bool = True) -> None:
    for key in (
        "lego_raw",
        "lego_results",
        "lego_settings",
        "lego_db",
        "lego_auth_summary",
        "lego_auth_fingerprint",
        "lego_reference_csv",
        "lego_symbol",
        "lego_dna_code",
        "lego_all_in_status",
        "lego_uat_preview",
        "lego_uat_output",
        "lego_audit_events",
        "lego_audit_firestore_off",
    ):
        st.session_state.pop(key, None)
    # Any connection change must void a stale Preview so a live Submit can never
    # reuse a payload that was validated under a different account/environment.
    for key in list(st.session_state.keys()):
        if str(key).startswith("order_") and str(key).endswith(
            ("_preview_state", "_output")
        ):
            st.session_state.pop(key, None)
    if clear_widgets:
        for key in list(st.session_state.keys()):
            if str(key).startswith("order_"):
                st.session_state.pop(key, None)
        for key in (
            "lego_account_id",
            "lego_app_key",
            "lego_app_secret",
            "lego_symbol_input",
            "lego_dna_code_input",
            "lego_submit_confirmation",
            "lego_cancel_confirmation",
        ):
            st.session_state[key] = ""


def _download_json(value: Any) -> bytes:
    return json.dumps(
        redact_payload(value), ensure_ascii=False, indent=2, default=str
    ).encode("utf-8")


def _real_webull_settings(settings: ConnectionSettings) -> WebullSettings:
    """Translate UI settings to the standalone read-only SDK contract."""

    return WebullSettings(
        environment=settings.environment,
        account_id=settings.account_id,
        app_key=settings.app_key,
        app_secret=settings.app_secret,
        region=settings.region,
    )


def authenticate_and_load(
    settings: ConnectionSettings,
    config: LegoDashboardConfig,
    *,
    symbol: str,
    dna_code: str,
) -> tuple[pd.DataFrame, firestore.Client, dict[str, Any]]:
    """Run real Step 0 reads; this is shared by Connect and All-in."""

    settings.validate()
    if not config.firebase_info:
        raise ValueError("Missing [firebase_service_account] in Streamlit secrets")
    dna_summary: dict[str, Any] = {"mode": "logged-only"}
    if dna_code.strip():
        dna, dna_summary = decode_guide_dna(dna_code)
        dna_summary = {
            **dna_summary,
            "ones": int(dna.sum()),
            "zeros": int(len(dna) - dna.sum()),
            "preview_first_20": dna[:20].astype(int).tolist(),
        }
    live = load_live_inputs(
        _real_webull_settings(settings),
        firebase_info=config.firebase_info,
        collection=config.trade_collection,
        limit=config.trade_limit,
        symbol=symbol,
    )
    raw = prepare_raw_frame(live.raw)
    summary = {
        **live.safe_summary,
        "firestore_project": config.firebase_info.get("project_id"),
        "dna_decoder": dna_summary,
    }
    return raw, live.firestore_client, summary


def run_all_pipeline_stages(
    raw: pd.DataFrame,
    config: LegoDashboardConfig,
    *,
    dna_code: str,
    on_step: Callable[[int], None] | None = None,
) -> dict[int, StageResult]:
    """Run pure Steps 1→17 and prove Step 18 can be materialized."""

    results: dict[int, StageResult] = {}
    context = PipelineContext(
        fix_c=config.fix_c,
        source_hash=dataframe_fingerprint(raw),
        dna_code=dna_code,
    )
    previous = None
    for stage_number in range(1, 18):
        result = run_stage(stage_number, raw, previous, context)
        results[stage_number] = result
        previous = result
        if on_step is not None:
            on_step(stage_number)
    final_dataframe(results[17])
    what_if_dataframe(results[17], config.fix_c)
    if on_step is not None:
        on_step(18)
    return results


def _order_defaults_from_row(stage_result: StageResult | None) -> tuple[str, str, float]:
    """Prefill order inputs from the latest chain row — convenience only."""

    default_symbol = str(st.session_state.get("lego_symbol", "") or "")
    default_side = "BUY"
    default_quantity = 1.0
    if stage_result is not None and not stage_result.frame.empty:
        latest = stage_result.frame.iloc[-1]
        symbol = str(latest.get("สินทรัพย์", "") or "").strip()
        if symbol:
            default_symbol = symbol
        side = str(latest.get("ฝั่ง", "") or "").strip().upper()
        if side in ("BUY", "SELL"):
            default_side = side
        raw_quantity = pd.to_numeric(
            pd.Series([latest.get("จำนวนสั่ง (หุ้น)")]), errors="coerce"
        ).iloc[0]
        if pd.notna(raw_quantity) and raw_quantity > 0:
            default_quantity = float(raw_quantity)
    return default_symbol.upper(), default_side, default_quantity


def _order_badge(action: str, summary_view: dict[str, Any]) -> tuple[str | None, str]:
    """Return a (message, streamlit-level) badge that never mislabels a fill."""

    status = summary_view.get("status")
    category = summary_view.get("status_category")
    order_id = summary_view.get("order_id")
    label = status or category
    if action == "PREVIEW":
        return ("Preview สำเร็จ — Webull ตรวจ payload แล้ว (ยังไม่ได้ส่งคำสั่ง)", "success")
    if action == "CANCEL":
        return (f"ส่งคำขอ Cancel แล้ว — สถานะจริง: {label}", "info")
    if action == "SUBMIT":
        if summary_view.get("is_filled"):
            return (f"order_id={order_id} · FILLED", "success")
        return (
            f"ส่งคำสั่งแล้ว order_id={order_id} · สถานะจริง={label} — "
            "ยังไม่ใช่ FILLED ห้ามนับเป็นเงินจริงจนกว่าจะยืนยัน fill",
            "warning",
        )
    if action == "QUERY":
        if summary_view.get("is_filled"):
            return (f"order_id={order_id} · FILLED", "success")
        if summary_view.get("is_terminal"):
            return (f"order_id={order_id} · terminal={label}", "warning")
        return (f"order_id={order_id} · pending/working={label} — ยังไม่ filled", "info")
    return (None, "info")


def _run_order_action(
    *,
    action: str,
    settings: ConnectionSettings,
    summary: dict[str, Any],
    call: Callable[[], Any],
    prefix: str,
    store_preview: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Execute one real Webull order call, redact, badge, and audit it."""

    started = time.perf_counter()
    try:
        result = call()
        safe_result = redact_payload(result)
        elapsed = (time.perf_counter() - started) * 1000
        summary_view = summarize_order_result(safe_result)
        if store_preview is not None:
            st.session_state[f"{prefix}_preview_state"] = dict(store_preview)
        badge, level = _order_badge(action, summary_view)
        st.session_state[f"{prefix}_output"] = {
            "action": action,
            "result": {"summary": summary_view, "raw": safe_result},
            "badge": badge,
            "level": level,
        }
        warning = record_audit(
            make_audit_event(
                action=action,
                settings=settings,
                request_summary=summary,
                result=safe_result,
                elapsed_ms=elapsed,
            )
        )
        if warning:
            st.warning(warning)
        return summary_view
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        warning = record_audit(
            make_audit_event(
                action=action,
                settings=settings,
                request_summary=summary,
                result=None,
                elapsed_ms=elapsed,
                error=exc,
            )
        )
        if warning:
            st.warning(warning)
        st.error(f"{exc.__class__.__name__}: {exc}")
        return None


def render_order_panel(
    tab_key: str,
    config: LegoDashboardConfig,
    *,
    default_symbol: str = "",
    default_side: str = "BUY",
    default_quantity: float = 1.0,
) -> None:
    """Guarded real-order control shared by every tab: Preview → Place → Query.

    UAT hits the real UAT endpoint.  Production is fail-closed behind a safety
    switch, a retyped confirmation phrase (which re-states account/symbol/side/
    quantity), and a Preview whose payload must match the one being placed.
    """

    st.divider()
    with st.expander("🔴 ส่งคำสั่งซื้อขายจริงผ่าน Webull Order API", expanded=False):
        _render_order_panel_body(
            tab_key,
            config,
            default_symbol=default_symbol,
            default_side=default_side,
            default_quantity=default_quantity,
        )


def _render_order_panel_body(
    tab_key: str,
    config: LegoDashboardConfig,
    *,
    default_symbol: str = "",
    default_side: str = "BUY",
    default_quantity: float = 1.0,
) -> None:
    """Body of :func:`render_order_panel`, rendered inside a collapsed expander."""

    settings: ConnectionSettings | None = st.session_state.get("lego_settings")
    if settings is None:
        st.info(
            "ต้องยืนยัน Webull ที่ Tab 0 (Connect & Load) ก่อนจึงจะ Preview/Submit order ได้"
        )
        return

    is_production = settings.environment == PRODUCTION_ENVIRONMENT
    st.caption(
        f"Environment: {settings.environment} · {settings.endpoint} · "
        f"Account #{account_fingerprint(settings.account_id)} · "
        "ค่าเริ่มต้นมาจากแถวล่าสุดเพื่อช่วยกรอกเท่านั้น — ตรวจทุกค่าใหม่ก่อนยิง"
    )
    if is_production:
        st.error(
            "โหมด Production — คำสั่งนี้เป็นเงินจริงและย้อนกลับไม่ได้ ต้องเปิด safety switch "
            "และพิมพ์ confirmation phrase ให้ตรงก่อน Submit"
        )
    else:
        st.warning("โหมด Test (UAT) — ยิง UAT endpoint จริง ไม่กระทบเงินจริง")

    prefix = f"order_{tab_key}"
    columns = st.columns(3)
    with columns[0]:
        symbol = (
            st.text_input("Symbol", value=default_symbol, key=f"{prefix}_symbol")
            .strip()
            .upper()
        )
    with columns[1]:
        side = st.selectbox(
            "Side",
            ["BUY", "SELL"],
            index=0 if default_side != "SELL" else 1,
            key=f"{prefix}_side",
        )
    with columns[2]:
        quantity = st.number_input(
            "Quantity (หุ้น)",
            min_value=0.00001,
            value=float(default_quantity),
            step=1.0,
            format="%.5f",
            key=f"{prefix}_quantity",
        )

    trading_session = st.selectbox(
        "Trading session",
        ["CORE", "PRE", "AFTER", "OVERNIGHT"],
        key=f"{prefix}_session",
    )

    coid_key = f"{prefix}_client_order_id"
    if coid_key not in st.session_state:
        st.session_state[coid_key] = generate_client_order_id(
            "LEGO", symbol or "NA", side, tab_key, time.time_ns()
        )
    coid_cols = st.columns([3, 1])
    with coid_cols[0]:
        client_order_id = st.text_input("Client Order ID", key=coid_key).strip()
    with coid_cols[1]:
        st.button(
            "🔄 id ใหม่",
            key=f"{prefix}_new_coid",
            on_click=lambda: st.session_state.update(
                {
                    coid_key: generate_client_order_id(
                        "LEGO", symbol or "NA", side, tab_key, time.time_ns()
                    )
                }
            ),
        )

    try:
        payload = build_market_order_payload(
            symbol, side, float(quantity), client_order_id, trading_session
        )
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        st.markdown("**Payload ที่ตรวจแล้ว (redacted)**")
        st.json(redact_payload(payload))
    except Exception as exc:
        payload = None
        payload_hash = ""
        st.error(f"{exc.__class__.__name__}: {exc}")

    request_summary = {
        "symbol": symbol,
        "side": side,
        "quantity": float(quantity),
        "client_order_id": client_order_id,
        "trading_session": trading_session,
        "environment": settings.environment,
    }

    if st.button("Preview order", disabled=payload is None, key=f"{prefix}_preview"):
        _run_order_action(
            action="PREVIEW",
            settings=settings,
            summary=request_summary,
            call=lambda: WebullManualClient(settings).preview_market_order(payload),
            store_preview={"payload_hash": payload_hash},
            prefix=prefix,
        )

    preview = st.session_state.get(f"{prefix}_preview_state")
    preview_matches = bool(
        preview and preview.get("payload_hash") == payload_hash and payload is not None
    )

    safety_switch = True
    if is_production:
        safety_switch = st.checkbox(
            "เปิด Production safety switch — ฉันยืนยันว่าจะยิงคำสั่งเงินจริง",
            value=False,
            key=f"{prefix}_safety",
        )

    phrase = ""
    confirmation = ""
    confirmation_ok = False
    if payload is not None:
        phrase = order_confirmation_phrase(
            settings.environment, settings.account_id, side, symbol, float(quantity)
        )
        st.markdown("**พิมพ์ confirmation phrase ให้ตรงเป๊ะก่อน Submit**")
        st.code(phrase, language=None)
        confirmation = st.text_input(
            "Confirmation phrase", value="", key=f"{prefix}_confirm"
        )
        confirmation_ok = confirmation.strip() == phrase

    gate = evaluate_submit_gate(
        environment=settings.environment,
        payload_valid=payload is not None,
        preview_matches=preview_matches,
        confirmation_ok=confirmation_ok,
        safety_switch=safety_switch,
    )
    if not gate.allowed:
        st.caption("ยังส่งไม่ได้: " + " · ".join(gate.reasons))

    if st.button(
        "🚀 Submit order (REAL)",
        disabled=not gate.allowed,
        type="primary",
        key=f"{prefix}_submit",
    ):
        if not gate.allowed or confirmation.strip() != phrase:
            st.error("การยืนยันไม่ครบ — ยกเลิกการ Submit")
        else:
            _run_order_action(
                action="SUBMIT",
                settings=settings,
                summary=request_summary,
                call=lambda: WebullManualClient(settings).place_market_order(payload),
                prefix=prefix,
            )
            # Force a fresh Preview + confirmation before any second submit.
            st.session_state.pop(f"{prefix}_preview_state", None)

    st.markdown("#### ตรวจสถานะคำสั่งจริง (Query)")
    query_id = st.text_input(
        "Client Order ID ที่จะ query", value=client_order_id, key=f"{prefix}_query_id"
    ).strip()
    if st.button("Query order status", disabled=not query_id, key=f"{prefix}_query"):
        _run_order_action(
            action="QUERY",
            settings=settings,
            summary={"client_order_id": query_id},
            call=lambda: WebullManualClient(settings).get_order_detail(query_id),
            prefix=prefix,
        )

    with st.expander("ยกเลิกคำสั่ง (Cancel)"):
        cancel_id = st.text_input(
            "Client Order ID ที่จะ cancel", key=f"{prefix}_cancel_id"
        ).strip()
        cancel_phrase = (
            f"CANCEL {'PROD' if is_production else 'UAT'} {cancel_id}"
            if cancel_id
            else ""
        )
        if cancel_id:
            st.code(cancel_phrase, language=None)
        cancel_confirm = st.text_input(
            "พิมพ์คำยืนยัน Cancel", value="", key=f"{prefix}_cancel_confirm"
        )
        if st.button("Cancel order", disabled=not cancel_id, key=f"{prefix}_cancel"):
            if cancel_confirm.strip() != cancel_phrase:
                st.error("คำยืนยัน Cancel ไม่ตรง")
            else:
                _run_order_action(
                    action="CANCEL",
                    settings=settings,
                    summary={"client_order_id": cancel_id},
                    call=lambda: WebullManualClient(settings).cancel_order(cancel_id),
                    prefix=prefix,
                )

    output = st.session_state.get(f"{prefix}_output")
    if output:
        st.markdown("#### ผลลัพธ์ล่าสุด (redacted)")
        badge = output.get("badge")
        if badge:
            getattr(st, output.get("level", "info"))(badge)
        st.json(output.get("result"))
        st.download_button(
            "Download order output JSON",
            data=_download_json(output.get("result")),
            file_name=f"webull_lego_order_{tab_key}.json",
            mime="application/json",
            key=f"{prefix}_download",
        )

    st.markdown("#### Learning Guide — order lifecycle")
    st.write(
        "ลำดับที่ปลอดภัย: (1) ตรวจ payload → (2) Preview ให้ Webull ตรวจก่อน → "
        "(3) พิมพ์ confirmation phrase + (Production) เปิด safety switch → "
        "(4) Submit ยิง place_order จริง → (5) Query อ่านสถานะจริง · "
        "สถานะ SUBMITTED/PENDING ไม่ใช่ FILLED และจะไม่ถูกนับเป็นเงินจริงจนกว่าจะมีหลักฐาน fill"
    )


def render_stage_tab(stage, config: LegoDashboardConfig) -> None:
    source_code = stage.source_code
    st.subheader(f"Step {stage.number} — {stage.title}")
    st.markdown(f"**Goal:** {stage.goal}")
    st.info(f"Quick Start: {stage.quick_start}")
    with st.expander(
        "LEGO code block ที่จะรัน — Single File", expanded=False
    ):
        st.caption(
            f"{stage.file_name} · คัดลอกหรือดาวน์โหลดไฟล์เดียวแล้วรันได้ · "
            "ฟังก์ชัน transform ในไฟล์นี้คือ callable เดียวกับปุ่ม Run"
        )
        st.code(source_code, language="python")
        st.download_button(
            "Download Single-File LEGO Block",
            data=source_code,
            file_name=stage.file_name,
            mime="text/x-python",
            key=f"lego_download_stage_{stage.number}",
        )

    raw: pd.DataFrame | None = st.session_state.get("lego_raw")
    results: dict[int, StageResult] = st.session_state.setdefault("lego_results", {})
    ready = raw is not None and (
        stage.number == 1 or (stage.number - 1) in results
    )
    if not ready:
        prerequisite = "Tab 0" if stage.number == 1 else f"Step {stage.number - 1}"
        st.warning(f"ยังรันไม่ได้ — ต้องให้ {prerequisite} สำเร็จก่อน")

    if st.button(
        f"Run LEGO Step {stage.number}",
        disabled=not ready,
        type="primary",
        key=f"lego_run_stage_{stage.number}",
    ):
        assert raw is not None
        invalidate_from(results, stage.number)
        previous = results.get(stage.number - 1)
        context = PipelineContext(
            fix_c=config.fix_c,
            source_hash=dataframe_fingerprint(raw),
            dna_code=st.session_state.get("lego_dna_code", ""),
        )
        try:
            results[stage.number] = run_stage(
                stage.number, raw, previous, context
            )
        except Exception as exc:
            st.error(f"{exc.__class__.__name__}: {exc}")

    result = results.get(stage.number)
    if result is not None:
        st.success(
            f"Step {stage.number} สำเร็จ — ส่ง accumulated dataframe ไป "
            + (f"Step {stage.number + 1}" if stage.number < 17 else "Final DataFrame")
        )
        metrics = st.columns(3)
        metrics[0].metric("Rows", len(result.frame))
        metrics[1].metric("Columns built", len(result.frame.columns))
        metrics[2].metric(
            "Non-blank current column",
            int(result.frame[stage.column_name].notna().sum()),
        )
        st.markdown("#### ผลลัพธ์สะสม")
        st.dataframe(
            result.frame.iloc[::-1].reset_index(drop=True),
            use_container_width=True,
        )
        with st.expander("Diagnostics + provenance", expanded=True):
            for diagnostic in result.diagnostics:
                st.write(f"• {diagnostic}")
            st.json(result.provenance)

    with st.expander("คู่มือเรียนรู้ LEGO Block", expanded=result is not None):
        st.write(stage.learning_guide)
        st.caption(
            "หลักคิด: input จากบล็อกก่อนหน้า → ฟังก์ชันบริสุทธิ์ → validation → "
            "output หนึ่งคอลัมน์ → ส่ง accumulated dataframe ต่อ"
        )

    default_symbol, default_side, default_quantity = _order_defaults_from_row(result)
    render_order_panel(
        f"stage{stage.number}",
        config,
        default_symbol=default_symbol,
        default_side=default_side,
        default_quantity=default_quantity,
    )


def render_auth_tab(config: LegoDashboardConfig) -> None:
    st.subheader("Step 0 — Authenticated connection")
    st.info(
        "กรอก Webull credential ใน session นี้ แล้วกด Connect & Load เพื่อยืนยัน "
        "Webull Account list + Balance + Positions + Quote และอ่าน Firestore trade log จริง"
    )
    environment = st.selectbox(
        "Environment",
        options=list(WEBULL_ENDPOINTS),
        index=0,
        key="lego_environment",
        help="Test/UAT เป็นค่าเริ่มต้น; Production อ่านได้แต่สั่งไม่ได้",
    )
    st.code(WEBULL_ENDPOINTS[environment], language=None)
    account_id = st.text_input(
        "Account ID", value="", key="lego_account_id", autocomplete="off"
    )
    app_key = st.text_input(
        "App Key",
        value="",
        type="password",
        key="lego_app_key",
        autocomplete="off",
    )
    app_secret = st.text_input(
        "App Secret",
        value="",
        type="password",
        key="lego_app_secret",
        autocomplete="off",
    )
    symbol = st.text_input(
        "Symbol สำหรับ Webull quote (optional)",
        value="",
        key="lego_symbol_input",
        help="ถ้าเว้นว่าง จะใช้ symbol แรกจาก Firestore trade log",
    ).strip().upper()
    dna_code = st.text_input(
        "DNA_CODE (encoded, bypass:N หรือ [1,N])",
        value="bypass:100",
        key="lego_dna_code_input",
        help="signal ที่บอท log ไว้มีสิทธิ์ก่อน; decoder เติมเฉพาะ signal ที่หายไป",
    ).strip()
    reference_file = st.file_uploader(
        "CSV reference (optional — ไม่ใช้แทน Firestore หลัก)",
        type=["csv"],
        key="lego_reference_upload",
    )

    settings = ConnectionSettings(
        environment=environment,
        account_id=account_id,
        app_key=app_key,
        app_secret=app_secret,
        region="th",
    )
    current_fingerprint = connection_fingerprint(
        settings, symbol=symbol, dna_code=dna_code
    )
    stored_fingerprint = st.session_state.get("lego_auth_fingerprint")
    if stored_fingerprint and stored_fingerprint != current_fingerprint:
        clear_connection_state(clear_widgets=False)
        st.warning("Connection input เปลี่ยน — ล้างผล Step 1–18 แล้ว")

    action_left, action_right = st.columns(2)
    with action_left:
        connect_clicked = st.button(
            "Connect & Load",
            type="primary",
            use_container_width=True,
            key="lego_connect_button",
        )
    with action_right:
        st.button(
            "Clear credentials + reset",
            use_container_width=True,
            on_click=clear_connection_state,
            key="lego_clear_button",
        )

    if connect_clicked:
        try:
            settings.validate()
            with st.spinner("ยิง Webull API จริงและโหลด Firestore จริง..."):
                raw, db, live_summary = authenticate_and_load(
                    settings,
                    config,
                    symbol=symbol,
                    dna_code=dna_code,
                )

            reference = None
            if reference_file is not None:
                reference_file.seek(0)
                reference = prepare_raw_frame(pd.read_csv(reference_file))

            st.session_state.lego_settings = settings
            st.session_state.lego_db = db
            st.session_state.lego_config = config
            st.session_state.lego_raw = raw
            st.session_state.lego_reference_csv = reference
            st.session_state.lego_symbol = symbol
            st.session_state.lego_dna_code = dna_code
            st.session_state.lego_results = {}
            st.session_state.lego_audit_events = []
            st.session_state.lego_auth_fingerprint = current_fingerprint
            st.session_state.lego_auth_summary = {
                **live_summary,
                "reference_csv_rows": len(reference) if reference is not None else 0,
            }
        except Exception as exc:
            clear_connection_state(clear_widgets=False)
            st.error(f"{exc.__class__.__name__}: {exc}")

    summary = st.session_state.get("lego_auth_summary")
    if summary is not None:
        st.success("Authenticated connection สำเร็จ — พร้อมส่ง raw dataframe ไป Step 1")
        safe_summary = redact_payload(summary)
        summary_cols = st.columns(4)
        summary_cols[0].metric("Environment", safe_summary["environment"])
        summary_cols[1].metric("Trade rows", safe_summary["trade_rows"])
        summary_cols[2].metric("FIX_C", f"${config.fix_c:,.2f}")
        summary_cols[3].metric("Reference rows", safe_summary["reference_csv_rows"])
        with st.expander("All authenticated output (redacted)"):
            st.json(safe_summary)
        st.download_button(
            "Download authenticated output JSON",
            data=_download_json(safe_summary),
            file_name="webull_lego_authenticated_output.json",
            mime="application/json",
        )
    elif not config.firebase_info:
        st.warning(
            "ยังไม่มี [firebase_service_account] — หน้าแอปโหลดได้ แต่ Connect & Load "
            "จะ fail-closed จนกว่าจะตั้ง Streamlit secrets"
        )

    st.markdown("#### Learning Guide")
    st.write(
        "Tab 0 ยิง Webull SDK จริง 3–4 read endpoints: Account list, Balance, "
        "Positions และ Market snapshot (เมื่อมี symbol) แล้วอ่าน Firestore จริง "
        "DNA decoder ใช้ width/value → seed → mutation ตาม Shannon Demon Learning Guide "
        "โดยไม่เขียน credential หรือ raw account response ลง Final CSV"
    )
    source = Path(__file__).with_name("webull_lego_single_file.py").read_text(
        encoding="utf-8"
    )
    with st.expander("Real All-in 0→18 — Single File", expanded=False):
        st.caption(
            "ไฟล์เดียวนี้มี Webull API reads, Firestore, DNA decode และ 17 transformations "
            "ครบ; Production ก็อ่านจริงแต่ไม่มี place/cancel method"
        )
        st.code(source, language="python")
        st.download_button(
            "Download Real All-in Single File",
            data=source,
            file_name="webull_lego_single_file.py",
            mime="text/x-python",
            key="lego_download_all_in_auth",
        )

    default_symbol, default_side, default_quantity = _order_defaults_from_row(None)
    render_order_panel(
        "auth",
        config,
        default_symbol=default_symbol,
        default_side=default_side,
        default_quantity=default_quantity,
    )


def render_final_tab(config: LegoDashboardConfig) -> None:
    st.subheader("Step 18 — Final DataFrame")
    results: dict[int, StageResult] = st.session_state.get("lego_results", {})
    if 17 not in results:
        completed = len([stage for stage in range(1, 18) if stage in results])
        st.warning(
            f"Final ยังล็อกอยู่ — สำเร็จ {completed}/17 steps; ต้อง Run Step 1 ถึง 17 ตามลำดับ"
        )
        symbol0, side0, qty0 = _order_defaults_from_row(None)
        render_order_panel(
            "final",
            config,
            default_symbol=symbol0,
            default_side=side0,
            default_quantity=qty0,
        )
        return

    final = final_dataframe(results[17])
    what_if = what_if_dataframe(results[17], config.fix_c)
    st.success("LEGO chain สำเร็จ 17/17 — Final DataFrame พร้อมใช้งาน")
    st.caption(
        "ตารางหลักใช้ broker-confirmed ledger: terminal fill + filled quantity + "
        "position reconciliation + execution price จริงเท่านั้น"
    )
    st.dataframe(final, use_container_width=True)
    st.download_button(
        "Download Final DataFrame CSV",
        data=final.to_csv(index=False).encode("utf-8-sig"),
        file_name="webull_lego_final_dataframe.csv",
        mime="text/csv",
    )

    st.markdown("### What-if learning ledger (แยกจากเงินจริง)")
    st.warning(
        "ชุดนี้คำนวณทุก positive quote เพื่อเทียบ CSV ตัวอย่าง ไม่ใช่ broker cash ledger"
    )
    st.dataframe(what_if, use_container_width=True)
    st.download_button(
        "Download What-if CSV",
        data=what_if.to_csv(index=False).encode("utf-8-sig"),
        file_name="webull_lego_what_if.csv",
        mime="text/csv",
    )

    reference = st.session_state.get("lego_reference_csv")
    if isinstance(reference, pd.DataFrame):
        with st.expander("Uploaded CSV reference"):
            clean_reference = reference.drop(
                columns=[INTERNAL_ROW_ID], errors="ignore"
            ).iloc[::-1].reset_index(drop=True)
            st.dataframe(clean_reference, use_container_width=True)

    audit_events = st.session_state.get("lego_audit_events", [])
    if audit_events:
        st.download_button(
            "Download sanitized UAT audit JSON",
            data=_download_json(audit_events),
            file_name="webull_lego_uat_audit.json",
            mime="application/json",
        )

    symbol0, side0, qty0 = _order_defaults_from_row(results.get(17))
    render_order_panel(
        "final",
        config,
        default_symbol=symbol0,
        default_side=side0,
        default_quantity=qty0,
    )


def render_all_in_sidebar(config: LegoDashboardConfig) -> None:
    """Real read-only Webull/Firestore Step 0 followed by Steps 1→18."""

    with st.sidebar:
        st.header("🧱 All-in Loop 0→18")
        st.caption(
            "ยิง Webull API + Firestore ใหม่จริง แล้วต่อ LEGO 17 ขั้นและ Final ในคลิกเดียว "
            "(loop เป็น read-only) จากนั้นยิง order จริงได้ที่ order panel ด้านล่าง "
            "ด้วย submit gate เดียวกับ Manual Run"
        )
        settings: ConnectionSettings | None = st.session_state.get("lego_settings")
        if settings is None:
            st.warning("กรอก credential และกด Connect & Load ที่ Tab 0 ก่อน")
        else:
            st.info(
                f"{settings.environment} · {settings.endpoint} · "
                "All-in loop = read-only reads · order panel ยิงจริงอยู่ด้านล่าง"
            )

        status = st.session_state.get("lego_all_in_status")
        if isinstance(status, dict):
            if status.get("ok"):
                st.success(
                    f"ครบ 0→18 · {status.get('rows', 0)} rows · "
                    f"{status.get('elapsed_seconds', 0):.3f}s"
                )
            else:
                st.error(
                    f"หยุดที่ Step {status.get('step', '?')}: "
                    f"{status.get('error_type', 'Error')}"
                )

        run_clicked = st.button(
            "Run ALL 0 → 18 (REAL READ)",
            type="primary",
            use_container_width=True,
            disabled=settings is None,
            key="lego_all_in_button",
        )
        if run_clicked and settings is not None:
            started = time.perf_counter()
            progress = st.progress(0.0, text="Step 0 · Webull + Firestore real reads")
            current_step = 0
            try:
                dna_code = st.session_state.get("lego_dna_code", "")
                symbol = st.session_state.get("lego_symbol", "")
                raw, db, summary = authenticate_and_load(
                    settings,
                    config,
                    symbol=symbol,
                    dna_code=dna_code,
                )
                progress.progress(1 / 19, text="Step 0 สำเร็จ · authenticated real reads")

                def update_progress(step: int) -> None:
                    nonlocal current_step
                    current_step = step
                    label = "Final DataFrame" if step == 18 else STAGES[step - 1].title
                    progress.progress(
                        (step + 1) / 19,
                        text=f"Step {step} สำเร็จ · {label}",
                    )

                results = run_all_pipeline_stages(
                    raw,
                    config,
                    dna_code=dna_code,
                    on_step=update_progress,
                )
                elapsed = time.perf_counter() - started
                reference = st.session_state.get("lego_reference_csv")
                st.session_state.lego_raw = raw
                st.session_state.lego_db = db
                st.session_state.lego_config = config
                st.session_state.lego_results = results
                st.session_state.lego_auth_summary = {
                    **summary,
                    "reference_csv_rows": (
                        len(reference) if isinstance(reference, pd.DataFrame) else 0
                    ),
                    "all_in_completed_steps": list(range(19)),
                }
                st.session_state.lego_all_in_status = {
                    "ok": True,
                    "step": 18,
                    "rows": len(raw),
                    "elapsed_seconds": elapsed,
                }
                st.rerun()
            except Exception as exc:
                st.session_state.lego_results = {}
                st.session_state.pop("lego_raw", None)
                st.session_state.pop("lego_auth_summary", None)
                st.session_state.lego_all_in_status = {
                    "ok": False,
                    "step": current_step,
                    "error_type": exc.__class__.__name__,
                }
                st.error(f"{exc.__class__.__name__}: {exc}")

        source = Path(__file__).with_name("webull_lego_single_file.py").read_text(
            encoding="utf-8"
        )
        st.download_button(
            "Download All-in Single File",
            data=source,
            file_name="webull_lego_single_file.py",
            mime="text/x-python",
            use_container_width=True,
            key="lego_download_all_in_sidebar",
        )
        st.caption("ไฟล์เดียว · env credentials · Test/Production real reads · no mutation")

        # All-in REAL order — run the chain read-only above, then fire the final
        # decision through the SAME guarded submit gate as the per-tab Manual Run
        # (Preview → confirmation phrase → Production safety switch → Submit).
        st.divider()
        st.markdown("**🔴 All-in REAL order (เหมือน Manual Run)**")
        results = st.session_state.get("lego_results", {})
        if settings is None:
            st.info("ต้อง Connect & Load ที่ Tab 0 ก่อน")
        elif 17 not in results:
            st.caption(
                "กด Run ALL 0 → 18 ให้ครบก่อน แล้ว order panel จะพร้อมยิงจริงที่นี่"
            )
        else:
            symbol0, side0, qty0 = _order_defaults_from_row(results.get(17))
            render_order_panel(
                "allin",
                config,
                default_symbol=symbol0,
                default_side=side0,
                default_quantity=qty0,
            )


if "lego_session_run_id" not in st.session_state:
    st.session_state.lego_session_run_id = uuid.uuid4().hex

try:
    dashboard_config = load_dashboard_config()
except Exception as config_error:
    st.error(f"Configuration error: {config_error}")
    dashboard_config = LegoDashboardConfig(firebase_info={})

st.title("🧱 Webull LEGO Chain")
st.caption(
    "Authenticated hybrid data → 17 manual LEGO blocks → Final DataFrame · "
    "ทุกแท็บ 0–18 มี order panel ยิง Webull Order API จริง (Preview → Place → Query) · "
    "การ Run/คำนวณเป็น read-only การส่ง order ต้องกด Submit เองเสมอ"
)
st.warning(
    "Deploy แอปนี้เป็น Private single-user · Production ส่ง order เงินจริงได้เฉพาะเมื่อเปิด "
    "safety switch + พิมพ์ confirmation phrase — ตรวจ account/symbol/side/quantity ทุกครั้ง"
)

tab_labels = [
    "0 · Authenticated connection",
    *[f"{stage.number} · {stage.column_name}" for stage in STAGES],
    "18 · Final DataFrame",
]
tabs = st.tabs(tab_labels)

with tabs[0]:
    render_auth_tab(dashboard_config)

for stage, tab in zip(STAGES, tabs[1:18]):
    with tab:
        render_stage_tab(stage, dashboard_config)

with tabs[18]:
    render_final_tab(dashboard_config)

render_all_in_sidebar(dashboard_config)
