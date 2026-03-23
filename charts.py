"""
charts.py — Plotly visualization layer for FP&A Variance Copilot.

Each function accepts prepared DataFrames (output of variance_engine.py) and
returns a fully styled go.Figure ready to pass to st.plotly_chart().

Design principles
-----------------
- White background, Calibri font, slate text — matches a real finance deck.
- Gridlines are present but quiet (#E2E8F0) so they guide the eye without competing.
- Every chart is self-contained: no global state, no side effects.
- Color constants are imported from config but can be overridden via kwargs.
"""

from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from config import COLORS, SEVERITY_ORDER


# ---------------------------------------------------------------------------
# Shared theme helpers
# ---------------------------------------------------------------------------

_FONT_FAMILY = "Calibri, 'Segoe UI', Arial, sans-serif"

_LAYOUT_DEFAULTS = dict(
    font=dict(family=_FONT_FAMILY, color="#334155"),   # slate-700
    paper_bgcolor="#FFFFFF",
    plot_bgcolor="#FFFFFF",
    margin=dict(l=48, r=32, t=56, b=48),
    hoverlabel=dict(
        bgcolor="#1E293B",
        font_color="#F1F5F9",
        font_family=_FONT_FAMILY,
        font_size=12,
        bordercolor="#334155",
    ),
    legend=dict(
        bgcolor="rgba(0,0,0,0)",
        bordercolor="#E2E8F0",
        borderwidth=1,
        font=dict(size=12),
    ),
)

_GRIDLINE_STYLE = dict(
    gridcolor="#E2E8F0",
    gridwidth=1,
    zeroline=False,
)

_AXIS_STYLE = dict(
    **_GRIDLINE_STYLE,
    linecolor="#CBD5E1",   # slate-300
    linewidth=1,
    tickfont=dict(family=_FONT_FAMILY, size=11, color="#64748B"),  # slate-500
    title_font=dict(family=_FONT_FAMILY, size=12, color="#475569"),  # slate-600
)


def _apply_layout(fig: go.Figure, **overrides) -> go.Figure:
    """Merge default layout settings into *fig*, then apply any overrides."""
    layout = {**_LAYOUT_DEFAULTS, **overrides}
    fig.update_layout(**layout)
    return fig


def _fmt_currency(value: float, show_sign: bool = False) -> str:
    """Compact currency label: $1.2M, $450K, $3K."""
    sign = ("+" if value >= 0 else "") if show_sign else ""
    abs_val = abs(value)
    if abs_val >= 1_000_000:
        return f"{sign}{'-' if value < 0 else ''}${abs_val / 1_000_000:.1f}M"
    if abs_val >= 1_000:
        return f"{sign}{'-' if value < 0 else ''}${abs_val / 1_000:.0f}K"
    return f"{sign}${value:,.0f}"


# ---------------------------------------------------------------------------
# 1. Waterfall chart — the hero
# ---------------------------------------------------------------------------

def plot_waterfall(waterfall_df: pd.DataFrame, title: str = "Q4 2024 Budget-to-Actual Bridge") -> go.Figure:
    """
    Render a Plotly waterfall chart bridging Budget total → material variances → Actual total.

    Color scheme
    ------------
    - Totals bars (Budget / Actual): teal (#0D9488)
    - Favorable bridge bars (cost reductions, revenue beats): green (#16A34A)
    - Unfavorable bridge bars (cost overruns, revenue misses): red (#DC2626)

    Label strategy
    --------------
    Text labels are placed outside each bar.  When there are more than 8 bridge
    bars the x-axis tick labels are angled at -35° to prevent overlap.

    Parameters
    ----------
    waterfall_df : pd.DataFrame
        Output of :func:`variance_engine.build_waterfall_data`.
        Expected columns: label, value, measure, text.
    title : str
        Chart title displayed at the top.

    Returns
    -------
    go.Figure
    """
    labels  = waterfall_df["label"].tolist()
    values  = waterfall_df["value"].tolist()
    measures = waterfall_df["measure"].tolist()
    texts   = waterfall_df["text"].tolist()

    # Assign connector / increase / decrease colors
    # Totals bars use teal; relative bars use green/red based on sign.
    increasing_color  = "#16A34A"   # green-600
    decreasing_color  = "#DC2626"   # red-600
    totals_color      = "#0D9488"   # teal-600

    # Build per-bar marker colors (Plotly waterfall doesn't support per-bar
    # color overrides directly, but we can layer invisible scatter points for
    # annotations and rely on the built-in increasing/decreasing/totals color split).
    # The three color kwargs cover all cases.

    fig = go.Figure(go.Waterfall(
        orientation="v",
        measure=measures,
        x=labels,
        y=values,
        text=texts,
        textposition="outside",
        textfont=dict(family=_FONT_FAMILY, size=11, color="#334155"),
        connector=dict(
            line=dict(color="#CBD5E1", width=1, dash="dot")
        ),
        increasing=dict(marker=dict(color=increasing_color, line=dict(width=0))),
        decreasing=dict(marker=dict(color=decreasing_color, line=dict(width=0))),
        totals=dict(marker=dict(color=totals_color, line=dict(width=0))),
        hovertemplate="<b>%{x}</b><br>%{text}<extra></extra>",
    ))

    # Angle tick labels when there are many bars to prevent overlap
    n_bars = len(labels)
    tick_angle = -35 if n_bars > 8 else 0
    tick_anchor = "end" if n_bars > 8 else "center"

    _apply_layout(
        fig,
        title=dict(
            text=title,
            font=dict(family=_FONT_FAMILY, size=16, color="#1E293B"),
            x=0.02,
            xanchor="left",
        ),
        xaxis=dict(
            **_AXIS_STYLE,
            tickangle=tick_angle,
            ticklabelposition="outside " + tick_anchor if n_bars > 8 else "outside",
            showgrid=False,
        ),
        yaxis=dict(
            **_AXIS_STYLE,
            tickformat="$,.0f",
            title="Amount (USD)",
            showgrid=True,
        ),
        showlegend=False,
        # Add breathing room above the tallest bar so outside text isn't clipped
        yaxis_automargin=True,
        margin=dict(l=72, r=32, t=64, b=80 if n_bars > 8 else 56),
    )

    return fig


# ---------------------------------------------------------------------------
# 2. Grouped bar chart — Budget vs Actual by Category
# ---------------------------------------------------------------------------

def plot_budget_vs_actual(
    category_df: pd.DataFrame,
    title: str = "Budget vs. Actual by Category",
) -> go.Figure:
    """
    Grouped bar chart comparing Budget and Actual totals for each Category.

    Visual design
    -------------
    - Budget bars: muted gray (#94A3B8, slate-400) — the baseline, de-emphasised.
    - Actual bars: teal (#0D9488) — the result you're here to explain.
    - Data labels are rendered above each bar using a consistent compact currency format.
    - Categories are sorted by absolute variance (largest swing first) so the most
      interesting categories appear on the left.

    Parameters
    ----------
    category_df : pd.DataFrame
        Output of :func:`variance_engine.summarize_by_category`.
        Expected columns: Category, Budget ($), Actual ($), Variance ($).
    title : str
        Chart title.

    Returns
    -------
    go.Figure
    """
    # Sort by absolute variance so most-impactful categories lead
    df = category_df.copy()
    df["_abs_var"] = df["Variance ($)"].abs()
    df.sort_values("_abs_var", ascending=True, inplace=True)   # ascending=True for horizontal feel
    df.drop(columns=["_abs_var"], inplace=True)

    budget_labels = [_fmt_currency(v) for v in df["Budget ($)"]]
    actual_labels = [_fmt_currency(v) for v in df["Actual ($)"]]

    budget_bar = go.Bar(
        name="Budget",
        x=df["Category"],
        y=df["Budget ($)"],
        text=budget_labels,
        textposition="outside",
        textfont=dict(family=_FONT_FAMILY, size=10, color="#64748B"),
        marker=dict(color="#94A3B8", line=dict(width=0)),
        hovertemplate="<b>%{x}</b><br>Budget: %{text}<extra></extra>",
    )

    actual_bar = go.Bar(
        name="Actual",
        x=df["Category"],
        y=df["Actual ($)"],
        text=actual_labels,
        textposition="outside",
        textfont=dict(family=_FONT_FAMILY, size=10, color="#0F766E"),
        marker=dict(color="#0D9488", line=dict(width=0)),
        hovertemplate="<b>%{x}</b><br>Actual: %{text}<extra></extra>",
    )

    fig = go.Figure(data=[budget_bar, actual_bar])

    _apply_layout(
        fig,
        title=dict(
            text=title,
            font=dict(family=_FONT_FAMILY, size=16, color="#1E293B"),
            x=0.02,
            xanchor="left",
        ),
        barmode="group",
        bargap=0.25,
        bargroupgap=0.08,
        xaxis=dict(**_AXIS_STYLE, showgrid=False),
        yaxis=dict(**_AXIS_STYLE, tickformat="$,.0f", title="Amount (USD)"),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
            font=dict(size=12),
        ),
        yaxis_automargin=True,
        margin=dict(l=72, r=32, t=72, b=56),
    )

    return fig


# ---------------------------------------------------------------------------
# 3. Severity donut — item count by severity band
# ---------------------------------------------------------------------------

def plot_severity_donut(
    enriched_df: pd.DataFrame,
    title: str = "Variances by Severity",
    material_only: bool = False,
) -> go.Figure:
    """
    Donut chart showing the distribution of line items across severity bands.

    Design
    ------
    - Four segments: Critical (red), Major (orange), Moderate (amber), Minor (gray).
    - The count for each band is displayed inside the segment as the text label,
      so the reader can immediately see "3 Critical items" without a tooltip.
    - The hole shows the total item count as a centred annotation.
    - Bands with zero items are omitted to keep the chart clean.

    Parameters
    ----------
    enriched_df : pd.DataFrame
        Output of :func:`variance_engine.compute_variances`.
    title : str
        Chart title.
    material_only : bool
        If True, count only material line items (Material == True).
        Useful for an executive view that filters out noise.

    Returns
    -------
    go.Figure
    """
    df = enriched_df[enriched_df["Material"]].copy() if material_only else enriched_df.copy()

    # Count per severity, preserving canonical order
    counts = df["Severity"].value_counts()
    ordered_labels = [s for s in SEVERITY_ORDER if s in counts.index]
    ordered_values = [counts[s] for s in ordered_labels]

    # Severity → color mapping
    severity_colors = {
        "Critical": "#DC2626",   # red-600
        "Major":    "#EA580C",   # orange-600
        "Moderate": "#D97706",   # amber-600
        "Minor":    "#94A3B8",   # slate-400
    }
    bar_colors = [severity_colors[s] for s in ordered_labels]

    total_items = sum(ordered_values)

    fig = go.Figure(go.Pie(
        labels=ordered_labels,
        values=ordered_values,
        hole=0.58,
        marker=dict(
            colors=bar_colors,
            line=dict(color="#FFFFFF", width=3),
        ),
        texttemplate="%{label}<br><b>%{value}</b>",
        textposition="inside",
        textfont=dict(family=_FONT_FAMILY, size=12, color="#FFFFFF"),
        hovertemplate="<b>%{label}</b><br>%{value} items (%{percent})<extra></extra>",
        direction="clockwise",
        sort=False,   # respect SEVERITY_ORDER
    ))

    # Centre annotation showing total
    scope_label = "material" if material_only else "total"
    fig.add_annotation(
        text=f"<b>{total_items}</b><br><span style='font-size:11px;color:#64748B'>{scope_label}</span>",
        x=0.5, y=0.5,
        showarrow=False,
        font=dict(family=_FONT_FAMILY, size=18, color="#1E293B"),
        align="center",
    )

    _apply_layout(
        fig,
        title=dict(
            text=title,
            font=dict(family=_FONT_FAMILY, size=16, color="#1E293B"),
            x=0.02,
            xanchor="left",
        ),
        showlegend=True,
        legend=dict(
            orientation="v",
            yanchor="middle",
            y=0.5,
            xanchor="left",
            x=1.02,
            font=dict(size=12),
        ),
        margin=dict(l=32, r=120, t=64, b=32),
    )

    return fig


# ---------------------------------------------------------------------------
# 4. Projection chart — placeholder for predictive feature
# ---------------------------------------------------------------------------

def plot_projection(
    historical_df: pd.DataFrame | None = None,
    projection_df: pd.DataFrame | None = None,
    metric: str = "Revenue",
    title: str | None = None,
) -> go.Figure:
    """
    Line chart showing historical actuals with a dashed projection line and
    shaded confidence band for the next quarter.

    Current state: PLACEHOLDER.
    The function signature and layout are fully defined so the Streamlit UI can
    call this function today and receive a chart with a "coming soon" message.
    The projection logic will be wired in a later sprint.

    Parameters
    ----------
    historical_df : pd.DataFrame | None
        One row per period with columns: Period (str), Actual (float).
        When None a sample trace is rendered so the chart is not blank.
    projection_df : pd.DataFrame | None
        One row per projected period with columns:
            Period     (str)   — period label, e.g. "Q1 2025"
            Projected  (float) — point estimate
            Lower      (float) — lower confidence bound
            Upper      (float) — upper confidence bound
        When None the projection band is omitted.
    metric : str
        The metric being projected (used in axis label and hover).
    title : str | None
        Chart title.  Defaults to f"{metric} — Actuals & Q+1 Projection".

    Returns
    -------
    go.Figure
        A fully styled figure.  If both inputs are None a placeholder figure
        is returned with an annotation explaining the feature is pending.

    Future wiring notes
    -------------------
    - historical_df will be built from compute_variances() output, grouped by Period.
    - projection_df will be produced by a forecasting module (linear trend or
      Prophet) and passed in from the Streamlit sidebar.
    - The confidence band is rendered as a filled area trace between Lower and Upper.
    """
    _title = title or f"{metric} — Actuals & Q+1 Projection"

    # --- Placeholder path: neither input supplied ---
    if historical_df is None and projection_df is None:
        fig = go.Figure()
        _apply_layout(
            fig,
            title=dict(
                text=_title,
                font=dict(family=_FONT_FAMILY, size=16, color="#1E293B"),
                x=0.02,
                xanchor="left",
            ),
            xaxis=dict(**_AXIS_STYLE, showgrid=False, visible=False),
            yaxis=dict(**_AXIS_STYLE, visible=False),
            margin=dict(l=32, r=32, t=64, b=32),
        )
        fig.add_annotation(
            text=(
                "<b>Projection feature coming soon</b><br>"
                "<span style='font-size:12px;color:#64748B'>"
                "Multi-period data required. Upload actuals for 3+ quarters to unlock.</span>"
            ),
            x=0.5, y=0.5,
            showarrow=False,
            font=dict(family=_FONT_FAMILY, size=15, color="#334155"),
            align="center",
            xref="paper",
            yref="paper",
            bgcolor="#F8FAFC",
            bordercolor="#E2E8F0",
            borderwidth=1,
            borderpad=16,
        )
        return fig

    # --- Live path: render actuals + optional projection band ---
    fig = go.Figure()

    # Historical actuals line
    if historical_df is not None:
        fig.add_trace(go.Scatter(
            x=historical_df["Period"],
            y=historical_df["Actual"],
            mode="lines+markers",
            name="Actual",
            line=dict(color="#0D9488", width=2.5),
            marker=dict(size=7, color="#0D9488", line=dict(width=2, color="#FFFFFF")),
            hovertemplate=f"<b>%{{x}}</b><br>{metric}: %{{y:$,.0f}}<extra></extra>",
        ))

    # Projection band (confidence interval as filled area)
    if projection_df is not None:
        # Upper bound (invisible line, forms top of shaded area)
        fig.add_trace(go.Scatter(
            x=projection_df["Period"],
            y=projection_df["Upper"],
            mode="lines",
            line=dict(width=0),
            showlegend=False,
            hoverinfo="skip",
        ))
        # Lower bound — fill to previous (upper) trace
        fig.add_trace(go.Scatter(
            x=projection_df["Period"],
            y=projection_df["Lower"],
            mode="lines",
            line=dict(width=0),
            fill="tonexty",
            fillcolor="rgba(13, 148, 136, 0.12)",   # teal, 12% opacity
            name="Confidence band",
            hoverinfo="skip",
            showlegend=True,
        ))
        # Point estimate — dashed line
        fig.add_trace(go.Scatter(
            x=projection_df["Period"],
            y=projection_df["Projected"],
            mode="lines+markers",
            name="Projection",
            line=dict(color="#0D9488", width=2, dash="dash"),
            marker=dict(size=7, color="#0D9488", symbol="circle-open",
                        line=dict(width=2, color="#0D9488")),
            hovertemplate=f"<b>%{{x}}</b><br>Projected {metric}: %{{y:$,.0f}}<extra></extra>",
        ))

    _apply_layout(
        fig,
        title=dict(
            text=_title,
            font=dict(family=_FONT_FAMILY, size=16, color="#1E293B"),
            x=0.02,
            xanchor="left",
        ),
        xaxis=dict(**_AXIS_STYLE, showgrid=False, title="Period"),
        yaxis=dict(**_AXIS_STYLE, tickformat="$,.0f", title=f"{metric} (USD)"),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="right",
            x=1,
        ),
        margin=dict(l=72, r=32, t=72, b=56),
    )

    return fig
