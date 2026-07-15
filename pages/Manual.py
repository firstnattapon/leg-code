"""Manual Test Lab for Webull, DNA, Logical FIX_C, and benchmarks."""

from __future__ import annotations

import json
import random
import time
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

from manual_tools import (
    ConnectionSettings,
    WEBULL_ENDPOINTS,
    WebullManualClient,
    build_market_order_payload,
    calculate_shannon_decision,
    dna_summary,
    encode_dna,
    generate_client_order_id,
    rebalancing_reference_curve,
    run_benchmark,
    simulate_rebalancing_cashflow,
)
from rebalancing_charts import cashflow_comparison_chart, reference_shift_chart


WEB_APPS_DIR = Path(__file__).resolve().parents[1] / "web_apps"


st.set_page_config(page_title="Manual Test Lab", page_icon="🧪", layout="wide")

st.title("🧪 Manual Test Lab")
st.caption(
    "ทดสอบ Webull API, market order, DNA, Logical FIX_C และ benchmark "
    "จากหน้าเดียว"
)
st.warning(
    "Credentials จะใช้เฉพาะในหน่วยความจำของ Streamlit session นี้ "
    "ไม่ถูกบันทึกลงไฟล์หรือแสดงในผลลัพธ์ กรุณา rotate credential "
    "ที่เคยส่งผ่านแชตหรือช่องทางสาธารณะ"
)


def current_settings() -> ConnectionSettings:
    return ConnectionSettings(
        environment=st.session_state.manual_environment,
        account_id=st.session_state.manual_account_id,
        app_key=st.session_state.manual_app_key,
        app_secret=st.session_state.manual_app_secret,
        region="th",
    )


def render_error(exc: Exception) -> None:
    st.error(f"{exc.__class__.__name__}: {exc}")


def render_web_app(filename: str, *, height: int = 900) -> None:
    """Render a bundled, self-contained HTML app in an isolated iframe."""
    app_path = WEB_APPS_DIR / filename
    try:
        app_html = app_path.read_text(encoding="utf-8")
    except OSError as exc:
        render_error(exc)
        return
    components.html(app_html, height=height, scrolling=True)


def require_credentials() -> ConnectionSettings | None:
    try:
        settings = current_settings()
        settings.validate()
        return settings
    except Exception as exc:
        render_error(exc)
        return None


def clear_credentials() -> None:
    for key in (
        "manual_account_id",
        "manual_app_key",
        "manual_app_secret",
        "manual_confirmation",
        "manual_cancel_confirmation",
    ):
        st.session_state[key] = ""


with st.sidebar:
    st.header("Webull connection")
    st.selectbox(
        "Environment",
        options=list(WEBULL_ENDPOINTS),
        index=0,
        key="manual_environment",
        help="ค่าเริ่มต้นเป็น Test (UAT)",
    )
    endpoint = WEBULL_ENDPOINTS[st.session_state.manual_environment]
    st.code(endpoint, language=None)
    if st.session_state.manual_environment == "Production":
        st.error("PRODUCTION — คำสั่งซื้ออาจใช้เงินจริง")
    else:
        st.success("TEST / UAT")

    st.text_input(
        "Account ID",
        value="",
        key="manual_account_id",
        autocomplete="off",
    )
    st.text_input(
        "App Key",
        value="",
        type="password",
        key="manual_app_key",
        autocomplete="off",
    )
    st.text_input(
        "App Secret",
        value="",
        type="password",
        key="manual_app_secret",
        autocomplete="off",
    )
    st.caption("ไม่มี credentials ใดถูกฝังเป็นค่า default")
    st.button(
        "Clear credentials",
        use_container_width=True,
        on_click=clear_credentials,
    )


(
    connection_tab,
    order_tab,
    account_tab,
    dna_tab,
    fix_c_tab,
    rebalancing_tab,
    web_apps_tab,
    cheat_sheet_tab,
    benchmark_tab,
) = st.tabs(
    [
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
)


with connection_tab:
    st.subheader("Authenticated connection + position + quote")
    symbol = st.text_input("Symbol", value="AAPL", key="manual_quote_symbol").upper()
    st.caption(
        "เรียก account position และ US stock snapshot จริงจาก endpoint ที่เลือก"
    )
    if st.button("Run connection test", type="primary"):
        settings = require_credentials()
        if settings is not None:
            try:
                started = time.perf_counter()
                result = WebullManualClient(settings).get_position_and_price(symbol)
                elapsed = time.perf_counter() - started
                cols = st.columns(4)
                cols[0].metric("Environment", result["environment"])
                cols[1].metric("Symbol", result["symbol"])
                cols[2].metric("Quantity", result["quantity"])
                cols[3].metric("Last price", result["last_price"])
                st.success(f"Connection passed in {elapsed:.3f}s")
                with st.expander("Raw API responses"):
                    st.json({
                        "position_response": result["position_response"],
                        "quote_response": result["quote_response"],
                    })
            except Exception as exc:
                render_error(exc)


with order_tab:
    st.subheader("Market order preview / real submission")
    left, right = st.columns(2)
    with left:
        order_symbol = st.text_input(
            "Order symbol", value="AAPL", key="manual_order_symbol"
        ).upper()
        order_side = st.selectbox("Side", ["BUY", "SELL"])
        order_quantity = st.number_input(
            "Quantity",
            min_value=0.00001,
            value=1.0,
            step=1.0,
            format="%.5f",
        )
    with right:
        trading_session = st.selectbox(
            "Trading session", ["CORE", "PRE", "AFTER", "OVERNIGHT"]
        )
        if "manual_order_nonce" not in st.session_state:
            st.session_state.manual_order_nonce = time.time_ns()
        if st.button("Generate new Client Order ID"):
            st.session_state.manual_order_nonce = time.time_ns()
        generated_order_id = generate_client_order_id(
            "MANUAL",
            order_symbol,
            order_side,
            order_quantity,
            st.session_state.manual_order_nonce,
        )
        client_order_id = st.text_input(
            "Client Order ID", value=generated_order_id, key="manual_client_order_id"
        )

    try:
        order_payload = build_market_order_payload(
            order_symbol,
            order_side,
            float(order_quantity),
            client_order_id,
            trading_session,
        )
        st.markdown("**Payload ที่จะส่ง**")
        st.json(order_payload)
    except Exception as exc:
        order_payload = None
        render_error(exc)

    action_left, action_right = st.columns(2)
    with action_left:
        st.markdown("#### Preview")
        st.caption("เรียก Webull preview endpoint และไม่ส่งคำสั่งซื้อ")
        if st.button(
            "Preview order",
            disabled=order_payload is None,
            use_container_width=True,
        ):
            settings = require_credentials()
            if settings is not None and order_payload is not None:
                try:
                    result = WebullManualClient(settings).preview_market_order(
                        order_payload
                    )
                    st.success("Preview completed")
                    st.json(result)
                except Exception as exc:
                    render_error(exc)

    with action_right:
        st.markdown("#### Submit real order")
        environment_word = (
            "PRODUCTION"
            if st.session_state.manual_environment == "Production"
            else "UAT"
        )
        confirmation_phrase = (
            f"PLACE {environment_word} {order_side} {order_symbol} "
            f"{float(order_quantity):g}"
        )
        st.error("ปุ่มนี้เรียก place_order จริง")
        armed = st.checkbox(
            "I understand this submits a real order",
            key="manual_order_armed",
        )
        confirmation = st.text_input(
            f"Type: {confirmation_phrase}",
            value="",
            key="manual_confirmation",
            autocomplete="off",
        )
        can_submit = (
            armed
            and confirmation == confirmation_phrase
            and order_payload is not None
        )
        if st.button(
            f"Submit to {environment_word}",
            type="primary",
            disabled=not can_submit,
            use_container_width=True,
        ):
            settings = require_credentials()
            if settings is not None and order_payload is not None:
                try:
                    result = WebullManualClient(settings).place_market_order(
                        order_payload
                    )
                    st.success("Webull accepted the order request")
                    st.json(result)
                except Exception as exc:
                    render_error(exc)


with account_tab:
    st.subheader("Account and order management")
    st.caption(
        "ตรวจ balance/positions/open orders/history/detail และยกเลิก order "
        "ผ่าน Webull SDK v2"
    )
    page_size = st.number_input(
        "Result page size", min_value=1, max_value=100, value=20, step=1
    )

    account_cols = st.columns(4)
    if account_cols[0].button("Account list", use_container_width=True):
        settings = require_credentials()
        if settings is not None:
            try:
                st.json(WebullManualClient(settings).get_account_list())
            except Exception as exc:
                render_error(exc)
    if account_cols[1].button("Balance", use_container_width=True):
        settings = require_credentials()
        if settings is not None:
            try:
                st.json(WebullManualClient(settings).get_account_balance())
            except Exception as exc:
                render_error(exc)
    if account_cols[2].button("Positions", use_container_width=True):
        settings = require_credentials()
        if settings is not None:
            try:
                st.json(WebullManualClient(settings).get_positions())
            except Exception as exc:
                render_error(exc)
    if account_cols[3].button("Open orders", use_container_width=True):
        settings = require_credentials()
        if settings is not None:
            try:
                st.json(
                    WebullManualClient(settings).get_open_orders(int(page_size))
                )
            except Exception as exc:
                render_error(exc)

    st.divider()
    st.markdown("#### Order history")
    history_cols = st.columns(2)
    start_date = history_cols[0].date_input("Start date")
    end_date = history_cols[1].date_input("End date")
    if st.button("Load order history"):
        settings = require_credentials()
        if settings is not None:
            try:
                st.json(
                    WebullManualClient(settings).get_order_history(
                        page_size=int(page_size),
                        start_date=start_date.isoformat(),
                        end_date=end_date.isoformat(),
                    )
                )
            except Exception as exc:
                render_error(exc)

    st.divider()
    st.markdown("#### Order detail / cancel")
    lookup_order_id = st.text_input(
        "Existing Client Order ID", key="manual_lookup_order_id"
    )
    detail_col, cancel_col = st.columns(2)
    with detail_col:
        if st.button("Get order detail", use_container_width=True):
            settings = require_credentials()
            if settings is not None:
                try:
                    st.json(
                        WebullManualClient(settings).get_order_detail(
                            lookup_order_id
                        )
                    )
                except Exception as exc:
                    render_error(exc)
    with cancel_col:
        cancel_environment = (
            "PRODUCTION"
            if st.session_state.manual_environment == "Production"
            else "UAT"
        )
        cancel_phrase = f"CANCEL {cancel_environment} {lookup_order_id.strip()}"
        cancel_armed = st.checkbox(
            "I understand this cancels an existing order",
            key="manual_cancel_armed",
        )
        cancel_confirmation = st.text_input(
            f"Type: {cancel_phrase}",
            key="manual_cancel_confirmation",
            autocomplete="off",
        )
        can_cancel = (
            bool(lookup_order_id.strip())
            and cancel_armed
            and cancel_confirmation == cancel_phrase
        )
        if st.button(
            f"Cancel on {cancel_environment}",
            disabled=not can_cancel,
            use_container_width=True,
        ):
            settings = require_credentials()
            if settings is not None:
                try:
                    result = WebullManualClient(settings).cancel_order(
                        lookup_order_id
                    )
                    st.success("Cancel request accepted")
                    st.json(result)
                except Exception as exc:
                    render_error(exc)


with dna_tab:
    st.subheader("DNA encode / decode output")
    encode_col, decode_col = st.columns(2)

    with encode_col:
        st.markdown("#### Encode")
        dna_length = st.number_input(
            "DNA length", min_value=1, value=60, step=1
        )
        mutation_rate = st.number_input(
            "Mutation rate (%)", min_value=0, max_value=100, value=10, step=1
        )
        seeds_text = st.text_input(
            "Seeds (comma separated)", value="425,90,219,548,205,493"
        )
        if st.button("Encode DNA", use_container_width=True):
            try:
                seeds = [
                    int(part.strip())
                    for part in seeds_text.split(",")
                    if part.strip()
                ]
                encoded = encode_dna(
                    int(dna_length), int(mutation_rate), seeds
                )
                st.session_state.manual_dna_code = encoded
                st.success(encoded)
            except Exception as exc:
                render_error(exc)

    with decode_col:
        st.markdown("#### Decode")
        dna_code = st.text_area(
            "DNA code",
            value="26021034252903219354832053493",
            key="manual_dna_code",
            help="รองรับ compact code, bypass:N และ [1,N]",
        )
        if st.button("Decode DNA", type="primary", use_container_width=True):
            try:
                result = dna_summary(dna_code.strip())
                cols = st.columns(4)
                cols[0].metric("Length", result["length"])
                cols[1].metric("Ones", result["ones"])
                cols[2].metric("Zeros", result["zeros"])
                cols[3].metric("Ones ratio", f"{result['ones_ratio']:.2%}")
                st.code(result["sha256"], language=None)
                st.json(result["output"])
                st.download_button(
                    "Download output JSON",
                    data=json.dumps(result, indent=2),
                    file_name="dna_output.json",
                    mime="application/json",
                )
            except Exception as exc:
                render_error(exc)


with fix_c_tab:
    st.subheader("Logical FIX_C")
    st.caption(
        "สูตรเดียวกับ bot: value_now = quantity × price, "
        "rebalance = |FIX_C − value_now|"
    )
    cols = st.columns(3)
    quantity = cols[0].number_input(
        "Current quantity", min_value=0.0, value=10.0, format="%.5f"
    )
    last_price = cols[1].number_input(
        "Last price", min_value=0.00001, value=100.0, format="%.5f"
    )
    fix_c = cols[2].number_input(
        "FIX_C", min_value=0.00001, value=1500.0, format="%.2f"
    )
    cols = st.columns(3)
    p0 = cols[0].number_input(
        "P0", min_value=0.00001, value=9.0, format="%.5f"
    )
    diff = cols[1].number_input(
        "DIFF", min_value=0.0, value=30.0, format="%.2f"
    )
    precision = cols[2].number_input(
        "Order decimal precision", min_value=0, max_value=10, value=5, step=1
    )
    if st.button("Calculate Logical FIX_C", type="primary"):
        try:
            decision = calculate_shannon_decision(
                float(quantity),
                float(last_price),
                float(fix_c),
                float(p0),
                float(diff),
                int(precision),
            )
            data = decision.to_dict()
            metrics = st.columns(5)
            metrics[0].metric("Action", data["action"])
            metrics[1].metric("Order qty", data["order_quantity"])
            metrics[2].metric("Value now", f"${data['value_now_usd']:,.2f}")
            metrics[3].metric("Rebalance", f"${data['rebalance_amount']:,.2f}")
            metrics[4].metric("Baseline PnL", f"${data['baseline_pnl']:,.2f}")
            st.json(data)
        except Exception as exc:
            render_error(exc)


with rebalancing_tab:
    st.subheader("Rebalancing Learning Guide 101 — ส่วนเกินทุนจาก Rebalancing")
    st.caption(
        "ตาม rebalancing_learning_guide_101_corrected: สุ่มราคาหลายรอบ "
        "เปรียบเทียบกระแสเงินสดจาก Rebalancing จริง (Aₙ) กับเส้นอ้างอิง ln (Rₙ) "
        "แล้วนำส่วนเกินสะสม (Eₙ) ไปวางบนเส้นอ้างอิงตามระดับราคา"
    )

    principle_cols = st.columns(2)
    with principle_cols[0]:
        st.markdown("#### 1) เส้นอ้างอิงทางทฤษฎี")
        st.code("Rₙ = Fix_c × ln(Pₙ / P₀)", language=None)
        st.caption(
            "กระแสเงินสดอ้างอิงของการรักษามูลค่าสินทรัพย์คงที่แบบต่อเนื่อง: "
            "ค่าบวกคือรับเงินจากการขาย และค่าลบคือใช้เงินซื้อ"
        )
    with principle_cols[1]:
        st.markdown("#### 2) เส้น Rebalancing จริง")
        st.code(
            "Aₙ = Fix_c × Σ [Pᵢ / Pᵢ₋₁ − 1]\nEₙ = Aₙ − Rₙ",
            language=None,
        )
        st.caption(
            "Aₙ สะสมผลจากทุกช่วงราคาที่เกิดขึ้นจริง "
            "ส่วน Eₙ คือเงินเกินทุนสะสมเหนือเส้นอ้างอิง"
        )

    st.markdown("#### Testing Lab — สุ่มราคา 100 รอบ")
    if "manual_guide_seed" not in st.session_state:
        st.session_state.manual_guide_seed = 101

    def randomize_guide_seed() -> None:
        st.session_state.manual_guide_seed = random.randint(0, 999_999_999)

    lab_cols = st.columns(3)
    guide_fix_c = lab_cols[0].number_input(
        "Fix_c", min_value=0.01, value=1500.0, step=100.0, format="%.2f"
    )
    guide_p0 = lab_cols[1].number_input(
        "ราคาเริ่มต้น t₀ (P₀)", min_value=0.01, value=100.0, format="%.5f"
    )
    guide_vol = lab_cols[2].number_input(
        "ความผันผวน/รอบ (%)",
        min_value=0.0, max_value=40.0, value=4.0, step=0.1,
    )
    lab_cols = st.columns(3)
    guide_drift = lab_cols[0].number_input(
        "แนวโน้ม/รอบ (%)", min_value=-10.0, max_value=10.0, value=0.0, step=0.01
    )
    guide_steps = lab_cols[1].number_input(
        "จำนวนรอบ", min_value=2, max_value=500, value=100, step=1
    )
    guide_seed = lab_cols[2].number_input(
        "Seed", min_value=0, step=1, key="manual_guide_seed"
    )
    st.button("สุ่ม Seed ใหม่", on_click=randomize_guide_seed)

    try:
        sim_rows = simulate_rebalancing_cashflow(
            float(guide_fix_c),
            float(guide_p0),
            float(guide_vol) / 100.0,
            float(guide_drift) / 100.0,
            int(guide_steps),
            int(guide_seed),
        )
        final_row = sim_rows[-1]
        stat_cols = st.columns(4)
        stat_cols[0].metric("ราคาสุดท้าย Pₙ", f"{final_row['price']:,.2f}")
        stat_cols[1].metric(
            "Rebalancing Aₙ", f"{final_row['actual_cumulative']:+,.2f}"
        )
        stat_cols[2].metric("อ้างอิง Rₙ", f"{final_row['ln_reference']:+,.2f}")
        stat_cols[3].metric("ส่วนเกินสะสม Eₙ", f"{final_row['excess']:+,.2f}")

        st.markdown("#### กราฟที่ 1 — เปรียบเทียบตามลำดับ Rebalance")
        st.altair_chart(
            cashflow_comparison_chart(sim_rows), use_container_width=True
        )

        st.markdown("#### กราฟที่ 2 — เงินทุนเทียบกับระดับราคา")
        st.code(
            "แกน X: ราคา x ตั้งแต่ 0 ถึง 2t₀\n"
            "Y₁(x) = Fix_c × ln(x / t₀)   ← เส้นอ้างอิง\n"
            "Y₂(x) = Y₁(x) + Eₙ           ← เส้นอ้างอิง + เงินเกินทุนสะสม",
            language=None,
        )
        curve_rows = rebalancing_reference_curve(
            float(guide_fix_c), float(guide_p0), float(final_row["excess"]),
            points=300,
        )
        st.altair_chart(
            reference_shift_chart(curve_rows, float(guide_p0)),
            use_container_width=True,
        )
        st.warning(
            "จุดสำคัญ: แกนราคาเริ่มแสดงที่ 0 ตามโจทย์ แต่ไม่ลากเส้นที่ x = 0 "
            "เพราะ ln(0) ไม่มีค่าจำกัด (มุ่งสู่ −∞) การคำนวณเส้นจึงเริ่มที่ค่าบวก"
            "เล็ก ๆ ใกล้ศูนย์ ส่วน Y₂ เป็นเส้น Y₁ ที่เลื่อนขึ้นในแนวตั้งเท่ากับ Eₙ "
            "จึงมีช่องว่างคงที่เท่ากับส่วนเกินสะสม"
        )
        st.success(
            "การอ่านเครื่องหมาย: ตามสมการ ln(x/t₀) ค่าบวกหมายถึงเงินสดที่ได้รับ"
            "จากการขาย และค่าลบหมายถึงเงินสดที่ใช้ซื้อ หากต้องการให้ "
            "“เงินทุนที่ใช้ซื้อ” เป็นบวก ต้องกลับเครื่องหมายเป็น Fix_c × ln(t₀/x) "
            "ซึ่งเป็นคนละ convention"
        )

        st.markdown("#### ข้อมูลการทดสอบ")
        sim_frame = pd.DataFrame(sim_rows)
        st.dataframe(
            sim_frame.rename(columns={
                "step": "รอบ",
                "price": "ราคา",
                "delta_actual": "ΔA รอบนี้",
                "actual_cumulative": "Aₙ",
                "ln_reference": "Rₙ",
                "excess": "Eₙ",
            }),
            use_container_width=True,
            hide_index=True,
        )
        st.download_button(
            "ดาวน์โหลด CSV",
            data=sim_frame.to_csv(index=False),
            file_name="rebalancing_test.csv",
            mime="text/csv",
        )
        st.caption(
            "แบบจำลองเพื่อการเรียนรู้ · ยังไม่รวมค่าธรรมเนียม spread, slippage "
            "และภาษี"
        )
    except Exception as exc:
        render_error(exc)


with web_apps_tab:
    st.subheader("Interactive Rebalancing Web Apps")
    st.caption(
        "เปิดคู่มือและ playground แบบโต้ตอบได้จาก Manual Test Lab "
        "โดยทำงานอยู่ใน iframe แยกจาก Webull credentials"
    )
    selected_web_app = st.radio(
        "เลือก Web App",
        options=("Rebalancing 101", "Rebalancing Playground"),
        horizontal=True,
        label_visibility="collapsed",
        key="manual_web_app_choice",
    )
    if selected_web_app == "Rebalancing 101":
        render_web_app("rebalancing101.html")
    else:
        render_web_app("rebalancing_playground.html")


with cheat_sheet_tab:
    st.subheader("⚡ Cheat Sheet — รู้ทันสมการในหน้าเดียว")
    st.caption(
        "ทดลอง Logical FIX_C และ Aₙ/Rₙ/Eₙ แบบ educational what-if "
        "ใน iframe ที่ไม่เรียก Webull API และไม่เข้าถึง credentials"
    )
    render_web_app("cheat_sheet.html", height=1200)


with benchmark_tab:
    st.subheader("Local CPU benchmark")
    st.caption("ไม่เรียก Webull API และไม่ส่ง order")
    iterations = st.number_input(
        "Iterations", min_value=1, max_value=100_000, value=1_000, step=1_000
    )
    benchmark_dna_code = st.text_input(
        "DNA code for benchmark",
        value="26021034252903219354832053493",
    )
    if st.button("Run benchmark", type="primary"):
        try:
            with st.spinner("Benchmarking..."):
                result = run_benchmark(
                    benchmark_dna_code.strip(),
                    quantity=10.0,
                    last_price=100.0,
                    fix_c=1500.0,
                    p0=9.0,
                    diff=30.0,
                    iterations=int(iterations),
                )
            metrics = st.columns(4)
            metrics[0].metric(
                "FIX_C mean",
                f"{result['logical_fix_c']['mean_microseconds']:.2f} µs",
            )
            metrics[1].metric(
                "FIX_C ops/s",
                f"{result['logical_fix_c']['operations_per_second']:,.0f}",
            )
            metrics[2].metric(
                "DNA mean",
                f"{result['decode_dna']['mean_microseconds']:.2f} µs",
            )
            metrics[3].metric(
                "DNA ops/s",
                f"{result['decode_dna']['operations_per_second']:,.0f}",
            )
            st.json(result)
        except Exception as exc:
            render_error(exc)
