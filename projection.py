"""
projection.py — Forward-looking variance projection for FP&A Variance Copilot.

Design philosophy
-----------------
This is not a statistical forecast.  It is a deliberate "what-if" scenario:
if nothing changes and the current-period variance pattern persists, what does
next quarter look like?  The math is intentionally transparent so any number
on screen can be explained in one sentence to a CFO.

Projection methodology
-----------------------
Material line items  → Projected Variance ($) = Current Variance ($)
                        (dollar run-rate: the same absolute gap recurs next quarter)
Non-material items   → Projected Variance ($) = 0
                        (assumed to normalize; noise should not drive the narrative)

Risk flag
---------
An item is "at risk" when it is unfavorable AND its cumulative two-quarter
variance (current + projected) exceeds a configurable threshold (default 20%
of the two-quarter combined budget).  This surfaces items that are becoming
structurally problematic, not just one-off misses.

Public API
----------
build_projection(enriched_df, next_q_growth_pct, risk_threshold_pct) -> pd.DataFrame
projection_summary(proj_df, period, next_q_label) -> dict
build_projection_chart_data(proj_df) -> pd.DataFrame
next_quarter_label(period) -> str
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd


# ── Constants ─────────────────────────────────────────────────────────────────

RISK_THRESHOLD_PCT = 0.20   # |cumulative variance %| >= 20% → At Risk


# ── Public API ────────────────────────────────────────────────────────────────

def build_projection(
    enriched_df: pd.DataFrame,
    next_q_growth_pct: float = 0.0,
    risk_threshold_pct: float = RISK_THRESHOLD_PCT,
) -> pd.DataFrame:
    """
    Project next-quarter actuals by assuming material variances persist at
    the same dollar run-rate.

    Parameters
    ----------
    enriched_df       : output of compute_variances()
    next_q_growth_pct : budget change for next quarter, e.g. 5.0 = 5% growth.
                        Applied uniformly to every line item's budget.
                        Default 0.0 = next quarter budget equals current budget.
    risk_threshold_pct: decimal threshold for the cumulative risk flag.
                        Default 0.20 = flag items where two-quarter cumulative
                        variance exceeds 20% of the combined two-quarter budget.

    Returns
    -------
    pd.DataFrame — one row per original line item with columns:

        From source
        -----------
        Line Item, Category, Favorable, Severity, Material
        Budget, Actual, Variance ($), Variance (%)

        Projection
        ----------
        Next Q Budget          : Current Budget × (1 + next_q_growth_pct / 100)
        Projected Actual       : Next Q Budget + Projected Variance ($)
        Projected Variance ($) : Current Variance ($) for material items, else 0
        Projected Variance (%) : Projected Variance ($) / |Next Q Budget| × 100

        Risk
        ----
        Cumulative Variance ($) : Variance ($) + Projected Variance ($)
        Cumulative Variance (%) : Cumulative Variance ($) / |two-quarter budget| × 100
        At Risk                 : bool — unfavorable AND cumulative breach
        Risk Reason             : str  — human-readable explanation, empty if safe

    Notes
    -----
    The dollar run-rate assumption (same $ variance, not same %) is conservative
    for revenue misses and may overstate cost risks when next-quarter budget
    differs materially.  The current default keeps the story clean and auditable.
    """
    df = enriched_df.copy()

    growth_factor = 1.0 + next_q_growth_pct / 100.0

    # ── Next quarter budget ───────────────────────────────────────────────────
    df["Next Q Budget"] = df["Budget"] * growth_factor

    # ── Projected variance: run-rate for material, zero otherwise ─────────────
    df["Projected Variance ($)"] = np.where(
        df["Material"],
        df["Variance ($)"],
        0.0,
    )

    # ── Projected actual & % ──────────────────────────────────────────────────
    df["Projected Actual"] = df["Next Q Budget"] + df["Projected Variance ($)"]
    df["Projected Variance (%)"] = np.where(
        df["Next Q Budget"] != 0,
        df["Projected Variance ($)"] / df["Next Q Budget"].abs() * 100,
        np.nan,
    )

    # ── Cumulative (current + projected combined) ─────────────────────────────
    two_q_budget = df["Budget"] + df["Next Q Budget"]
    df["Cumulative Variance ($)"] = df["Variance ($)"] + df["Projected Variance ($)"]
    df["Cumulative Variance (%)"] = np.where(
        two_q_budget != 0,
        df["Cumulative Variance ($)"] / two_q_budget.abs() * 100,
        np.nan,
    )

    # ── Risk flag ─────────────────────────────────────────────────────────────
    cum_abs_pct = df["Cumulative Variance (%)"].abs()
    df["At Risk"] = (~df["Favorable"]) & (cum_abs_pct >= risk_threshold_pct * 100)

    df["Risk Reason"] = df.apply(
        lambda row: (
            f"Cumulative {row['Cumulative Variance (%)']:+.1f}% over two quarters "
            f"(threshold >{risk_threshold_pct * 100:.0f}%)"
            if row["At Risk"] else ""
        ),
        axis=1,
    )

    out_cols = [
        "Line Item", "Category", "Favorable", "Severity", "Material",
        "Budget", "Actual", "Variance ($)", "Variance (%)",
        "Next Q Budget", "Projected Actual",
        "Projected Variance ($)", "Projected Variance (%)",
        "Cumulative Variance ($)", "Cumulative Variance (%)",
        "At Risk", "Risk Reason",
    ]
    return df[[c for c in out_cols if c in df.columns]].reset_index(drop=True)


def projection_summary(
    proj_df: pd.DataFrame,
    period: str,
    next_q_label: str = "Next Quarter",
) -> dict:
    """
    Aggregate key statistics from a projection DataFrame.

    Parameters
    ----------
    proj_df      : output of build_projection()
    period       : current reporting period label, e.g. "Q4 2024"
    next_q_label : projected period label, e.g. "Q1 2025"

    Returns
    -------
    dict with keys:
        projected_net_budget      : float
        projected_net_variance    : float (sum of all Projected Variance ($))
        projected_net_variance_pct: float
        n_at_risk                 : int
        at_risk_items             : list[str]
        worst_item                : str   — unfavorable item with largest |Projected Variance ($)|
        worst_variance            : float
        improving_items           : list[str] — favorable items with non-zero projected variance
        period                    : str
        next_q_label              : str
    """
    total_proj_budget   = proj_df["Next Q Budget"].sum()
    total_proj_variance = proj_df["Projected Variance ($)"].sum()
    proj_pct = (
        total_proj_variance / abs(total_proj_budget) * 100
        if total_proj_budget else 0.0
    )

    at_risk_df   = proj_df[proj_df["At Risk"]]
    improving_df = proj_df[
        proj_df["Favorable"] & (proj_df["Projected Variance ($)"] != 0)
    ]

    unfav_nonzero = proj_df[
        ~proj_df["Favorable"] & (proj_df["Projected Variance ($)"] != 0)
    ]
    if len(unfav_nonzero) > 0:
        worst_row     = unfav_nonzero.loc[unfav_nonzero["Projected Variance ($)"].abs().idxmax()]
        worst_item    = str(worst_row["Line Item"])
        worst_variance = float(worst_row["Projected Variance ($)"])
    else:
        worst_item    = "None"
        worst_variance = 0.0

    return {
        "projected_net_budget":       total_proj_budget,
        "projected_net_variance":     total_proj_variance,
        "projected_net_variance_pct": proj_pct,
        "n_at_risk":                  len(at_risk_df),
        "at_risk_items":              at_risk_df["Line Item"].tolist(),
        "worst_item":                 worst_item,
        "worst_variance":             worst_variance,
        "improving_items":            improving_df["Line Item"].tolist(),
        "period":                     period,
        "next_q_label":               next_q_label,
    }


def build_projection_chart_data(proj_df: pd.DataFrame) -> pd.DataFrame:
    """
    Return a chart-ready slice of a projection DataFrame.

    Filters to material items only, selects the columns the projection chart
    needs, and sorts by absolute projected variance descending so the largest
    exposures appear at the top of the chart.

    Parameters
    ----------
    proj_df : output of build_projection()

    Returns
    -------
    pd.DataFrame with columns:
        Line Item, Category, Favorable,
        Variance ($), Variance (%),
        Projected Variance ($), Projected Variance (%),
        Cumulative Variance (%), At Risk
    """
    material = proj_df[proj_df["Material"]].copy()
    material["_abs_proj"] = material["Projected Variance ($)"].abs()
    material.sort_values("_abs_proj", ascending=False, inplace=True)
    material.drop(columns=["_abs_proj"], inplace=True)

    cols = [
        "Line Item", "Category", "Favorable",
        "Variance ($)", "Variance (%)",
        "Projected Variance ($)", "Projected Variance (%)",
        "Cumulative Variance (%)", "At Risk",
    ]
    return material[[c for c in cols if c in material.columns]].reset_index(drop=True)


def next_quarter_label(period: str) -> str:
    """
    Infer the next-quarter label from a period string.

    Handles the most common formats:
        "Q4 2024"  → "Q1 2025"
        "Q3 2024"  → "Q4 2024"
        "H1 2024"  → "H2 2024"
        "H2 2024"  → "H1 2025"
        "FY2024"   → "FY2025"
        Anything else → "Next Quarter"

    Parameters
    ----------
    period : str — current period label from the sidebar

    Returns
    -------
    str — inferred next period label
    """
    period = period.strip()

    # Quarterly: "Q1 2024", "Q4 2024", etc.
    m = re.match(r"Q([1-4])\s*(\d{4})", period, re.IGNORECASE)
    if m:
        q, y = int(m.group(1)), int(m.group(2))
        if q == 4:
            return f"Q1 {y + 1}"
        return f"Q{q + 1} {y}"

    # Half-year: "H1 2024", "H2 2024"
    m = re.match(r"H([12])\s*(\d{4})", period, re.IGNORECASE)
    if m:
        h, y = int(m.group(1)), int(m.group(2))
        if h == 2:
            return f"H1 {y + 1}"
        return f"H2 {y}"

    # Full year: "FY2024", "FY 2024"
    m = re.match(r"FY\s*(\d{4})", period, re.IGNORECASE)
    if m:
        return f"FY{int(m.group(1)) + 1}"

    return "Next Quarter"


# ── Formatting helpers ────────────────────────────────────────────────────────

def _d(value: float, compact: bool = False, sign: bool = False) -> str:
    """Format a float as a currency string."""
    if pd.isna(value):
        return "—"
    prefix = ("+" if value >= 0 else "") if sign else ""
    neg    = "-" if value < 0 else ""
    abs_v  = abs(value)
    if compact:
        if abs_v >= 1_000_000:
            return f"{prefix}{neg}${abs_v / 1_000_000:.1f}M"
        if abs_v >= 1_000:
            return f"{prefix}{neg}${abs_v / 1_000:.0f}K"
    return f"{prefix}${value:,.0f}"
