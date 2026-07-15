"""Altair chart builders for the corrected Rebalancing Learning Guide 101.

Shared by the Manual Test Lab tab and the Shannon Demon Dashboard so both
pages draw the same two guide charts:

1. Cash-flow comparison by rebalance order: Aₙ (actual), Rₙ (ln reference),
   and Eₙ = Aₙ − Rₙ (cumulative excess).
2. Capital versus price level: Y₁(x) = Fix_c × ln(x/t₀) and
   Y₂(x) = Y₁(x) + Eₙ.
"""

from __future__ import annotations

import altair as alt
import pandas as pd

CASHFLOW_SERIES = {
    "actual_cumulative": ("Aₙ: Rebalancing จริง", "#3b82f6"),
    "ln_reference": ("Rₙ: อ้างอิง ln", "#d97706"),
    "excess": ("Eₙ: ส่วนเกินสะสม", "#10b981"),
}
REFERENCE_SERIES = {
    "y_reference": ("Y₁: เส้นอ้างอิง ln", "#d97706"),
    "y_rebalanced": ("Y₂: อ้างอิง + Eₙ", "#10b981"),
}
ZERO_LINE_COLOR = "#94a3b8"
MARKER_COLOR = "#64748b"


def _long_frame(
    frame: pd.DataFrame,
    x_column: str,
    series: dict[str, tuple[str, str]],
    value_name: str,
) -> tuple[pd.DataFrame, alt.Scale]:
    melted = frame.melt(
        id_vars=[x_column],
        value_vars=list(series),
        var_name="series",
        value_name=value_name,
    )
    melted["series"] = melted["series"].map({
        key: label for key, (label, _color) in series.items()
    })
    scale = alt.Scale(
        domain=[label for label, _color in series.values()],
        range=[color for _label, color in series.values()],
    )
    return melted, scale


def _zero_rule() -> alt.Chart:
    return (
        alt.Chart(pd.DataFrame({"zero": [0.0]}))
        .mark_rule(strokeDash=[5, 5], color=ZERO_LINE_COLOR)
        .encode(y="zero:Q")
    )


def cashflow_comparison_chart(
    rows: list[dict[str, float]],
    x_title: str = "ลำดับ Rebalance",
    height: int = 380,
) -> alt.LayerChart:
    """Guide chart 1: Aₙ, Rₙ, and Eₙ against the rebalance order."""
    frame = pd.DataFrame(rows)
    melted, scale = _long_frame(frame, "step", CASHFLOW_SERIES, "cashflow")
    lines = (
        alt.Chart(melted)
        .mark_line(strokeWidth=2.4)
        .encode(
            x=alt.X("step:Q", title=x_title),
            y=alt.Y("cashflow:Q", title="กระแสเงินสดสะสม"),
            color=alt.Color(
                "series:N",
                scale=scale,
                legend=alt.Legend(title=None, orient="top"),
            ),
        )
    )
    return (_zero_rule() + lines).properties(height=height)


def reference_shift_chart(
    curve_rows: list[dict[str, float]],
    p0: float,
    clip_quantile: float = 0.025,
    height: int = 380,
) -> alt.LayerChart:
    """Guide chart 2: Y₁ and the vertically shifted Y₂ against price.

    The x axis starts at 0 per the guide, while the lines themselves start
    at a small positive price because ln(0) diverges. The bottom quantile of
    y values is clipped so the dive toward −∞ near zero does not squash the
    readable part of the chart.
    """
    frame = pd.DataFrame(curve_rows)
    melted, scale = _long_frame(frame, "price", REFERENCE_SERIES, "capital")

    y_values = melted["capital"]
    y_low = float(y_values.quantile(clip_quantile))
    y_high = float(y_values.max())
    padding = max((y_high - y_low) * 0.08, 1.0)
    y_scale = alt.Scale(domain=[y_low - padding, y_high + padding], nice=False)

    lines = (
        alt.Chart(melted)
        .mark_line(strokeWidth=2.4, clip=True)
        .encode(
            x=alt.X(
                "price:Q",
                title="ระดับราคา x (0 ถึง 2t₀)",
                scale=alt.Scale(domain=[0.0, 2.0 * float(p0)], nice=False),
            ),
            y=alt.Y("capital:Q", title="กระแสเงินสด / เงินทุน", scale=y_scale),
            color=alt.Color(
                "series:N",
                scale=scale,
                legend=alt.Legend(title=None, orient="top"),
            ),
        )
    )
    anchor = pd.DataFrame({"price": [float(p0)], "label": ["t₀"]})
    anchor_rule = (
        alt.Chart(anchor)
        .mark_rule(strokeDash=[4, 4], color=MARKER_COLOR)
        .encode(x="price:Q")
    )
    anchor_text = (
        alt.Chart(anchor)
        .mark_text(baseline="top", dy=4, color=MARKER_COLOR)
        .encode(x="price:Q", y=alt.value(0), text="label:N")
    )
    return (_zero_rule() + lines + anchor_rule + anchor_text).properties(
        height=height
    )
