"""Read-only Shannon Demon Firestore dashboard."""

from __future__ import annotations

import pandas as pd
import streamlit as st
from google.cloud import firestore
from google.oauth2 import service_account

from manual_tools import (
    rebalancing_cashflow_from_prices,
    rebalancing_reference_curve,
)
from rebalancing_charts import cashflow_comparison_chart, reference_shift_chart
from trade_log import (
    TRADE_PRICE_COLUMNS,
    build_trade_log_display,
    find_trade_price_column,
    realized_cashflow_from_trades,
    trade_price_series,
)

st.set_page_config(page_title="Shannon Demon Dashboard", layout="wide")


@st.cache_resource
def get_client(project_id: str) -> firestore.Client:
    info = dict(st.secrets["firebase_service_account"])
    creds = service_account.Credentials.from_service_account_info(info)
    return firestore.Client(credentials=creds, project=project_id)


def load_state(db: firestore.Client, collection: str, document: str) -> dict:
    snapshot = db.collection(collection).document(document).get()
    return snapshot.to_dict() or {}


def load_trades(db: firestore.Client, collection: str, limit: int) -> pd.DataFrame:
    docs = (
        db.collection(collection)
        .order_by("created_at", direction=firestore.Query.DESCENDING)
        .limit(limit)
        .stream()
    )
    rows = [doc.to_dict() for doc in docs]
    return pd.json_normalize(rows, sep="_") if rows else pd.DataFrame()


def positive_number(value) -> bool:
    """Accept legacy numeric strings without letting malformed state crash UI."""
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def render_reference_chart(fix_c: float, p0: float, excess: float) -> None:
    st.markdown("#### กราฟที่ 2 — เงินทุนเทียบกับระดับราคา")
    st.code(
        "แกน X: ราคา x ตั้งแต่ 0 ถึง 2t₀\n"
        "Y₁(x) = Fix_c × ln(x / t₀)   ← เส้นอ้างอิง\n"
        "Y₂(x) = Y₁(x) + Eₙ           ← เส้นอ้างอิง + เงินเกินทุนสะสม",
        language=None,
    )
    curve_rows = rebalancing_reference_curve(fix_c, p0, excess, points=300)
    st.altair_chart(reference_shift_chart(curve_rows, p0), use_container_width=True)
    st.caption(
        "แกนราคาเริ่มแสดงที่ 0 แต่ไม่ลากเส้นที่ x = 0 เพราะ ln(0) มุ่งสู่ −∞; "
        "Y₂ คือ Y₁ ที่เลื่อนขึ้นในแนวตั้งเท่ากับ Eₙ ช่องว่างระหว่างเส้นจึงคงที่ "
        "ค่าบวกคือเงินสดที่ได้รับจากการขาย ค่าลบคือเงินสดที่ใช้ซื้อ"
    )


with st.sidebar:
    st.header("Firestore target")
    try:
        default_project = dict(st.secrets["firebase_service_account"]).get(
            "project_id", ""
        )
    except (KeyError, FileNotFoundError):
        default_project = ""
    project_id = st.text_input("Project ID", value=default_project)
    state_collection = st.text_input(
        "State collection", value="shannon_demon_state"
    )
    state_document = st.text_input(
        "State document", value="SHANNON_DEMON_DNA_SMR"
    )
    trade_collection = st.text_input(
        "Trade collection", value="shannon_demon_trades"
    )
    trade_limit = st.number_input(
        "Trades to show", min_value=10, max_value=1000, value=100, step=10
    )
    st.divider()
    st.header("Rebalancing guide")
    guide_fix_c = st.number_input(
        "Fix_c", min_value=0.01, value=1500.0, step=100.0, format="%.2f"
    )
    guide_p0 = st.number_input(
        "ราคาเริ่มต้น t₀ (P₀)",
        min_value=0.01,
        value=100.0,
        format="%.5f",
        help=(
            "ใช้เฉพาะเส้นสาธิตตอนยังไม่มี trade log — เมื่อมีข้อมูลแล้ว "
            "ระบบ anchor P₀ ที่ราคาแรกในหน้าต่างโดยอัตโนมัติ "
            "เพื่อไม่ให้เกิดสเต็ปปลอมจาก P₀ ที่ไกลจากราคาจริง"
        ),
    )
    if st.button("Refresh"):
        st.cache_resource.clear()
        st.rerun()

st.title("Shannon Demon Dashboard")
st.page_link("pages/Manual.py", label="Open Manual Test Lab", icon="🧪")

st.subheader("หลักการ Rebalancing Learning Guide 101")
principle_cols = st.columns(3)
with principle_cols[0]:
    st.markdown("#### 1) เส้นอ้างอิง Rₙ — ทุน×เทรนด์")
    st.code("Rₙ = Fix_c × ln(Pₙ / P₀)", language=None)
    st.caption(
        "กระแสเงินสด baseline ที่ระดับราคาอย่างเดียวอธิบายได้ "
        "(rebalance ต่อเนื่องเชิงอุดมคติ) — ขึ้นลงตามเทรนด์ ไม่ใช่ฝีมือ harvest"
    )
with principle_cols[1]:
    st.markdown("#### 2) เงินสดสะสมจริง Aₙ")
    st.code("Aₙ = Fix_c × Σ [Pᵢ / Pᵢ₋₁ − 1]", language=None)
    st.caption(
        "กระแสเงินสดสุทธิจากการ rebalance ทุกสเต็ป (+ รับจากขาย, − จ่ายซื้อ) "
        "— ยังปนส่วนของเทรนด์ จึงไม่ใช่ตัววัดกำไรโดยตรง"
    )
with principle_cols[2]:
    st.markdown("#### 3) กำไร harvest จริง Eₙ")
    st.code("Eₙ = Aₙ − Rₙ  ≥ 0", language=None)
    st.caption(
        "ทุนส่วนเกินเหนือเส้นอ้างอิง = กำไรจากความผันผวนล้วน ๆ "
        "(แยกทุนและเทรนด์ออกแล้ว) ไม่ลดลงเลย และ realize เต็ม"
        "เมื่อรอบซื้อ-ขายปิด (ราคาย้อนกลับมาระดับเดิม)"
    )

st.info(
    "สามสูตรด้านบนเป็น Learning Guide ภายใต้ ideal full-rebalance ทุก price step. "
    "ส่วน dataframe เงินจริงด้านล่างใช้ signed filled notional จาก broker; "
    "เมื่อมี threshold, partial fill, slippage หรือ fee ค่า Eₙ จาก execution "
    "อาจติดลบได้และไม่ควรถูกเรียกว่า harvest ที่รับประกันว่า Eₙ ≥ 0"
)

if not project_id:
    st.info("ตั้งค่า firebase_service_account และ Project ID เพื่ออ่าน Firestore")
    render_reference_chart(float(guide_fix_c), float(guide_p0), 0.0)
    st.stop()

try:
    db = get_client(project_id)
    state = load_state(db, state_collection, state_document)
    if state:
        latest_market_state = state.get("latest_market_state")
        if not isinstance(latest_market_state, dict):
            latest_market_state = {}
        cols = st.columns(5)
        cols[0].metric("DNA step", state.get("dna_step", "-"))
        cols[1].metric("Last signal", state.get("last_signal", "-"))
        cols[2].metric("Last status", state.get("last_status", "-"))
        cols[3].metric(
            "จำนวนถือครองล่าสุด (หุ้น)",
            latest_market_state.get("quantity", "-"),
        )
        last_logged = state.get("last_logged_at")
        cols[4].metric("Last logged at", str(last_logged) if last_logged else "-")
        if latest_market_state:
            st.caption(
                "Latest holding source: "
                f"`{latest_market_state.get('source', 'unknown')}` · "
                "observed at "
                f"`{latest_market_state.get('observed_at', '-')}` · "
                "ค่านี้ไม่มาจาก expected_position_after"
            )
        pending_order = state.get("pending_order")
        if (
            isinstance(pending_order, dict)
            and positive_number(pending_order.get("filled_quantity", 0))
            and pending_order.get("position_reconciled") is not True
        ):
            st.warning(
                "มี fill ที่กำลังรอ Webull Positions ยืนยันจำนวนถือครอง "
                f"(sync={pending_order.get('position_sync_status', 'PENDING')}, "
                f"cycles={pending_order.get('position_reconcile_cycles', 0)}). "
                "ระบบต้องไม่ส่ง order ใหม่จนกว่าจะ reconcile สำเร็จ"
            )
    else:
        st.info(f"ยังไม่มี state document ที่ {state_collection}/{state_document}")

    st.subheader("Trade log")
    trades = load_trades(db, trade_collection, int(trade_limit))
    if trades.empty:
        st.info(f"ยังไม่มี trade log ใน collection {trade_collection}")
        render_reference_chart(float(guide_fix_c), float(guide_p0), 0.0)
    else:
        if "status" in trades:
            st.bar_chart(trades["status"].value_counts())

        price_column = find_trade_price_column(trades)
        prices = (
            trade_price_series(trades, price_column) if price_column else []
        )
        # Anchor P₀ at the first price inside the window so Aₙ/Rₙ/Eₙ all
        # start at 0 (ทุนถูกแยกออก) and measure only what happened in view.
        # An external P₀ far from the traded range would inject one huge
        # synthetic first step that dwarfs the real harvest.
        theoretical_anchor_p0 = prices[0] if prices else float(guide_p0)

        # Realized cash is a different data product from the Learning Guide's
        # every-quote what-if path.  Probe eligibility first, then anchor Rₙ at
        # the first broker execution in view.  A market quote is never used as
        # a substitute when the execution price is missing.
        realized_probe = realized_cashflow_from_trades(
            trades, float(guide_fix_c), float(guide_p0)
        )
        confirmed_execution_prices = realized_probe.loc[
            realized_probe["eligible"], "execution_price"
        ].dropna()
        execution_anchor_p0 = (
            float(confirmed_execution_prices.iloc[0])
            if not confirmed_execution_prices.empty
            else float(guide_p0)
        )
        realized_rows = realized_cashflow_from_trades(
            trades, float(guide_fix_c), execution_anchor_p0
        )
        confirmed_fills = int(realized_rows["eligible"].sum())
        fills_missing_execution_price = int(
            realized_rows["missing_execution_price"].sum()
        )

        if price_column:
            st.caption(
                "จัดกลุ่มคอลัมน์เพื่ออ่านง่าย — "
                "① Logged DNA (บันทึกจากบอท) · "
                "② Execution reference Rₙ · "
                "③ เงินสดจาก execution ที่ยืนยันแล้ว Aₙ, Eₙ "
                f"(คอลัมน์ราคา: `{price_column}` · "
                f"execution anchor P₀ = {execution_anchor_p0:,.5f})"
            )
            st.caption(
                "คอลัมน์ realized รับเฉพาะ terminal fill ที่ filled_quantity > 0, "
                "position_reconciled = true และมี execution price จริง; "
                "PASS/pending/rejected/unfilled ไม่เปลี่ยนยอดสะสม และไม่ใช้ last_price แทน fill price"
            )
            st.caption(
                "จำนวนถือครองใช้เฉพาะ position_after / market_state / quantity "
                "ที่มาจาก Webull Positions; คอลัมน์ “คาดหลัง fill” ใช้วินิจฉัยเท่านั้น "
                "และไม่ถูกนำไปแทนจำนวนถือครองหรือเงินจริง"
            )
            st.caption(
                "เครื่องหมาย ส่วนต่างเป้าหมาย: − ต้องขายออก · + ต้องซื้อเข้า "
                "(มุมปรับพอร์ต — ตรงข้ามกับ ΔAₙ/Aₙ ที่ + คือเงินสดรับจากการขาย)"
            )
            st.dataframe(
                build_trade_log_display(
                    trades, price_column, float(guide_fix_c), execution_anchor_p0
                ),
                use_container_width=True,
            )
        else:
            st.dataframe(trades, use_container_width=True)

        st.subheader("ยอดเงินจริงจาก execution ที่ยืนยันแล้ว")
        realized_valid = realized_rows.dropna(subset=["actual_cumulative"])
        if realized_valid.empty:
            st.info(
                "ยังไม่มี fill ที่ผ่านครบทั้งสถานะ terminal, filled_quantity, "
                "Positions reconciliation และ execution price — คอลัมน์ realized จึงเว้นว่างอย่างตั้งใจ"
            )
        else:
            realized_final = realized_valid.iloc[-1]
            realized_cols = st.columns(4)
            realized_cols[0].metric(
                "Confirmed fills", confirmed_fills
            )
            realized_cols[1].metric(
                "Aₙ เงินสดสะสม (fee-aware)",
                f"{realized_final['actual_cumulative']:+,.2f}",
            )
            realized_cols[2].metric(
                "Rₙ execution reference",
                f"{realized_final['ln_reference']:+,.2f}",
            )
            realized_cols[3].metric(
                "Eₙ = Aₙ − Rₙ",
                f"{realized_final['excess']:+,.2f}",
            )
        if fills_missing_execution_price:
            st.warning(
                f"พบ {fills_missing_execution_price} fill ที่ reconcile กับ Positions แล้ว "
                "แต่ log ไม่มี execution/average fill price จึงไม่คำนวณเงินจริงจาก last_price แทน "
                "ต้องเพิ่ม execution price ใน bot trade document ก่อน"
            )

        st.subheader("กราฟ Learning Guide 101 แบบ what-if จาก market quote")
        if not prices:
            st.info(
                "ไม่พบคอลัมน์ราคาที่ใช้งานได้ใน trade log "
                f"(มองหา: {', '.join(TRADE_PRICE_COLUMNS)} "
                "รวมถึงคอลัมน์จากข้อมูลซ้อนที่ลงท้ายด้วยชื่อเหล่านี้) "
                "จึงแสดงเฉพาะเส้นอ้างอิงทางทฤษฎี"
            )
            render_reference_chart(float(guide_fix_c), float(guide_p0), 0.0)
        else:
            rows = rebalancing_cashflow_from_prices(
                prices, float(guide_fix_c), theoretical_anchor_p0
            )
            final_row = rows[-1]
            cols = st.columns(5)
            cols[0].metric(
                "What-if harvest Eₙ", f"{final_row['excess']:+,.2f}"
            )
            cols[1].metric("ราคาสุดท้าย Pₙ", f"{final_row['price']:,.2f}")
            cols[2].metric(
                "What-if Aₙ", f"{final_row['actual_cumulative']:+,.2f}"
            )
            cols[3].metric("อ้างอิง Rₙ", f"{final_row['ln_reference']:+,.2f}")
            cols[4].metric("Confirmed fills", confirmed_fills)
            st.warning(
                "กราฟชุดนี้เป็น Learning Guide แบบ what-if rebalance ทุก market quote "
                "ไม่ใช่เงินสดจาก broker; เงินจริงให้ดู dataframe และ metrics ส่วน execution ด้านบน"
            )
            st.caption(
                f"what-if anchor P₀ = ราคาแรกในหน้าต่าง ({theoretical_anchor_p0:,.2f}) → "
                "Eₙ เริ่มจาก 0 แยกทุนออก อ่านเป็นกำไรสะสมได้ตรง ๆ; "
                "เส้น Rₙ เต็มประวัติตั้งแต่ P₀ จริงของบอทดูได้จากคอลัมน์ "
                "`baseline_pnl` ที่บอทบันทึกไว้ทุกแถว"
            )

            st.markdown(
                f"#### กราฟที่ 1 — เปรียบเทียบตามลำดับ trade (คอลัมน์ราคา: "
                f"`{price_column}`)"
            )
            st.altair_chart(
                cashflow_comparison_chart(rows, x_title="ลำดับ trade"),
                use_container_width=True,
            )
            render_reference_chart(
                float(guide_fix_c), theoretical_anchor_p0, float(final_row["excess"])
            )
except Exception as exc:
    st.error(f"Firestore error: {exc}")
