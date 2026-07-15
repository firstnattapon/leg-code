"""Webull LEGO Chain — a manually-run, 17-stage Streamlit learning app."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import time
import uuid
from typing import Any

import pandas as pd
import streamlit as st
from google.cloud import firestore
from google.oauth2 import service_account

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
    format_order_quantity,
    generate_client_order_id,
)
from lego_uat import (
    account_fingerprint,
    build_audit_event,
    redact_payload,
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
    )


@st.cache_resource(show_spinner=False)
def firestore_client(firebase_json: str) -> firestore.Client:
    info = json.loads(firebase_json)
    credentials = service_account.Credentials.from_service_account_info(info)
    return firestore.Client(credentials=credentials, project=info["project_id"])


def load_trade_log(
    db: firestore.Client, collection: str, limit: int
) -> pd.DataFrame:
    documents = (
        db.collection(collection)
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(int(limit))
        .stream()
    )
    rows = [document.to_dict() for document in documents]
    normalized = pd.json_normalize(rows, sep="_") if rows else pd.DataFrame()
    return prepare_raw_frame(normalized)


def connection_fingerprint(settings: ConnectionSettings) -> str:
    payload = "\x00".join(
        (
            settings.environment,
            settings.account_id,
            settings.app_key,
            settings.app_secret,
            settings.region,
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
    """Write sanitized audit best-effort and always keep a session download copy."""

    st.session_state.setdefault("lego_audit_events", []).append(event)
    db = st.session_state.get("lego_db")
    config = st.session_state.get("lego_config")
    if db is None or config is None:
        return "ไม่มี Firestore client สำหรับบันทึก audit"
    try:
        db.collection(config.audit_collection).document(event["event_id"]).set(event)
    except Exception as exc:  # audit failure must not hide an API result
        return f"เขียน audit ไม่สำเร็จ: {exc.__class__.__name__}: {exc}"
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
        "lego_uat_preview",
        "lego_uat_output",
        "lego_audit_events",
    ):
        st.session_state.pop(key, None)
    if clear_widgets:
        for key in (
            "lego_account_id",
            "lego_app_key",
            "lego_app_secret",
            "lego_submit_confirmation",
            "lego_cancel_confirmation",
        ):
            st.session_state[key] = ""


def _download_json(value: Any) -> bytes:
    return json.dumps(
        redact_payload(value), ensure_ascii=False, indent=2, default=str
    ).encode("utf-8")


def _latest_stage_row(stage_result: StageResult) -> pd.Series:
    if stage_result.frame.empty:
        return pd.Series(dtype=object)
    return stage_result.frame.iloc[-1]


def render_uat_panel(stage_result: StageResult) -> None:
    st.divider()
    st.subheader("UAT order actions — แยกจาก LEGO Run")
    st.caption(
        "ค่าเริ่มต้นมาจากแถวล่าสุดเพื่อช่วยกรอกเท่านั้น กรุณาตรวจทุกค่าใหม่ "
        "Preview, Submit และ Cancel จะทำงานเฉพาะ Test/UAT"
    )
    settings: ConnectionSettings | None = st.session_state.get("lego_settings")
    if settings is None:
        st.info("ต้องผ่าน Tab 0 ก่อนใช้ UAT actions")
        return

    latest = _latest_stage_row(stage_result)
    default_symbol = str(latest.get("สินทรัพย์", "AAPL") or "AAPL")
    default_side = str(latest.get("ฝั่ง", "BUY") or "BUY")
    if default_side not in ("BUY", "SELL"):
        default_side = "BUY"
    raw_quantity = pd.to_numeric(
        pd.Series([latest.get("จำนวนสั่ง (หุ้น)")]), errors="coerce"
    ).iloc[0]
    default_quantity = float(raw_quantity) if pd.notna(raw_quantity) and raw_quantity > 0 else 1.0

    left, middle, right = st.columns(3)
    with left:
        symbol = st.text_input(
            "UAT symbol", value=default_symbol, key="lego_uat_symbol"
        ).strip().upper()
    with middle:
        side = st.selectbox(
            "UAT side",
            ["BUY", "SELL"],
            index=0 if default_side == "BUY" else 1,
            key="lego_uat_side",
        )
    with right:
        quantity = st.number_input(
            "UAT quantity",
            min_value=0.00001,
            value=default_quantity,
            step=1.0,
            format="%.5f",
            key="lego_uat_quantity",
        )

    trading_session = st.selectbox(
        "Trading session",
        ["CORE", "PRE", "AFTER", "OVERNIGHT"],
        key="lego_uat_trading_session",
    )
    if "lego_uat_client_order_id" not in st.session_state:
        st.session_state.lego_uat_client_order_id = generate_client_order_id(
            "LEGO", symbol or "AAPL", side, quantity, time.time_ns()
        )
    client_order_id = st.text_input(
        "Client Order ID", key="lego_uat_client_order_id"
    ).strip()

    try:
        payload = build_market_order_payload(
            symbol,
            side,
            float(quantity),
            client_order_id,
            trading_session,
        )
        payload_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()
        st.markdown("**Payload ที่ตรวจแล้ว**")
        st.json(redact_payload(payload))
    except Exception as exc:
        payload = None
        payload_hash = ""
        st.error(f"{exc.__class__.__name__}: {exc}")

    uat_only = settings.environment == "Test (UAT)"
    if not uat_only:
        st.error("Production เป็น read-only ใน LEGO app — mutation buttons ถูกปิด")

    if st.button(
        "Preview UAT order",
        disabled=payload is None or not uat_only,
        key="lego_uat_preview_button",
    ):
        started = time.perf_counter()
        summary = {
            "symbol": symbol,
            "side": side,
            "quantity": float(quantity),
            "client_order_id": client_order_id,
            "trading_session": trading_session,
        }
        try:
            result = WebullManualClient(settings).preview_market_order(payload)
            safe_result = redact_payload(result)
            elapsed = (time.perf_counter() - started) * 1000
            st.session_state.lego_uat_preview = {
                "payload_hash": payload_hash,
                "result": safe_result,
            }
            st.session_state.lego_uat_output = {
                "action": "PREVIEW",
                "result": safe_result,
            }
            warning = record_audit(
                make_audit_event(
                    action="PREVIEW",
                    settings=settings,
                    request_summary=summary,
                    result=safe_result,
                    elapsed_ms=elapsed,
                )
            )
            if warning:
                st.warning(warning)
        except Exception as exc:
            elapsed = (time.perf_counter() - started) * 1000
            warning = record_audit(
                make_audit_event(
                    action="PREVIEW",
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

    preview = st.session_state.get("lego_uat_preview")
    preview_matches = bool(preview and preview.get("payload_hash") == payload_hash)
    phrase = f"PLACE TEST {side} {symbol} {format_order_quantity(float(quantity))}"
    st.code(phrase, language=None)
    confirmation = st.text_input(
        "พิมพ์คำยืนยัน Submit ให้ตรง",
        value="",
        key="lego_submit_confirmation",
    )
    if st.button(
        "Submit UAT order",
        disabled=payload is None or not uat_only or not preview_matches,
        type="primary",
        key="lego_uat_submit_button",
    ):
        if confirmation.strip() != phrase:
            st.error("คำยืนยัน Submit ไม่ตรง")
        else:
            started = time.perf_counter()
            summary = {
                "symbol": symbol,
                "side": side,
                "quantity": float(quantity),
                "client_order_id": client_order_id,
                "trading_session": trading_session,
            }
            try:
                result = WebullManualClient(settings).place_market_order(payload)
                safe_result = redact_payload(result)
                elapsed = (time.perf_counter() - started) * 1000
                st.session_state.lego_uat_output = {
                    "action": "SUBMIT",
                    "result": safe_result,
                }
                warning = record_audit(
                    make_audit_event(
                        action="SUBMIT",
                        settings=settings,
                        request_summary=summary,
                        result=safe_result,
                        elapsed_ms=elapsed,
                    )
                )
                if warning:
                    st.warning(warning)
            except Exception as exc:
                elapsed = (time.perf_counter() - started) * 1000
                warning = record_audit(
                    make_audit_event(
                        action="SUBMIT",
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

    st.markdown("#### Cancel UAT order")
    cancel_id = st.text_input("Client Order ID ที่จะ cancel", key="lego_cancel_order_id").strip()
    cancel_phrase = f"CANCEL TEST {cancel_id}"
    if cancel_id:
        st.code(cancel_phrase, language=None)
    cancel_confirmation = st.text_input(
        "พิมพ์คำยืนยัน Cancel ให้ตรง",
        value="",
        key="lego_cancel_confirmation",
    )
    if st.button(
        "Cancel UAT order",
        disabled=not uat_only or not cancel_id,
        key="lego_uat_cancel_button",
    ):
        if cancel_confirmation.strip() != cancel_phrase:
            st.error("คำยืนยัน Cancel ไม่ตรง")
        else:
            started = time.perf_counter()
            summary = {"client_order_id": cancel_id}
            try:
                result = WebullManualClient(settings).cancel_order(cancel_id)
                safe_result = redact_payload(result)
                elapsed = (time.perf_counter() - started) * 1000
                st.session_state.lego_uat_output = {
                    "action": "CANCEL",
                    "result": safe_result,
                }
                warning = record_audit(
                    make_audit_event(
                        action="CANCEL",
                        settings=settings,
                        request_summary=summary,
                        result=safe_result,
                        elapsed_ms=elapsed,
                    )
                )
                if warning:
                    st.warning(warning)
            except Exception as exc:
                elapsed = (time.perf_counter() - started) * 1000
                warning = record_audit(
                    make_audit_event(
                        action="CANCEL",
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

    if "lego_uat_output" in st.session_state:
        safe_output = st.session_state.lego_uat_output
        st.markdown("#### UAT output (redacted)")
        st.json(safe_output)
        st.download_button(
            "Download UAT output JSON",
            data=_download_json(safe_output),
            file_name="webull_lego_uat_output.json",
            mime="application/json",
        )


def render_stage_tab(stage, config: LegoDashboardConfig) -> None:
    st.subheader(f"Step {stage.number} — {stage.title}")
    st.info(f"Quick Start: {stage.quick_start}")
    st.markdown("#### LEGO code block ที่จะรัน")
    st.code(stage.source_code, language="python")

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

        if stage.number == 11:
            render_uat_panel(result)

    with st.expander("คู่มือเรียนรู้ LEGO Block", expanded=result is not None):
        st.write(stage.learning_guide)
        st.caption(
            "หลักคิด: input จากบล็อกก่อนหน้า → ฟังก์ชันบริสุทธิ์ → validation → "
            "output หนึ่งคอลัมน์ → ส่ง accumulated dataframe ต่อ"
        )


def render_auth_tab(config: LegoDashboardConfig) -> None:
    st.subheader("Step 0 — Authenticated connection")
    st.info(
        "กรอก Webull credential ใน session นี้ แล้วกด Connect & Load เพื่อยืนยัน "
        "Webull Positions และอ่าน Firestore trade log"
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
    current_fingerprint = connection_fingerprint(settings)
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
            if not config.firebase_info:
                raise ValueError(
                    "Missing [firebase_service_account] in Streamlit secrets"
                )
            with st.spinner("ยืนยัน Webull และโหลด Firestore..."):
                started = time.perf_counter()
                positions = WebullManualClient(settings).get_positions()
                db = firestore_client(
                    json.dumps(config.firebase_info, sort_keys=True)
                )
                raw = load_trade_log(
                    db, config.trade_collection, config.trade_limit
                )
                elapsed = time.perf_counter() - started

            reference = None
            if reference_file is not None:
                reference_file.seek(0)
                reference = prepare_raw_frame(pd.read_csv(reference_file))

            st.session_state.lego_settings = settings
            st.session_state.lego_db = db
            st.session_state.lego_config = config
            st.session_state.lego_raw = raw
            st.session_state.lego_reference_csv = reference
            st.session_state.lego_results = {}
            st.session_state.lego_audit_events = []
            st.session_state.lego_auth_fingerprint = current_fingerprint
            st.session_state.lego_auth_summary = {
                "environment": environment,
                "endpoint": settings.endpoint,
                "account_fingerprint": account_fingerprint(account_id),
                "firestore_project": config.firebase_info.get("project_id"),
                "trade_collection": config.trade_collection,
                "trade_rows": len(raw),
                "reference_csv_rows": len(reference) if reference is not None else 0,
                "elapsed_seconds": round(elapsed, 3),
                "positions_response": redact_payload(positions),
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
        "Tab 0 ยืนยันสองแหล่งพร้อมกัน: Webull แสดงว่ credential อ่านบัญชีได้ "
        "และ Firestore ให้ DNA/trade documents ที่ Webull API ไม่มี ค่า secret "
        "ไม่ถูกเพิ่มลง dataframe, audit หรือไฟล์ดาวน์โหลด"
    )


def render_final_tab(config: LegoDashboardConfig) -> None:
    st.subheader("Step 18 — Final DataFrame")
    results: dict[int, StageResult] = st.session_state.get("lego_results", {})
    if 17 not in results:
        completed = len([stage for stage in range(1, 18) if stage in results])
        st.warning(
            f"Final ยังล็อกอยู่ — สำเร็จ {completed}/17 steps; ต้อง Run Step 1 ถึง 17 ตามลำดับ"
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
    "ทุก Run เป็น read-only ยกเว้นปุ่ม UAT ที่แยกและยืนยันชัดเจน"
)
st.warning(
    "Deploy แอปนี้เป็น Private single-user ก่อนใส่ Firebase service account ใน Streamlit secrets"
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
