"""Webull LEGO Chain — one immutable snapshot, one newly calculated row."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import importlib
import json
from pathlib import Path
import time
from typing import Any, Callable

import pandas as pd
import streamlit as st

import lego_pipeline as _lego_pipeline

EXPECTED_PIPELINE_SCHEMA_VERSION = 4
if (
    getattr(_lego_pipeline, "PIPELINE_SCHEMA_VERSION", 0)
    != EXPECTED_PIPELINE_SCHEMA_VERSION
):
    _lego_pipeline = importlib.reload(_lego_pipeline)

from lego_live import StepZeroResult, load_step_zero_snapshot
from lego_orders import (
    PRODUCTION_ENVIRONMENT,
    evaluate_submit_gate,
    order_confirmation_phrase,
    summarize_order_result,
)
from lego_pipeline import (
    FINAL_COLUMNS,
    PipelineContext,
    STAGES,
    StageResult,
    final_dataframe,
    invalidate_from,
    run_stage,
)
from lego_store import (
    FirestoreCollections,
    StaleAnchorError,
    build_final_document,
    persist_final_row,
)
from lego_uat import account_fingerprint, build_audit_event, redact_payload
import manual_tools
from manual_tools import WEBULL_ENDPOINTS, ConnectionSettings


st.set_page_config(
    page_title="Webull LEGO Chain",
    page_icon="🧱",
    layout="wide",
)


@dataclass(frozen=True)
class LegoDashboardConfig:
    firebase_info: dict[str, Any]
    collections: FirestoreCollections = field(default_factory=FirestoreCollections)
    fix_c: float = 1500.0
    diff: float = 30.0
    decimal_precision: int = 5
    audit_to_firestore: bool = True


def _secret_section(name: str) -> dict[str, Any]:
    try:
        return dict(st.secrets[name])
    except (KeyError, FileNotFoundError, TypeError):
        return {}


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def load_dashboard_config() -> LegoDashboardConfig:
    firebase_info = _secret_section("firebase_service_account")
    lego = _secret_section("lego_dashboard")
    try:
        fix_c = float(lego.get("fix_c", 1500.0))
        diff = float(lego.get("diff", 30.0))
        precision = int(lego.get("decimal_precision", 5))
    except (TypeError, ValueError) as exc:
        raise ValueError("fix_c, diff, and decimal_precision must be numeric") from exc
    if fix_c <= 0 or diff < 0 or precision < 0:
        raise ValueError("fix_c > 0, diff >= 0, and precision >= 0 are required")
    collections = FirestoreCollections(
        rows=str(lego.get("rows_collection", "webull_lego_rows")).strip(),
        state=str(lego.get("state_collection", "webull_lego_state")).strip(),
        order_audit=str(
            lego.get("order_audit_collection", "webull_lego_order_audit")
        ).strip(),
    )
    return LegoDashboardConfig(
        firebase_info=firebase_info,
        collections=collections,
        fix_c=fix_c,
        diff=diff,
        decimal_precision=precision,
        audit_to_firestore=_coerce_bool(lego.get("audit_to_firestore", True)),
    )


def connection_fingerprint(
    settings: ConnectionSettings,
    *,
    symbol: str,
    dna_code: str,
    fix_c: float,
    diff: float,
    decimal_precision: int,
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
            repr(float(fix_c)),
            repr(float(diff)),
            str(int(decimal_precision)),
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def clear_connection_state(*, clear_widgets: bool = True) -> None:
    for key in (
        "lego_raw",
        "lego_context",
        "lego_results",
        "lego_settings",
        "lego_db",
        "lego_config",
        "lego_auth_summary",
        "lego_auth_fingerprint",
        "lego_symbol",
        "lego_dna_code",
        "lego_fix_c",
        "lego_diff",
        "lego_decimal_precision",
        "lego_account_fingerprint",
        "lego_strategy_hash",
        "lego_final_persisted",
        "lego_final_document",
        "lego_all_in_status",
        "lego_audit_events",
        "lego_audit_firestore_off",
        "order_final_preview_state",
        "order_final_output",
    ):
        st.session_state.pop(key, None)
    if clear_widgets:
        for key in (
            "lego_account_id",
            "lego_app_key",
            "lego_app_secret",
            "lego_symbol_input",
            "lego_dna_code_input",
            "order_final_confirm",
        ):
            st.session_state[key] = ""


def _download_json(value: Any) -> bytes:
    return json.dumps(
        redact_payload(value), ensure_ascii=False, indent=2, default=str
    ).encode("utf-8")


def _auth_summary_metrics(
    summary: dict[str, Any],
    raw: Any,
) -> tuple[int, int, int, str]:
    """Read Step-0 metrics without trusting a stale Streamlit session schema."""

    snapshot_rows = summary.get("snapshot_rows")
    if snapshot_rows is None:
        snapshot_rows = len(raw) if isinstance(raw, pd.DataFrame) else 0

    anchor = summary.get("anchor")
    anchor_version = (
        anchor.get("version", 0)
        if isinstance(anchor, dict)
        else summary.get("anchor_version", 0)
    )
    run_id = str(summary.get("run_id") or "")
    return (
        int(snapshot_rows),
        int(summary.get("old_trade_log_reads", 0)),
        int(anchor_version or 0),
        run_id[:10] or "—",
    )


def authenticate_and_load(
    settings: ConnectionSettings,
    config: LegoDashboardConfig,
    *,
    symbol: str,
    dna_code: str,
    fix_c: float,
    diff: float,
    decimal_precision: int,
) -> StepZeroResult:
    return load_step_zero_snapshot(
        settings,
        firebase_info=config.firebase_info,
        collections=config.collections,
        symbol=symbol,
        dna_code=dna_code,
        fix_c=fix_c,
        diff=diff,
        decimal_precision=decimal_precision,
    )


def run_all_pipeline_stages(
    raw: pd.DataFrame,
    context: PipelineContext,
    *,
    on_step: Callable[[int], None] | None = None,
) -> dict[int, StageResult]:
    results: dict[int, StageResult] = {}
    previous = None
    for stage_number in range(1, 18):
        previous = run_stage(stage_number, raw, previous, context)
        results[stage_number] = previous
        if on_step is not None:
            on_step(stage_number)
    final_dataframe(results[17])
    if on_step is not None:
        on_step(18)
    return results


def _store_step_zero(
    result: StepZeroResult,
    settings: ConnectionSettings,
    config: LegoDashboardConfig,
    *,
    symbol: str,
    dna_code: str,
    fix_c: float,
    diff: float,
    decimal_precision: int,
    auth_fingerprint: str,
) -> None:
    st.session_state.lego_settings = settings
    st.session_state.lego_db = result.firestore_client
    st.session_state.lego_config = config
    st.session_state.lego_raw = result.raw
    st.session_state.lego_context = result.context
    st.session_state.lego_results = {}
    st.session_state.lego_symbol = symbol
    st.session_state.lego_dna_code = dna_code
    st.session_state.lego_fix_c = fix_c
    st.session_state.lego_diff = diff
    st.session_state.lego_decimal_precision = decimal_precision
    st.session_state.lego_account_fingerprint = result.account_fingerprint
    st.session_state.lego_strategy_hash = result.strategy_hash
    st.session_state.lego_auth_summary = result.safe_summary
    st.session_state.lego_auth_fingerprint = auth_fingerprint
    st.session_state.lego_final_persisted = None
    st.session_state.lego_final_document = None
    st.session_state.lego_audit_events = []


def _persist_completed_row(config: LegoDashboardConfig) -> tuple[Any, dict[str, Any]]:
    results: dict[int, StageResult] = st.session_state.get("lego_results", {})
    if 17 not in results:
        raise ValueError("Step 17 must be complete before Step 18")
    context: PipelineContext | None = st.session_state.get("lego_context")
    db = st.session_state.get("lego_db")
    settings: ConnectionSettings | None = st.session_state.get("lego_settings")
    if context is None or db is None or settings is None:
        raise ValueError("Step 0 context is missing; reconnect before Step 18")
    final = final_dataframe(results[17])
    summary = dict(st.session_state.get("lego_auth_summary") or {})
    document = build_final_document(
        context=context,
        final=final,
        stage_result=results[17],
        environment=settings.environment,
        account_fingerprint=str(
            st.session_state.get("lego_account_fingerprint", "")
        ),
        strategy_hash=str(st.session_state.get("lego_strategy_hash", "")),
        snapshot_summary={
            "snapshot_at": summary.get("snapshot_at"),
            "symbol": summary.get("symbol"),
            "price": summary.get("price"),
            "holdings": summary.get("holdings"),
            "old_trade_log_reads": 0,
        },
    )
    persisted = persist_final_row(
        db,
        config.collections,
        context=context,
        document=document,
    )
    st.session_state.lego_final_persisted = persisted
    st.session_state.lego_final_document = document
    return persisted, document


def make_audit_event(
    *,
    action: str,
    settings: ConnectionSettings,
    request_summary: dict[str, Any],
    result: Any | None,
    elapsed_ms: float,
    error: Exception | None = None,
) -> dict[str, Any]:
    context: PipelineContext | None = st.session_state.get("lego_context")
    return build_audit_event(
        action=action,
        environment=settings.environment,
        account_id=settings.account_id,
        session_run_id=context.run_id if context is not None else "unknown",
        request_summary=request_summary,
        result=result,
        elapsed_ms=elapsed_ms,
        error=error,
    )


def record_audit(event: dict[str, Any]) -> str | None:
    st.session_state.setdefault("lego_audit_events", []).append(event)
    if st.session_state.get("lego_audit_firestore_off"):
        return None
    config: LegoDashboardConfig | None = st.session_state.get("lego_config")
    db = st.session_state.get("lego_db")
    if config is None or db is None or not config.audit_to_firestore:
        return None
    try:
        db.collection(config.collections.order_audit).document(event["event_id"]).set(
            event
        )
    except Exception as exc:
        st.session_state.lego_audit_firestore_off = True
        return (
            "บันทึก order audit ลง Firestore ไม่ได้ แต่ API result ไม่ได้รับผลกระทบ "
            f"และ audit ยังอยู่ใน session: {exc.__class__.__name__}: {exc}"
        )
    return None


def _run_order_action(
    *,
    action: str,
    settings: ConnectionSettings,
    request_summary: dict[str, Any],
    call: Callable[[], Any],
    preview_state: dict[str, Any] | None = None,
) -> None:
    started = time.perf_counter()
    try:
        result = call()
        safe_result = redact_payload(result)
        elapsed = (time.perf_counter() - started) * 1000
        if preview_state is not None:
            st.session_state.order_final_preview_state = dict(preview_state)
        summary = summarize_order_result(safe_result)
        st.session_state.order_final_output = {
            "action": action,
            "summary": summary,
            "raw": safe_result,
        }
        warning = record_audit(
            make_audit_event(
                action=action,
                settings=settings,
                request_summary=request_summary,
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
                action=action,
                settings=settings,
                request_summary=request_summary,
                result=None,
                elapsed_ms=elapsed,
                error=exc,
            )
        )
        if warning:
            st.warning(warning)
        st.error(f"{exc.__class__.__name__}: {exc}")


def render_final_order_panel(final: pd.DataFrame) -> None:
    st.divider()
    st.subheader("UAT Preview / Submit — หลัง Step 18 เท่านั้น")
    settings: ConnectionSettings | None = st.session_state.get("lego_settings")
    context: PipelineContext | None = st.session_state.get("lego_context")
    persisted = st.session_state.get("lego_final_persisted")
    if settings is None or context is None or persisted is None:
        st.info("ต้อง append final row ที่ Step 18 ก่อนจึงจะ Preview/Submit ได้")
        return
    status = str(final.loc[0, "สถานะ"])
    if status not in {"READY_BUY", "READY_SELL"}:
        st.info(f"{status} เป็น PASS — ไม่มี order ให้ส่ง")
        return
    if settings.environment == PRODUCTION_ENVIRONMENT:
        st.error("Production เป็น read-only; LEGO Dashboard ส่ง order ได้เฉพาะ Test (UAT)")
        return

    symbol = str(final.loc[0, "สินทรัพย์"])
    side = str(final.loc[0, "ฝั่ง"])
    quantity = float(final.loc[0, "จำนวนสั่ง (หุ้น)"])
    client_order_id = manual_tools.generate_client_order_id(
        "LEGO_ONE_ROW",
        symbol,
        context.run_id,
        account_fingerprint(settings.account_id),
        side,
        quantity,
    )
    payload = manual_tools.build_market_order_payload(
        symbol,
        side,
        quantity,
        client_order_id,
    )
    payload_hash = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()
    preview_state = st.session_state.get("order_final_preview_state")
    preview_matches = (
        isinstance(preview_state, dict)
        and preview_state.get("payload_hash") == payload_hash
        and preview_state.get("run_id") == context.run_id
    )
    phrase = order_confirmation_phrase(
        settings.environment,
        settings.account_id,
        side,
        symbol,
        quantity,
    )
    st.json(
        {
            "environment": settings.environment,
            "run_id": context.run_id,
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "client_order_id": client_order_id,
        }
    )
    preview_clicked = st.button("Preview immutable final-row order", key="order_final_preview")
    if preview_clicked:
        client = manual_tools.WebullManualClient(settings)
        _run_order_action(
            action="PREVIEW",
            settings=settings,
            request_summary={
                "run_id": context.run_id,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "client_order_id": client_order_id,
            },
            call=lambda: client.preview_market_order(payload),
            preview_state={"payload_hash": payload_hash, "run_id": context.run_id},
        )
        st.rerun()

    confirmation = st.text_input(
        f"พิมพ์ `{phrase}` เพื่อยืนยัน",
        value="",
        key="order_final_confirm",
        autocomplete="off",
    )
    gate = evaluate_submit_gate(
        environment=settings.environment,
        payload_valid=True,
        preview_matches=preview_matches,
        confirmation_ok=confirmation.strip() == phrase,
        safety_switch=False,
    )
    submit_clicked = st.button(
        "🚀 Submit UAT order",
        type="primary",
        key="order_final_submit",
        disabled=not gate.allowed,
    )
    if not gate.allowed:
        st.caption(" · ".join(gate.reasons))
    if submit_clicked:
        client = manual_tools.WebullManualClient(settings)
        _run_order_action(
            action="SUBMIT",
            settings=settings,
            request_summary={
                "run_id": context.run_id,
                "symbol": symbol,
                "side": side,
                "quantity": quantity,
                "client_order_id": client_order_id,
            },
            call=lambda: client.place_market_order(payload),
        )
        st.rerun()
    output = st.session_state.get("order_final_output")
    if isinstance(output, dict):
        st.json(redact_payload(output))


def render_stage_tab(stage: Any, config: LegoDashboardConfig) -> None:
    st.subheader(f"Step {stage.number} — {stage.title}")
    st.markdown(f"**Goal:** {stage.goal}")
    st.info(f"Quick Start: {stage.quick_start}")
    with st.expander("Single-File LEGO Block", expanded=False):
        st.code(stage.source_code, language="python")
        st.download_button(
            "Download Single-File LEGO Block",
            data=stage.source_code,
            file_name=stage.file_name,
            mime="text/x-python",
            key=f"lego_download_stage_{stage.number}",
        )

    raw: pd.DataFrame | None = st.session_state.get("lego_raw")
    context: PipelineContext | None = st.session_state.get("lego_context")
    results: dict[int, StageResult] = st.session_state.setdefault("lego_results", {})
    ready = (
        raw is not None
        and context is not None
        and (stage.number == 1 or stage.number - 1 in results)
    )
    if not ready:
        prerequisite = "Step 0" if stage.number == 1 else f"Step {stage.number - 1}"
        st.warning(f"ยังรันไม่ได้ — ต้องให้ {prerequisite} สำเร็จก่อน")
    if st.button(
        f"Run LEGO Step {stage.number}",
        disabled=not ready,
        type="primary",
        key=f"lego_run_stage_{stage.number}",
    ):
        assert raw is not None and context is not None
        invalidate_from(results, stage.number)
        try:
            results[stage.number] = run_stage(
                stage.number,
                raw,
                results.get(stage.number - 1),
                context,
            )
        except Exception as exc:
            st.error(f"{exc.__class__.__name__}: {exc}")
    result = results.get(stage.number)
    if result is not None:
        st.success(
            f"Step {stage.number} สำเร็จ — draft row ยังมี 1 แถว และส่งต่อ Step "
            f"{stage.number + 1 if stage.number < 17 else 18}"
        )
        metrics = st.columns(3)
        metrics[0].metric("Draft rows", len(result.frame))
        metrics[1].metric("Columns built", len(result.frame.columns))
        metrics[2].metric(
            "Current value",
            str(result.frame[stage.column_name].iloc[0]),
        )
        st.dataframe(result.frame, use_container_width=True)
        with st.expander("Diagnostics + provenance", expanded=True):
            for diagnostic in result.diagnostics:
                st.write(f"• {diagnostic}")
            st.json(result.provenance)
    with st.expander("คู่มือเรียนรู้ LEGO Block", expanded=result is not None):
        st.write(stage.learning_guide)


def render_auth_tab(config: LegoDashboardConfig) -> None:
    st.subheader("Step 0 — Immutable Webull snapshot + latest anchor")
    st.info(
        "อ่าน Account, Balance, Positions และ Quote ครั้งเดียว สร้าง snapshot หนึ่งแถว "
        "และอ่านเฉพาะ latest final anchor ของ chain เดียวกัน; ไม่อ่าน shannon_demon_trades"
    )
    environment = st.selectbox(
        "Environment",
        options=list(WEBULL_ENDPOINTS),
        index=0,
        key="lego_environment",
    )
    account_id = st.text_input(
        "Account ID", value="", key="lego_account_id", autocomplete="off"
    )
    app_key = st.text_input(
        "App Key", value="", type="password", key="lego_app_key", autocomplete="off"
    )
    app_secret = st.text_input(
        "App Secret",
        value="",
        type="password",
        key="lego_app_secret",
        autocomplete="off",
    )
    symbol = st.text_input(
        "Symbol",
        value="",
        key="lego_symbol_input",
        help="จำเป็นสำหรับ current quote และ positions snapshot",
    ).strip().upper()
    dna_code = st.text_input(
        "DNA_CODE",
        value="bypass:100",
        key="lego_dna_code_input",
    ).strip()
    parameters = st.columns(3)
    with parameters[0]:
        fix_c = st.number_input(
            "FIX_C",
            min_value=0.00001,
            value=float(config.fix_c),
            format="%.2f",
            key="lego_fix_c_input",
        )
    with parameters[1]:
        diff = st.number_input(
            "DIFF",
            min_value=0.0,
            value=float(config.diff),
            format="%.2f",
            key="lego_diff_input",
        )
    with parameters[2]:
        decimal_precision = st.number_input(
            "Quantity precision",
            min_value=0,
            max_value=12,
            value=int(config.decimal_precision),
            step=1,
            key="lego_precision_input",
        )
    settings = ConnectionSettings(
        environment=environment,
        account_id=account_id,
        app_key=app_key,
        app_secret=app_secret,
        region="th",
    )
    current_fingerprint = connection_fingerprint(
        settings,
        symbol=symbol,
        dna_code=dna_code,
        fix_c=float(fix_c),
        diff=float(diff),
        decimal_precision=int(decimal_precision),
    )
    stored = st.session_state.get("lego_auth_fingerprint")
    if stored and stored != current_fingerprint:
        clear_connection_state(clear_widgets=False)
        st.warning("Connection หรือ strategy input เปลี่ยน — ล้าง draft/final เดิมแล้ว")
    actions = st.columns(2)
    with actions[0]:
        connect_clicked = st.button(
            "Connect & Create New Draft Row",
            type="primary",
            use_container_width=True,
            key="lego_connect_button",
        )
    with actions[1]:
        st.button(
            "Clear credentials + reset",
            use_container_width=True,
            on_click=clear_connection_state,
            key="lego_clear_button",
        )
    if connect_clicked:
        try:
            with st.spinner("อ่าน Webull snapshot และ latest LEGO anchor..."):
                result = authenticate_and_load(
                    settings,
                    config,
                    symbol=symbol,
                    dna_code=dna_code,
                    fix_c=float(fix_c),
                    diff=float(diff),
                    decimal_precision=int(decimal_precision),
                )
            _store_step_zero(
                result,
                settings,
                config,
                symbol=symbol,
                dna_code=dna_code,
                fix_c=float(fix_c),
                diff=float(diff),
                decimal_precision=int(decimal_precision),
                auth_fingerprint=current_fingerprint,
            )
        except Exception as exc:
            clear_connection_state(clear_widgets=False)
            st.error(f"{exc.__class__.__name__}: {exc}")
    summary = st.session_state.get("lego_auth_summary")
    if isinstance(summary, dict):
        st.success("Step 0 สำเร็จ — สร้าง immutable draft source 1 แถว")
        raw = st.session_state.get("lego_raw")
        snapshot_rows, old_trade_log_reads, anchor_version, run_id = (
            _auth_summary_metrics(summary, raw)
        )
        metrics = st.columns(4)
        metrics[0].metric("Snapshot rows", snapshot_rows)
        metrics[1].metric("Old trade-log reads", old_trade_log_reads)
        metrics[2].metric("Anchor version", anchor_version)
        metrics[3].metric("Run ID", run_id)
        with st.expander("Authenticated output (redacted)", expanded=True):
            st.json(redact_payload(summary))
        if isinstance(raw, pd.DataFrame):
            st.dataframe(raw, use_container_width=True)
        else:
            st.warning("Step 0 session เก่าไม่ครบถ้วน — กรุณา Connect ใหม่")
    elif not config.firebase_info:
        st.warning("ยังไม่มี [firebase_service_account] ใน Streamlit secrets")


def render_final_tab(config: LegoDashboardConfig) -> None:
    st.subheader("Step 18 — Validate + append exactly one final row")
    results: dict[int, StageResult] = st.session_state.get("lego_results", {})
    if 17 not in results:
        completed = len([number for number in range(1, 18) if number in results])
        st.warning(f"Final ยังล็อกอยู่ — สำเร็จ {completed}/17 steps")
        return
    final = final_dataframe(results[17])
    st.dataframe(final, use_container_width=True)
    persisted = st.session_state.get("lego_final_persisted")
    if persisted is None:
        if st.button(
            "Finalize Step 18 + Append New Row",
            type="primary",
            key="lego_finalize_button",
        ):
            try:
                persisted, _ = _persist_completed_row(config)
                st.success(
                    f"append สำเร็จ · run_id={persisted.run_id} · version={persisted.version}"
                )
                st.rerun()
            except StaleAnchorError as exc:
                st.session_state.lego_results = {}
                st.error(f"{exc} — กลับไป Step 0 เพื่อสร้าง snapshot ใหม่")
            except Exception as exc:
                st.error(f"{exc.__class__.__name__}: {exc}")
    else:
        verb = "created" if persisted.created else "idempotent existing"
        st.success(
            f"Step 18 persisted ({verb}) · run_id={persisted.run_id} · "
            f"anchor version={persisted.version}"
        )
        st.download_button(
            "Download Current Final Row CSV",
            data=final.to_csv(index=False).encode("utf-8-sig"),
            file_name=f"webull_lego_{persisted.run_id}.csv",
            mime="text/csv",
        )
        st.download_button(
            "Download Current Final Row JSON",
            data=_download_json(st.session_state.get("lego_final_document")),
            file_name=f"webull_lego_{persisted.run_id}.json",
            mime="application/json",
        )
        render_final_order_panel(final)
    audit_events = st.session_state.get("lego_audit_events", [])
    if audit_events:
        st.download_button(
            "Download sanitized order audit JSON",
            data=_download_json(audit_events),
            file_name="webull_lego_order_audit.json",
            mime="application/json",
        )


def render_all_in_sidebar(config: LegoDashboardConfig) -> None:
    with st.sidebar:
        st.header("🧱 All-in Loop 0→18")
        st.caption(
            "ยิง Step 0 ใหม่ สร้าง one-row draft, คำนวณ 1→17 และ append ที่ Step 18 "
            "แบบ transaction; ไม่ส่ง order อัตโนมัติ"
        )
        settings: ConnectionSettings | None = st.session_state.get("lego_settings")
        run_clicked = st.button(
            "Run ALL 0 → 18 (NEW ROW)",
            type="primary",
            use_container_width=True,
            disabled=settings is None,
            key="lego_all_in_button",
        )
        if settings is None:
            st.warning("กรอก credential และ Connect ที่ Step 0 ก่อน")
        if run_clicked and settings is not None:
            started = time.perf_counter()
            current_step = 0
            progress = st.progress(0.0, text="Step 0 · immutable real reads")
            try:
                result = authenticate_and_load(
                    settings,
                    config,
                    symbol=str(st.session_state.lego_symbol),
                    dna_code=str(st.session_state.lego_dna_code),
                    fix_c=float(st.session_state.lego_fix_c),
                    diff=float(st.session_state.lego_diff),
                    decimal_precision=int(st.session_state.lego_decimal_precision),
                )
                auth_fingerprint = connection_fingerprint(
                    settings,
                    symbol=str(st.session_state.lego_symbol),
                    dna_code=str(st.session_state.lego_dna_code),
                    fix_c=float(st.session_state.lego_fix_c),
                    diff=float(st.session_state.lego_diff),
                    decimal_precision=int(st.session_state.lego_decimal_precision),
                )
                _store_step_zero(
                    result,
                    settings,
                    config,
                    symbol=str(st.session_state.lego_symbol),
                    dna_code=str(st.session_state.lego_dna_code),
                    fix_c=float(st.session_state.lego_fix_c),
                    diff=float(st.session_state.lego_diff),
                    decimal_precision=int(st.session_state.lego_decimal_precision),
                    auth_fingerprint=auth_fingerprint,
                )
                progress.progress(1 / 19, text="Step 0 สำเร็จ · one snapshot row")

                def update_progress(step: int) -> None:
                    nonlocal current_step
                    current_step = step
                    label = "Persist final row" if step == 18 else STAGES[step - 1].title
                    progress.progress((step + 1) / 19, text=f"Step {step} · {label}")

                results = run_all_pipeline_stages(
                    result.raw,
                    result.context,
                    on_step=update_progress,
                )
                st.session_state.lego_results = results
                persisted, _ = _persist_completed_row(config)
                elapsed = time.perf_counter() - started
                st.session_state.lego_all_in_status = {
                    "ok": True,
                    "step": 18,
                    "rows": 1,
                    "run_id": persisted.run_id,
                    "created": persisted.created,
                    "elapsed_seconds": elapsed,
                }
                st.rerun()
            except Exception as exc:
                st.session_state.lego_all_in_status = {
                    "ok": False,
                    "step": current_step,
                    "error_type": exc.__class__.__name__,
                }
                st.error(f"{exc.__class__.__name__}: {exc}")
        status = st.session_state.get("lego_all_in_status")
        if isinstance(status, dict):
            if status.get("ok"):
                st.success(
                    f"ครบ 0→18 · 1 new row · {status.get('elapsed_seconds', 0):.3f}s"
                )
            else:
                st.error(
                    f"หยุดที่ Step {status.get('step')}: {status.get('error_type')}"
                )


config = load_dashboard_config()
st.title("🧱 Webull LEGO Chain")
st.caption(
    "Step 0 immutable snapshot → Steps 1–17 calculate one draft row → "
    "Step 18 transactional append → optional UAT Submit"
)
render_all_in_sidebar(config)

tab_labels = ["0 · Authenticated connection"] + [
    f"{stage.number} · {stage.title}" for stage in STAGES
] + ["18 · Final DataFrame"]
tabs = st.tabs(tab_labels)
with tabs[0]:
    render_auth_tab(config)
for stage, tab in zip(STAGES, tabs[1:18]):
    with tab:
        render_stage_tab(stage, config)
with tabs[18]:
    render_final_tab(config)
