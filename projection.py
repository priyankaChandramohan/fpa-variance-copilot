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
"""

from __future__ import annotations

import re

import anthropic
import numpy as np
import pandas as pd

from variance_engine import get_material_items


# ── Constants ─────────────────────────────────────────────────────────────────

RISK_THRESHOLD_PCT    = 0.20   # |cumulative variance %| >= 20% → At Risk
PROJECTION_MODEL      = "claude-sonnet-4-20250514"
PROJECTION_MAX_TOKENS = 180    # 2–3 sentences only


# ── Public API ────────────────────────────────────────────────────────────────

def project_next_quarter(
    enriched_df: pd.DataFrame,
    next_q_growth_pct: float = 0.0,
    risk_threshold_pct: float = RISK_THRESHOLD_PCT,
) -> pd.DataFrame:
    """
    Project next-quarter actuals by assuming material variances persist.

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
    for revenue misses (they should be expressed as $) but may overstate cost
    risks when next-quarter budget is materially different.  A growth-adjusted
    variant (same %) is trivially derivable but adds explanation overhead in
    presentations — the current default keeps the story clean.
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


def summarize_projection(
    projection_df: pd.DataFrame,
    period: str,
    next_q_label: str = "Next Quarter",
) -> dict:
    """
    Compute summary statistics for the projection summary card.

    Parameters
    ----------
    projection_df : output of project_next_quarter()
    period        : current reporting period label, e.g. "Q4 2024"
    next_q_label  : projected period label, e.g. "Q1 2025"

    Returns
    -------
    dict with keys:
        projected_net_budget     : float
        projected_net_variance   : float (sum of all Projected Variance ($))
        projected_net_variance_pct: float
        n_at_risk                : int
        at_risk_items            : list[str]
        worst_item               : str   — unfavorable item with largest |Projected Variance ($)|
        worst_variance           : float
        improving_items          : list[str] — favorable items with non-zero projected variance
        period                   : str
        next_q_label             : str
    """
    total_proj_budget   = projection_df["Next Q Budget"].sum()
    total_proj_variance = projection_df["Projected Variance ($)"].sum()
    proj_pct = (
        total_proj_variance / abs(total_proj_budget) * 100
        if total_proj_budget else 0.0
    )

    at_risk_df   = projection_df[projection_df["At Risk"]]
    improving_df = projection_df[
        projection_df["Favorable"] & (projection_df["Projected Variance ($)"] != 0)
    ]

    unfav_nonzero = projection_df[
        ~projection_df["Favorable"] & (projection_df["Projected Variance ($)"] != 0)
    ]
    if len(unfav_nonzero) > 0:
        worst_row     = unfav_nonzero.loc[unfav_nonzero["Projected Variance ($)"].abs().idxmax()]
        worst_item    = str(worst_row["Line Item"])
        worst_variance = float(worst_row["Projected Variance ($)"])
    else:
        worst_item    = "None"
        worst_variance = 0.0

    return {
        "projected_net_budget":      total_proj_budget,
        "projected_net_variance":    total_proj_variance,
        "projected_net_variance_pct": proj_pct,
        "n_at_risk":                  len(at_risk_df),
        "at_risk_items":              at_risk_df["Line Item"].tolist(),
        "worst_item":                 worst_item,
        "worst_variance":             worst_variance,
        "improving_items":            improving_df["Line Item"].tolist(),
        "period":                     period,
        "next_q_label":               next_q_label,
    }


def generate_risk_narrative(
    summary: dict,
    api_key: str,
) -> str | None:
    """
    Generate a 2–3 sentence AI risk narrative for the forward projection.

    Designed to be a fast, focused call (max_tokens=180) — this should feel
    like a one-line analyst note, not a full commentary section.

    Parameters
    ----------
    summary : dict from summarize_projection()
    api_key : Anthropic API key; returns None if empty

    Returns
    -------
    str | None — narrative text, or None if no key / API error
    """
    if not api_key:
        return None

    at_risk_list = ", ".join(summary["at_risk_items"]) or "none"
    proj_var     = summary["projected_net_variance"]
    sign         = "+" if proj_var >= 0 else ""

    prompt = (
        f"Forward projection from {summary['period']} to {summary['next_q_label']}:\n"
        f"- Projected net variance: {sign}{_d(proj_var, compact=True)} "
        f"({summary['projected_net_variance_pct']:+.1f}% of next-quarter budget)\n"
        f"- Items at cumulative risk (>20% threshold): {at_risk_list}\n"
        f"- Largest adverse projected item: {summary['worst_item']} "
        f"({_d(summary['worst_variance'], compact=True, sign=True)})\n"
        f"- Items trending favorably: {', '.join(summary['improving_items'][:3]) or 'none'}\n\n"
        "Write exactly 2–3 sentences of forward-looking risk commentary for a CFO. "
        "Lead with the most important number, identify the primary risk driver, "
        "and end with one specific mitigation action. No preamble."
    )

    try:
        client   = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model=PROJECTION_MODEL,
            max_tokens=PROJECTION_MAX_TOKENS,
            system=(
                "You are a senior FP&A analyst writing a concise risk note. "
                "Be direct. Cite specific dollar amounts. No filler phrases."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception:
        return None


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
