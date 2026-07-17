"""Webull LEGO Chain — a manually-run, one-new-row Streamlit learning app.

A single authenticated run reads one immutable Webull snapshot (positions +
quote — never the ``shannon_demon_trades`` log) and the latest recurrence anchor
of the same chain, computes exactly one new 17-column row, and — only at Step
18 — appends it transactionally.  Preview/Submit is available afterwards for the
committed row, UAT only; Production is read-only.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import time
import uuid
from typing import Any

import pandas as pd
import streamlit as st

import lego_one_row as _engine

EXPECTED_ONE_ROW_SCHEMA_VERSION = 1
if getattr(_engine, "ONE_ROW_SCHEMA_VERSION", 0) != EXPECTED_ONE_ROW_SCHEMA_VERSION:
    import importlib

    _engine = importlib.reload(_engine)

from lego_one_row import (
    DECISION_STAGE,
    FINAL_COLUMNS,
    STAGE_SPECS,
    STATUS_READY_BUY,
    STATUS_READY_SELL,
    ComputedRow,
    RunContext,
    StrategyParameters,
    account_fingerprint,
    anchor_from_state,
    build_final_document,
    build_snapshot,
    compute_chain_key,
    compute_row,
    compute_run_id,
    present_row,
    present_value,
)
from lego_state import (
    ORDER_AUDIT_COLLECTION,
    FirestoreStateStore,
    StaleAnchorError,
    finalize_row,
    read_anchor_state,
)
from manual_tools import (
    DEFAULT_ORDER_DECIMAL_PRECISION,
    WEBULL_ENDPOINTS,
    ConnectionSettings,
    WebullManualClient,
    build_market_order_payload,
    first_value,
    generate_client_order_id,
    iter_dicts,
)
from lego_uat import build_audit_event, redact_payload
from lego_orders import (
    PRODUCTION_ENVIRONMENT,
    summarize_order_result,
)


st.set_page_config(page_title="Webull LEGO Chain", page_icon="🧱", layout="wide")


@dataclass(frozen=True)
class LegoDashboardConfig:
    firebase_info: dict[str, Any]
    order_audit_collection: str = ORDER_AUDIT_COLLECTION
    fix_c: float = 1500.0
    diff: float = 0.0
    dna_code: str = "bypass:100"
    decimal_precision: int = DEFAULT_ORDER_DECIMAL_PRECISION
    audit_to_firestore: bool = False


def _secret_section(name: str) -> dict[str, Any]:
    try:
        return dict(st.secrets[name])
    except (KeyError, FileNotFoundError, TypeError):
        return {}


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def load_dashboard_config() -> LegoDashboardConfig:
    firebase_info = _secret_section("firebase_service_account")
    lego = _secret_section("lego_dashboard")
    try:
        fix_c = float(lego.get("fix_c", 1500.0))
        diff = float(lego.get("diff", 0.0))
        precision = int(lego.get("decimal_precision", DEFAULT_ORDER_DECIMAL_PRECISION))
    except (TypeError, ValueError) as exc:
        raise ValueError("lego_dashboard fix_c/diff/decimal_precision must be numeric") from exc
    if fix_c <= 0:
        raise ValueError("lego_dashboard.fix_c must be greater than 0")
    if diff < 0:
        raise ValueError("lego_dashboard.diff cannot be negative")
    return LegoDashboardConfig(
        firebase_info=firebase_info,
        order_audit_collection=str(
            lego.get("order_audit_collection", ORDER_AUDIT_COLLECTION)
        ).strip(),
        fix_c=fix_c,
        diff=diff,
        dna_code=str(lego.get("dna_code", "bypass:100")).strip() or "bypass:100",
        decimal_precision=precision,
        audit_to_firestore=_coerce_bool(lego.get("audit_to_firestore", False)),
    )


# --------------------------------------------------------------------------- #
# Firestore + Step 0 reads
# --------------------------------------------------------------------------- #
def _make_firestore_client(firebase_info: dict[str, Any]):
    from google.cloud import firestore
    from google.oauth2 import service_account

    if firebase_info:
        credentials = service_account.Credentials.from_service_account_info(firebase_info)
        return firestore.Client(credentials=credentials, project=firebase_info["project_id"])
    return firestore.Client()


def _build_strategy_params(config: LegoDashboardConfig, dna_code: str) -> StrategyParameters:
    return StrategyParameters(
        fix_c=config.fix_c,
        diff=config.diff,
        dna_code=dna_code or config.dna_code,
        decimal_precision=config.decimal_precision,
    )


def connect_and_prepare_run(
    settings: ConnectionSettings,
    config: LegoDashboardConfig,
    *,
    symbol: str,
    dna_code: str,
) -> tuple[RunContext, Any, ComputedRow, dict[str, Any]]:
    """Real Step 0: one immutable Webull snapshot + latest chain anchor.

    Reads Account list / Balance / Positions / Quote once and the chain's latest
    state pointer.  It never queries ``shannon_demon_trades`` or any trade log.
    """

    settings.validate()
    normalized_symbol = symbol.strip().upper()
    if not normalized_symbol:
        raise ValueError("Symbol is required — the new row's price comes from a live quote")
    if not config.firebase_info:
        raise ValueError("Missing [firebase_service_account] in Streamlit secrets")

    params = _build_strategy_params(config, dna_code)  # validates DNA + config

    client = WebullManualClient(settings)
    account_list = client.get_account_list()
    balance = client.get_account_balance()
    position_and_price = client.get_position_and_price(normalized_symbol)
    positions_response = position_and_price["position_response"]
    quote_response = position_and_price["quote_response"]

    snapshot = build_snapshot(
        environment=settings.environment,
        account_id=settings.account_id,
        symbol=normalized_symbol,
        positions_response=positions_response,
        quote_response=quote_response,
    )

    db = _make_firestore_client(config.firebase_info)
    store = FirestoreStateStore(db)
    chain_key = compute_chain_key(
        snapshot.environment, snapshot.account_fingerprint, snapshot.symbol, params
    )
    anchor = anchor_from_state(read_anchor_state(store, chain_key))
    # A per-preparation random nonce guarantees a unique run_id for each Connect
    # & Load / Run ALL, even when price/holdings/anchor are unchanged.  The id is
    # then held for this prepared run, so re-clicking Step 18 stays idempotent.
    run_id = compute_run_id(chain_key, anchor, snapshot, nonce=uuid.uuid4().hex)
    ctx = RunContext(
        run_id=run_id, chain_key=chain_key, snapshot=snapshot, anchor=anchor, params=params
    )
    computed = compute_row(ctx)

    summary = {
        "environment": settings.environment,
        "endpoint": settings.endpoint,
        "account_fingerprint": snapshot.account_fingerprint,
        "symbol": snapshot.symbol,
        "api_reads": ["account_list", "account_balance", "positions", "market_snapshot"],
        "snapshot": {
            "price": snapshot.price,
            "holdings": snapshot.holdings,
            "captured_at": snapshot.captured_at,
        },
        "chain": {
            "chain_key": chain_key,
            "run_id": run_id,
            "anchor_exists": anchor.exists,
            "anchor_version": anchor.version,
            "next_dna_step": computed.columns["DNA step"],
        },
        "account_list": redact_payload(account_list),
        "balance": redact_payload(balance),
        "positions": redact_payload(positions_response),
        "quote": redact_payload(quote_response),
        "firestore_project": config.firebase_info.get("project_id"),
        "old_trade_log_reads": 0,
    }
    return ctx, store, computed, summary


def clear_run_state() -> None:
    for key in (
        "lego_ctx",
        "lego_store",
        "lego_computed",
        "lego_revealed",
        "lego_commit_result",
        "lego_auth_summary",
        "lego_settings",
        "lego_order_audit_events",
    ):
        st.session_state.pop(key, None)
    for key in list(st.session_state.keys()):
        if str(key).startswith("order_"):
            st.session_state.pop(key, None)


# --------------------------------------------------------------------------- #
# Draft-row presentation
# --------------------------------------------------------------------------- #
def draft_row_frame(computed: ComputedRow, revealed: int) -> pd.DataFrame:
    """One-row DataFrame with the columns revealed so far, presented for display."""

    revealed = max(0, min(17, int(revealed)))
    row: dict[str, Any] = {}
    for index in range(revealed):
        name = FINAL_COLUMNS[index]
        value = computed.columns[name]
        if name == "สถานะ" and revealed < DECISION_STAGE:
            value = _engine.STATUS_SNAPSHOT_READY
        row[name] = present_value(name, value)
    return pd.DataFrame([row]) if row else pd.DataFrame()


def final_row_frame(computed: ComputedRow) -> pd.DataFrame:
    return pd.DataFrame([present_row(computed.columns)])


# --------------------------------------------------------------------------- #
# Order panel — UAT only, from the immutable committed final row
# --------------------------------------------------------------------------- #
def _record_order_audit(config: LegoDashboardConfig, event: dict[str, Any]) -> str | None:
    st.session_state.setdefault("lego_order_audit_events", []).append(event)
    store = st.session_state.get("lego_store")
    if store is None or not getattr(config, "audit_to_firestore", False):
        return None
    if st.session_state.get("lego_audit_firestore_off"):
        return None
    try:
        store.record_order_audit(event)
    except Exception as exc:
        st.session_state["lego_audit_firestore_off"] = True
        return (
            "บันทึก order audit ลง Firestore ไม่ได้ — order ไม่ได้รับผลกระทบ และ audit ถูกเก็บใน "
            f"session แล้ว เหตุผล: {exc.__class__.__name__}: {exc}"
        )
    return None


def _business_message(result: Any) -> str | None:
    """Pull a Webull business error/message (code/msg) out of any response shape."""

    for node in iter_dicts(result):
        message = first_value(
            node, "msg", "message", "error_msg", "errorMsg", "detail", "description"
        )
        if message:
            code = first_value(node, "code", "error_code", "errorCode")
            return f"{str(code) + ': ' if code not in (None, '') else ''}{message}"
    return None


def _order_badge(
    action: str, summary_view: dict[str, Any], raw: Any = None
) -> tuple[str | None, str]:
    """Human-readable status that never mislabels a fill and confirms delivery."""

    status = summary_view.get("status")
    category = summary_view.get("status_category")
    order_id = summary_view.get("order_id")
    label = status or category
    if action == "PREVIEW":
        message = _business_message(raw)
        if message and not order_id:
            return (f"Preview ตอบกลับ: {message} — ตรวจ payload/สิทธิ์เทรดก่อน Submit", "warning")
        return ("Preview สำเร็จ — Webull ตรวจ payload แล้ว (ยังไม่ได้ส่งคำสั่ง)", "success")
    if action == "SUBMIT":
        if summary_view.get("is_filled"):
            return (f"ส่งคำสั่งแล้ว · order_id={order_id} · FILLED", "success")
        if order_id:
            return (
                f"✅ ส่งถึง Webull แล้ว · order_id={order_id} · สถานะ={label} — "
                "SUBMITTED/PENDING ยังไม่ใช่ FILLED; UAT เป็น paper จึงไม่กระทบ holdings จริง",
                "warning",
            )
        message = _business_message(raw)
        return (
            "⚠️ Webull ไม่ได้สร้าง order — "
            + (f"เหตุผลจาก Webull: {message}" if message else "ดู raw ด้านล่าง")
            + " · (market ปิด / ไม่มีสิทธิ์เทรด symbol นี้ / payload ไม่ผ่าน) กด Query ตรวจซ้ำ",
            "error",
        )
    if action == "QUERY":
        if summary_view.get("is_filled"):
            return (f"order_id={order_id} · FILLED", "success")
        if order_id:
            return (f"พบคำสั่งที่ Webull · order_id={order_id} · สถานะ={label}", "info")
        return ("Webull ไม่พบคำสั่งนี้ — order อาจยังไม่ถูกส่งสำเร็จ", "error")
    return (None, "info")


def _run_order_action(
    *,
    action: str,
    settings: ConnectionSettings,
    config: LegoDashboardConfig,
    request_summary: dict[str, Any],
    call,
    prefix: str,
    store_preview: dict[str, Any] | None = None,
) -> None:
    started = time.perf_counter()
    try:
        result = call()
        safe_result = redact_payload(result)
        elapsed = (time.perf_counter() - started) * 1000
        summary_view = summarize_order_result(safe_result)
        if store_preview is not None:
            st.session_state[f"{prefix}_preview_state"] = dict(store_preview)
        badge, level = _order_badge(action, summary_view, raw=safe_result)
        st.session_state[f"{prefix}_output"] = {
            "action": action,
            "result": {"summary": summary_view, "raw": safe_result},
            "badge": badge,
            "level": level,
        }
        warning = _record_order_audit(
            config,
            build_audit_event(
                action=action,
                environment=settings.environment,
                account_id=settings.account_id,
                session_run_id=st.session_state.get("lego_session_run_id", "unknown"),
                request_summary=request_summary,
                result=safe_result,
                elapsed_ms=elapsed,
            ),
        )
        if warning:
            st.warning(warning)
    except Exception as exc:
        elapsed = (time.perf_counter() - started) * 1000
        warning = _record_order_audit(
            config,
            build_audit_event(
                action=action,
                environment=settings.environment,
                account_id=settings.account_id,
                session_run_id=st.session_state.get("lego_session_run_id", "unknown"),
                request_summary=request_summary,
                result=None,
                elapsed_ms=elapsed,
                error=exc,
            ),
        )
        if warning:
            st.warning(warning)
        st.error(f"{exc.__class__.__name__}: {exc}")


def render_order_panel(config: LegoDashboardConfig) -> None:
    """Submit the committed final row's order — mirrors the proven Manual page.

    Like ``pages/Manual.py``: an "armed" checkbox plus an exact confirmation
    phrase gate a direct ``place_market_order``; Preview is optional and never
    gates Submit.  Works in UAT (paper) and Production (real money).  The symbol,
    side, and quantity are locked to the immutable final row.
    """

    st.divider()
    st.markdown("### 🔴 ส่งคำสั่งจาก final row (หลัง Step 18)")
    settings: ConnectionSettings | None = st.session_state.get("lego_settings")
    commit_result = st.session_state.get("lego_commit_result")
    computed: ComputedRow | None = st.session_state.get("lego_computed")
    ctx: RunContext | None = st.session_state.get("lego_ctx")
    if settings is None or ctx is None or computed is None:
        st.info("ต้อง Connect & Load ที่ Tab 0 ก่อน")
        return
    if commit_result is None:
        st.info("ต้องกด Step 18 (append final row) ให้สำเร็จก่อน จึงจะส่ง order ได้")
        return

    decision = computed.decision
    if decision.status not in (STATUS_READY_BUY, STATUS_READY_SELL):
        st.info(
            f"final row เป็น {decision.status} — ไม่มี order ให้ส่ง (เปิดเฉพาะ READY_BUY / READY_SELL)"
        )
        return

    symbol = ctx.snapshot.symbol
    side = decision.side or "BUY"
    quantity = float(decision.quantity)
    is_production = settings.environment == PRODUCTION_ENVIRONMENT
    environment_word = "PRODUCTION" if is_production else "UAT"
    if is_production:
        st.error(
            "โหมด Production — คำสั่งนี้เป็น **เงินจริงและย้อนกลับไม่ได้** ตรวจ account/side/quantity ให้ชัวร์"
        )
    else:
        st.warning(
            "โหมด Test (UAT) — ยิง UAT endpoint จริง (paper) payload มาจาก final row ที่บันทึกแล้ว"
        )
    st.caption(
        f"Account #{account_fingerprint(settings.account_id)} · {symbol} · {side} · {quantity} "
        f"· run_id {ctx.run_id[:12]}… · ค่าเหล่านี้ล็อกจาก final row แก้ไม่ได้"
    )

    prefix = f"order_{ctx.run_id[:8]}"
    trading_session = st.selectbox(
        "Trading session", ["CORE", "PRE", "AFTER", "OVERNIGHT"], key=f"{prefix}_session"
    )
    # Deterministic client_order_id from the immutable run identity.
    client_order_id = generate_client_order_id(
        "LEGO", symbol, account_fingerprint(settings.account_id), side, quantity, ctx.run_id
    )
    try:
        payload = build_market_order_payload(
            symbol, side, quantity, client_order_id, trading_session
        )
        st.markdown("**Payload ที่จะส่ง (redacted)**")
        st.json(redact_payload(payload))
    except Exception as exc:
        payload = None
        st.error(f"{exc.__class__.__name__}: {exc}")

    request_summary = {
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "client_order_id": client_order_id,
        "trading_session": trading_session,
        "environment": settings.environment,
        "run_id": ctx.run_id,
    }

    # Preview is optional — like pages/Manual.py it never gates Submit.
    if st.button("Preview order (optional)", disabled=payload is None, key=f"{prefix}_preview"):
        _run_order_action(
            action="PREVIEW",
            settings=settings,
            config=config,
            request_summary=request_summary,
            call=lambda: WebullManualClient(settings).preview_market_order(payload),
            prefix=prefix,
        )

    # Submit gate mirrors the working Manual page: armed checkbox + exact phrase.
    confirmation_phrase = f"PLACE {environment_word} {side} {symbol} {quantity:g}"
    st.error("ปุ่มด้านล่างเรียก place_order จริง (ส่งคำสั่งเข้าตลาด)")
    armed = st.checkbox("ฉันเข้าใจว่านี่คือการส่งคำสั่งจริง", key=f"{prefix}_armed")
    confirmation = st.text_input(
        f"พิมพ์ให้ตรง: {confirmation_phrase}", value="", key=f"{prefix}_confirm"
    )
    can_submit = armed and confirmation.strip() == confirmation_phrase and payload is not None
    if not can_submit:
        st.caption("ต้องติ๊กยืนยัน + พิมพ์ phrase ให้ตรงก่อน จึงจะกด Submit ได้ (ไม่ต้อง Preview ก่อนก็ได้)")

    if st.button(
        f"🚀 Submit to {environment_word}",
        disabled=not can_submit,
        type="primary",
        key=f"{prefix}_submit",
    ):
        _run_order_action(
            action="SUBMIT",
            settings=settings,
            config=config,
            request_summary=request_summary,
            call=lambda: WebullManualClient(settings).place_market_order(payload),
            prefix=prefix,
        )

    st.markdown("#### ตรวจว่าคำสั่งถึง Webull จริง (Query)")
    st.caption(
        "UAT เป็น paper trading — holdings จริงไม่ขยับ และ draft row ใช้ snapshot ตอน Step 0 "
        "(ต้อง Connect ใหม่เพื่อเห็น holdings อัปเดต) · ใช้ Query เพื่อยืนยันว่า Webull รับ order แล้ว"
    )
    if st.button("Query order status", key=f"{prefix}_query"):
        _run_order_action(
            action="QUERY",
            settings=settings,
            config=config,
            request_summary={"client_order_id": client_order_id},
            call=lambda: WebullManualClient(settings).get_order_detail(client_order_id),
            prefix=prefix,
        )

    output = st.session_state.get(f"{prefix}_output")
    if output:
        badge = output.get("badge")
        if badge:
            getattr(st, output.get("level", "info"))(badge)
        st.markdown("#### ผลลัพธ์ล่าสุด (redacted)")
        st.json(output.get("result"))

    audit_events = st.session_state.get("lego_order_audit_events", [])
    if audit_events:
        st.download_button(
            "Download sanitized order audit JSON",
            data=json.dumps(
                redact_payload(audit_events), ensure_ascii=False, indent=2, default=str
            ).encode("utf-8"),
            file_name="webull_lego_order_audit.json",
            mime="application/json",
            key=f"{prefix}_audit_download",
        )


# --------------------------------------------------------------------------- #
# Tab 0 — authenticated connection + Step 0
# --------------------------------------------------------------------------- #
def render_auth_tab(config: LegoDashboardConfig) -> None:
    st.subheader("Step 0 — Authenticated snapshot + chain anchor")
    st.info(
        "กด Connect & Load เพื่ออ่าน Webull Account/Balance/Positions/Quote ครั้งเดียว และอ่าน "
        "latest final anchor ของ chain เดียวกัน — จากนั้นจะสร้าง draft row ใหม่ 1 แถว "
        "(ไม่มีการอ่าน shannon_demon_trades)"
    )
    environment = st.selectbox(
        "Environment", options=list(WEBULL_ENDPOINTS), index=0, key="lego_environment"
    )
    st.code(WEBULL_ENDPOINTS[environment], language=None)
    account_id = st.text_input("Account ID", value="", key="lego_account_id", autocomplete="off")
    app_key = st.text_input("App Key", value="", type="password", key="lego_app_key", autocomplete="off")
    app_secret = st.text_input("App Secret", value="", type="password", key="lego_app_secret", autocomplete="off")
    symbol = st.text_input(
        "Symbol (จำเป็น — ราคา Pₙ มาจาก live quote ของ symbol นี้)", value="", key="lego_symbol_input"
    ).strip().upper()
    dna_code = st.text_input(
        "DNA_CODE (encoded, bypass:N หรือ [1,N])", value=config.dna_code, key="lego_dna_code_input"
    ).strip()

    st.caption(
        f"Chain config: FIX_C=${config.fix_c:,.2f} · DIFF=${config.diff:,.2f} · "
        f"precision={config.decimal_precision} — เปลี่ยนค่าเหล่านี้ใน secrets เพื่อเริ่ม chain ใหม่"
    )

    settings = ConnectionSettings(
        environment=environment,
        account_id=account_id,
        app_key=app_key,
        app_secret=app_secret,
        region="th",
    )

    left, right = st.columns(2)
    with left:
        connect_clicked = st.button(
            "Connect & Load", type="primary", use_container_width=True, key="lego_connect_button"
        )
    with right:
        st.button(
            "Clear + reset run", use_container_width=True, on_click=clear_run_state, key="lego_clear_button"
        )

    if connect_clicked:
        try:
            with st.spinner("ยิง Webull API จริง + อ่าน chain anchor..."):
                ctx, store, computed, summary = connect_and_prepare_run(
                    settings, config, symbol=symbol, dna_code=dna_code
                )
            st.session_state.lego_settings = settings
            st.session_state.lego_store = store
            st.session_state.lego_ctx = ctx
            st.session_state.lego_computed = computed
            st.session_state.lego_config = config
            st.session_state.lego_revealed = 0
            st.session_state.lego_commit_result = None
            st.session_state.lego_auth_summary = summary
            st.session_state.lego_order_audit_events = []
        except Exception as exc:
            clear_run_state()
            st.error(f"{exc.__class__.__name__}: {exc}")

    summary = st.session_state.get("lego_auth_summary")
    if summary is not None:
        safe = redact_payload(summary)
        st.success(
            "Step 0 สำเร็จ — draft row ใหม่ 1 แถวพร้อมแล้ว "
            f"(DNA step {safe['chain']['next_dna_step']}, "
            f"{'มี anchor' if safe['chain']['anchor_exists'] else 'chain ใหม่'})"
        )
        cols = st.columns(4)
        cols[0].metric("Environment", safe["environment"])
        cols[1].metric("Price Pₙ", f"${safe['snapshot']['price']:,.2f}")
        cols[2].metric("Holdings", safe["snapshot"]["holdings"])
        cols[3].metric("Old trade-log reads", safe["old_trade_log_reads"])
        with st.expander("All authenticated output (redacted)"):
            st.json(safe)
        st.download_button(
            "Download authenticated output JSON",
            data=json.dumps(safe, ensure_ascii=False, indent=2, default=str).encode("utf-8"),
            file_name="webull_lego_authenticated_output.json",
            mime="application/json",
        )
    elif not config.firebase_info:
        st.warning(
            "ยังไม่มี [firebase_service_account] — Connect & Load จะ fail-closed จนกว่าจะตั้ง secrets"
        )

    st.markdown("#### Learning Guide")
    st.write(
        "Tab 0 อ่าน Webull SDK จริง 4 read endpoints (Account/Balance/Positions/Quote) และอ่าน "
        "เฉพาะ latest anchor ของ chain ผ่าน webull_lego_state/webull_lego_rows — ไม่แตะ trade log "
        "หรือประวัติหลายแถว credential และ raw response อยู่ใน session เท่านั้น"
    )
    _render_single_file_expander("auth")


def _render_single_file_expander(key: str) -> None:
    try:
        source = Path(__file__).with_name("webull_lego_single_file.py").read_text(encoding="utf-8")
    except OSError:
        return
    with st.expander("Real All-in 0→18 — Single File", expanded=False):
        st.caption("ไฟล์เดียวรัน Step 0→18 ด้วย engine เดียวกัน (read-only reads; ไม่มี place/cancel)")
        st.code(source, language="python")
        st.download_button(
            "Download Real All-in Single File",
            data=source,
            file_name="webull_lego_single_file.py",
            mime="text/x-python",
            key=f"lego_download_single_file_{key}",
        )


# --------------------------------------------------------------------------- #
# Tabs 1–17 — reveal one column of the single new row
# --------------------------------------------------------------------------- #
def render_stage_tab(spec, config: LegoDashboardConfig) -> None:
    st.subheader(f"Step {spec.number} — {spec.title}")
    st.info(f"Quick Start: {spec.quick_start}")

    computed: ComputedRow | None = st.session_state.get("lego_computed")
    revealed = int(st.session_state.get("lego_revealed", 0))
    ready = computed is not None and (spec.number == 1 or revealed >= spec.number - 1)
    if computed is None:
        st.warning("ยังรันไม่ได้ — ต้อง Connect & Load ที่ Tab 0 ก่อน")
    elif not ready:
        st.warning(f"ยังรันไม่ได้ — ต้อง Run Step {spec.number - 1} ก่อน")

    if st.button(
        f"Run LEGO Step {spec.number}", disabled=not ready, type="primary", key=f"lego_run_stage_{spec.number}"
    ):
        st.session_state.lego_revealed = max(revealed, spec.number)
        revealed = st.session_state.lego_revealed

    if computed is not None and revealed >= spec.number:
        stage = computed.stages[spec.number - 1]
        st.success(
            f"Step {spec.number} สำเร็จ — คอลัมน์ '{spec.column_name}' ของ draft row ถูกคำนวณแล้ว"
        )
        st.markdown("#### Draft row (แถวเดียว)")
        st.dataframe(draft_row_frame(computed, revealed), use_container_width=True)
        with st.expander("Diagnostics + provenance", expanded=True):
            for diagnostic in stage.diagnostics:
                st.write(f"• {diagnostic}")
            st.json(stage.provenance)
            st.caption(f"upstream hash: {stage.input_hash[:16]}… · output hash: {stage.output_hash[:16]}…")

    with st.expander("คู่มือเรียนรู้ LEGO Block", expanded=computed is not None and revealed >= spec.number):
        st.write(spec.learning_guide)
        st.caption("หลักคิดใหม่: snapshot ปัจจุบัน + anchor ล่าสุด → คำนวณ 1 ค่า → เติมลง draft row แถวเดียว")
        st.caption(
            "โค้ดคำนวณทั้ง 17 คอลัมน์อยู่ใน engine เดียว (lego_one_row.py) และไฟล์เดียว "
            "webull_lego_single_file.py — ดาวน์โหลด All-in Single File ได้ที่ Tab 0 หรือ sidebar"
        )


# --------------------------------------------------------------------------- #
# Tab 18 — validate + transactional append + order panel
# --------------------------------------------------------------------------- #
def commit_final_row_action(config: LegoDashboardConfig) -> None:
    ctx: RunContext = st.session_state["lego_ctx"]
    store = st.session_state["lego_store"]
    computed: ComputedRow = st.session_state["lego_computed"]
    try:
        result = finalize_row(store, ctx, computed)
        st.session_state.lego_commit_result = result
    except StaleAnchorError as exc:
        st.session_state.lego_commit_result = None
        st.error(
            f"Anchor ล้าสมัย — {exc}. มี run อื่นต่อ chain ไปแล้ว กรุณากลับไป Tab 0 กด Connect & Load "
            "ใหม่เพื่ออ่าน anchor ล่าสุด"
        )
    except Exception as exc:
        st.session_state.lego_commit_result = None
        st.error(f"{exc.__class__.__name__}: {exc}")


def render_final_tab(config: LegoDashboardConfig) -> None:
    st.subheader("Step 18 — Validate + append final row (transaction)")
    computed: ComputedRow | None = st.session_state.get("lego_computed")
    ctx: RunContext | None = st.session_state.get("lego_ctx")
    if computed is None or ctx is None:
        st.warning("ยังล็อกอยู่ — ต้อง Connect & Load ที่ Tab 0 ก่อน")
        return
    revealed = int(st.session_state.get("lego_revealed", 0))
    if revealed < 17:
        st.warning(f"เดิน Manual มาแล้ว {revealed}/17 steps — กด Run ให้ครบ 1→17 หรือใช้ All-in ก่อน")

    st.markdown("#### Final row ที่จะบันทึก (แถวเดียว)")
    st.dataframe(final_row_frame(computed), use_container_width=True)

    commit_result = st.session_state.get("lego_commit_result")
    disabled = revealed < 17 and commit_result is None
    st.button(
        "✅ Append final row (Step 18)",
        type="primary",
        disabled=disabled,
        on_click=commit_final_row_action,
        args=(config,),
        key="lego_commit_button",
    )

    commit_result = st.session_state.get("lego_commit_result")
    if commit_result is not None:
        if commit_result.idempotent:
            st.info(
                f"run_id นี้ถูกบันทึกไว้แล้ว (idempotent) — ไม่สร้างเอกสารซ้ำ · version "
                f"{commit_result.version}"
            )
        else:
            st.success(
                f"บันทึก final row สำเร็จ · version {commit_result.version} · row_id "
                f"{commit_result.row_id[:12]}…"
            )
        document = build_final_document(computed, ctx)
        final_df = final_row_frame(computed)
        st.download_button(
            "Download final row CSV",
            data=final_df.to_csv(index=False).encode("utf-8-sig"),
            file_name="webull_lego_final_row.csv",
            mime="text/csv",
        )
        st.download_button(
            "Download final row JSON",
            data=json.dumps(
                redact_payload(document), ensure_ascii=False, indent=2, default=str
            ).encode("utf-8"),
            file_name="webull_lego_final_row.json",
            mime="application/json",
        )
        with st.expander("Provenance + recurrence metadata (redacted)"):
            st.json(redact_payload(document["provenance"]))
            st.json(redact_payload(document["metadata"]))

    render_order_panel(config)


# --------------------------------------------------------------------------- #
# All-in sidebar — Step 0→18 in one click (same engine + persistence)
# --------------------------------------------------------------------------- #
def render_all_in_sidebar(config: LegoDashboardConfig) -> None:
    with st.sidebar:
        st.header("🧱 All-in 0→18")
        st.caption(
            "อ่าน snapshot ใหม่ + คำนวณ 1 แถว + append transaction ในคลิกเดียว ให้ผลเหมือน Manual 0→18 "
            "ทุกประการ จากนั้น order panel จะพร้อมที่ Tab 18"
        )
        settings: ConnectionSettings | None = st.session_state.get("lego_settings")
        symbol_default = ""
        ctx = st.session_state.get("lego_ctx")
        if ctx is not None:
            symbol_default = ctx.snapshot.symbol
        symbol = st.text_input("Symbol", value=symbol_default, key="lego_all_in_symbol").strip().upper()
        dna_code = st.text_input(
            "DNA_CODE", value=st.session_state.get("lego_dna_code_input", config.dna_code), key="lego_all_in_dna"
        ).strip()

        if settings is None:
            st.warning("กรอก credential และกด Connect & Load ที่ Tab 0 ก่อน (All-in ใช้ credential เดียวกัน)")

        if st.button(
            "Run ALL 0 → 18", type="primary", use_container_width=True, disabled=settings is None, key="lego_all_in_button"
        ):
            try:
                with st.spinner("Step 0→18 (read → compute → append)..."):
                    new_ctx, store, computed, summary = connect_and_prepare_run(
                        settings, config, symbol=symbol or symbol_default, dna_code=dna_code
                    )
                    result = finalize_row(store, new_ctx, computed)
                st.session_state.lego_store = store
                st.session_state.lego_ctx = new_ctx
                st.session_state.lego_computed = computed
                st.session_state.lego_config = config
                st.session_state.lego_revealed = 17
                st.session_state.lego_commit_result = result
                st.session_state.lego_auth_summary = summary
                st.session_state.setdefault("lego_order_audit_events", [])
                if result.idempotent:
                    st.info(f"idempotent — run_id เดิม ไม่สร้างซ้ำ (version {result.version})")
                else:
                    st.success(f"ครบ 0→18 · version {result.version}")
                st.rerun()
            except StaleAnchorError as exc:
                st.error(f"Anchor ล้าสมัย — {exc} · กด Connect & Load ที่ Tab 0 ใหม่")
            except Exception as exc:
                st.error(f"{exc.__class__.__name__}: {exc}")

        _render_single_file_expander("sidebar")


# --------------------------------------------------------------------------- #
# App body
# --------------------------------------------------------------------------- #
if "lego_session_run_id" not in st.session_state:
    st.session_state.lego_session_run_id = uuid.uuid4().hex

try:
    dashboard_config = load_dashboard_config()
except Exception as config_error:
    st.error(f"Configuration error: {config_error}")
    dashboard_config = LegoDashboardConfig(firebase_info={})

st.title("🧱 Webull LEGO Chain — one new row")
st.caption(
    "หนึ่ง run = อ่าน snapshot ปัจจุบัน 1 ชุด + anchor ล่าสุด 1 แถว → คำนวณ row ใหม่ 1 แถว → "
    "append แบบ transaction ที่ Step 18 · การคำนวณเป็น read-only การส่ง order (UAT) ต้องกด Submit เอง"
)
st.warning(
    "Deploy เป็น Private single-user · Production เป็น read-only เสมอ · UAT ส่ง order ได้เฉพาะ final row "
    "ที่บันทึกแล้วและเป็น READY_BUY/READY_SELL"
)

tab_labels = [
    "0 · Snapshot + anchor",
    *[f"{spec.number} · {spec.column_name}" for spec in STAGE_SPECS],
    "18 · Final row + append",
]
tabs = st.tabs(tab_labels)

with tabs[0]:
    render_auth_tab(dashboard_config)

for spec, tab in zip(STAGE_SPECS, tabs[1:18]):
    with tab:
        render_stage_tab(spec, dashboard_config)

with tabs[18]:
    render_final_tab(dashboard_config)

render_all_in_sidebar(dashboard_config)
