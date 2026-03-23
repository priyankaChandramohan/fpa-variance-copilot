"""
variance_engine.py — Computation core for FP&A Variance Copilot.

All pure-Python / pandas logic lives here.  No UI, no API calls.
Each function is independently testable and documented for interview walkthroughs.
"""

from __future__ import annotations

import pandas as pd
import numpy as np
from typing import Any

from config import (
    REQUIRED_COLUMNS,
    MATERIALITY_PCT,
    MATERIALITY_ABS,
    SEVERITY_BANDS,
    REVENUE_CATEGORY_KEYWORDS,
    TOP_N_VARIANCES,
    WATERFALL_MIN_IMPACT_PCT,
)


# ---------------------------------------------------------------------------
# 1. validate_csv
# ---------------------------------------------------------------------------

def validate_csv(df: pd.DataFrame) -> tuple[bool, str]:
    """
    Validate that a DataFrame conforms to the expected upload schema.

    Checks performed (in order):
        1. All required columns are present (case-insensitive match).
        2. Budget and Actual columns contain numeric data.
        3. No row has both Budget and Actual as NaN (completely empty rows are dropped
           upstream, but mixed-NaN rows are flagged).
        4. At least one data row exists after dropping fully-blank rows.

    Parameters
    ----------
    df : pd.DataFrame
        Raw DataFrame as read from the uploaded CSV.

    Returns
    -------
    (is_valid, message) : tuple[bool, str]
        is_valid — True if the data passes all checks.
        message  — Human-readable explanation; empty string when valid.

    Notes
    -----
    Column matching is case-insensitive and strips surrounding whitespace so that
    "  budget " and "Budget" both resolve correctly.
    """
    # Normalise column names for comparison
    col_map = {c.strip().lower(): c for c in df.columns}

    # --- Check 1: required columns present ---
    missing = []
    for required in REQUIRED_COLUMNS:
        if required.lower() not in col_map:
            missing.append(required)
    if missing:
        return False, f"Missing required column(s): {', '.join(missing)}. Expected: {', '.join(REQUIRED_COLUMNS)}."

    # Rename to canonical names so downstream code can use exact strings
    rename = {col_map[r.lower()]: r for r in REQUIRED_COLUMNS if col_map[r.lower()] != r}
    if rename:
        df.rename(columns=rename, inplace=True)

    # --- Check 2: numeric Budget / Actual ---
    for col in ["Budget", "Actual"]:
        non_numeric = df[col].apply(lambda v: not _is_numeric(v) if pd.notna(v) else False)
        if non_numeric.any():
            bad_rows = df.index[non_numeric].tolist()[:3]  # show first 3 offenders
            return False, (
                f"Column '{col}' contains non-numeric values at row(s) {[r + 2 for r in bad_rows]} "
                f"(row 1 = header). Remove currency symbols or commas and re-upload."
            )

    # --- Check 3: no rows where both Budget AND Actual are NaN ---
    both_null = df["Budget"].isna() & df["Actual"].isna()
    if both_null.any():
        count = int(both_null.sum())
        return False, f"{count} row(s) have both Budget and Actual empty. Please fill or remove them."

    # --- Check 4: at least one usable row ---
    if df.shape[0] == 0:
        return False, "The uploaded file contains no data rows."

    return True, ""


def _is_numeric(value: Any) -> bool:
    """Return True if *value* can be interpreted as a finite float."""
    try:
        f = float(value)
        return np.isfinite(f)
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# 2. compute_variances
# ---------------------------------------------------------------------------

def compute_variances(df: pd.DataFrame) -> pd.DataFrame:
    """
    Enrich a validated DataFrame with derived variance columns.

    Added columns
    -------------
    Variance ($)   : Actual − Budget  (always; negative means under-budget)
    Variance (%)   : Variance ($) / |Budget| × 100  (NaN when Budget == 0)
    Favorable      : bool — True when the variance is operationally good
    Severity       : str  — one of Critical / Major / Moderate / Minor
    Material       : bool — True when |Var%| >= MATERIALITY_PCT **or** |Var$| >= MATERIALITY_ABS

    Sign convention
    ---------------
    Revenue / gross-profit categories: Actual > Budget is favorable (more revenue = good).
    All cost categories:               Actual < Budget is favorable (lower spend = good).
    The category type is determined by :func:`_is_revenue_category`.

    Parameters
    ----------
    df : pd.DataFrame
        Output of :func:`validate_csv` (columns already renamed to canonical names).

    Returns
    -------
    pd.DataFrame
        Original DataFrame with five new columns appended.  The input is not
        mutated — a copy is returned.
    """
    result = df.copy()

    # Cast to float to handle any object columns that slipped through
    result["Budget"] = pd.to_numeric(result["Budget"], errors="coerce")
    result["Actual"] = pd.to_numeric(result["Actual"], errors="coerce")

    # --- Variance ($) ---
    result["Variance ($)"] = result["Actual"] - result["Budget"]

    # --- Variance (%) ---
    # Use absolute value of budget as denominator so a -$100K budget item
    # doesn't flip the sign of the percentage.
    result["Variance (%)"] = np.where(
        result["Budget"] != 0,
        (result["Variance ($)"] / result["Budget"].abs()) * 100,
        np.nan,
    )

    # --- Favorable flag ---
    result["Favorable"] = result.apply(
        lambda row: _is_favorable(row["Variance ($)"], row["Category"]),
        axis=1,
    )

    # --- Severity ---
    result["Severity"] = result["Variance (%)"].apply(
        lambda pct: _assign_severity(pct)
    )

    # --- Material ---
    abs_pct = result["Variance (%)"].abs()
    abs_dollar = result["Variance ($)"].abs()
    result["Material"] = (abs_pct >= MATERIALITY_PCT * 100) | (abs_dollar >= MATERIALITY_ABS)

    return result


def _is_revenue_category(category: str) -> bool:
    """Return True if *category* follows the revenue sign convention."""
    cat_lower = str(category).lower()
    return any(keyword in cat_lower for keyword in REVENUE_CATEGORY_KEYWORDS)


def _is_favorable(variance_dollars: float, category: str) -> bool:
    """
    Determine whether a dollar variance is operationally favorable.

    For revenue categories a positive variance (actual > budget) is good.
    For cost categories a negative variance (actual < budget) is good.
    """
    if pd.isna(variance_dollars):
        return False
    if _is_revenue_category(category):
        return variance_dollars >= 0
    else:
        return variance_dollars <= 0


def _assign_severity(variance_pct: float | None) -> str:
    """
    Map an absolute variance percentage to a severity label.

    Iterates SEVERITY_BANDS (defined in config) from highest to lowest
    threshold and returns the first label whose threshold is met.
    Returns 'Minor' for NaN inputs (e.g., zero-budget lines).
    """
    if pd.isna(variance_pct):
        return "Minor"
    abs_pct = abs(variance_pct) / 100   # bands stored as decimals
    for label, threshold in SEVERITY_BANDS:
        if abs_pct >= threshold:
            return label
    return "Minor"


# ---------------------------------------------------------------------------
# 3. summarize_by_category
# ---------------------------------------------------------------------------

def summarize_by_category(df: pd.DataFrame) -> pd.DataFrame:
    """
    Aggregate variance data by Category.

    Returns one row per Category with:
        Budget ($)      : sum of Budget for the category
        Actual ($)      : sum of Actual for the category
        Variance ($)    : sum of Variance ($)
        Variance (%)    : category-level % = Var($) / |Budget($)| × 100
        Material Items  : count of material line items in the category
        Dominant Severity: worst (highest) severity band present

    Parameters
    ----------
    df : pd.DataFrame
        Output of :func:`compute_variances`.

    Returns
    -------
    pd.DataFrame
        Sorted by |Variance ($)| descending so the biggest-impact categories
        appear first — ready to render as a summary table.
    """
    agg = (
        df.groupby("Category", sort=False)
        .agg(
            Budget=("Budget", "sum"),
            Actual=("Actual", "sum"),
            Variance_Dollar=("Variance ($)", "sum"),
            Material_Items=("Material", "sum"),
        )
        .reset_index()
    )

    agg.rename(columns={
        "Budget": "Budget ($)",
        "Actual": "Actual ($)",
        "Variance_Dollar": "Variance ($)",
        "Material_Items": "Material Items",
    }, inplace=True)

    # Recalculate % at category level (avoids averaging individual row %s)
    agg["Variance (%)"] = np.where(
        agg["Budget ($)"] != 0,
        (agg["Variance ($)"] / agg["Budget ($)"].abs()) * 100,
        np.nan,
    )

    # Dominant severity: the worst severity level that appears in this category
    severity_rank = {label: i for i, (label, _) in enumerate(SEVERITY_BANDS)}
    worst_severity = (
        df.groupby("Category")["Severity"]
        .apply(lambda s: min(s, key=lambda x: severity_rank.get(x, 99)))
        .reset_index()
        .rename(columns={"Severity": "Dominant Severity"})
    )
    agg = agg.merge(worst_severity, on="Category", how="left")

    # Sort by absolute dollar impact
    agg["_abs_var"] = agg["Variance ($)"].abs()
    agg.sort_values("_abs_var", ascending=False, inplace=True)
    agg.drop(columns=["_abs_var"], inplace=True)
    agg.reset_index(drop=True, inplace=True)

    return agg


# ---------------------------------------------------------------------------
# 4. build_waterfall_data
# ---------------------------------------------------------------------------

def build_waterfall_data(df: pd.DataFrame) -> pd.DataFrame:
    """
    Prepare data for a Plotly waterfall chart showing the bridge from
    Budget total to Actual total through material variances.

    Structure of the returned DataFrame
    ------------------------------------
    label   : str   — bar label shown on the x-axis
    value   : float — the amount this bar contributes (positive or negative)
    measure : str   — 'absolute' (Budget/Actual totals) or 'relative' (bridge bars)
    text    : str   — formatted label displayed on the bar

    Chart interpretation
    --------------------
    Bar 1 (absolute) : Total Budget
    Bars 2…N (relative) : each material variance, ordered largest absolute impact first
    Final bar (absolute) : Total Actual  ← Plotly draws this as a totals bar

    Only variances whose |Var($)| / |Total Budget| >= WATERFALL_MIN_IMPACT_PCT are
    included as bridge bars.  Residual immaterial variances are collapsed into a
    single "Other" bar so the chart always reconciles to the correct Actual total.

    Parameters
    ----------
    df : pd.DataFrame
        Output of :func:`compute_variances`.

    Returns
    -------
    pd.DataFrame
        Ready to unpack directly into `go.Waterfall(x=..., y=..., measure=...)`.
    """
    total_budget = df["Budget"].sum()
    total_actual = df["Actual"].sum()

    # Filter to material bridge items
    min_abs = abs(total_budget) * WATERFALL_MIN_IMPACT_PCT
    material_df = df[df["Variance ($)"].abs() >= min_abs].copy()
    material_df.sort_values("Variance ($)", key=lambda s: s.abs(), ascending=False, inplace=True)

    rows = []

    # Opening bar
    rows.append({
        "label": "Budget",
        "value": total_budget,
        "measure": "absolute",
        "text": _fmt_currency(total_budget),
    })

    # Bridge bars
    included_var_sum = 0.0
    for _, row in material_df.iterrows():
        var_d = row["Variance ($)"]
        rows.append({
            "label": row["Line Item"],
            "value": var_d,
            "measure": "relative",
            "text": _fmt_currency(var_d, show_sign=True),
        })
        included_var_sum += var_d

    # Residual "Other" bar — keeps chart arithmetically correct
    residual = (total_actual - total_budget) - included_var_sum
    if abs(residual) > 1:   # ignore floating-point dust
        rows.append({
            "label": "Other (net)",
            "value": residual,
            "measure": "relative",
            "text": _fmt_currency(residual, show_sign=True),
        })

    # Closing bar
    rows.append({
        "label": "Actual",
        "value": total_actual,
        "measure": "absolute",
        "text": _fmt_currency(total_actual),
    })

    return pd.DataFrame(rows)


def _fmt_currency(value: float, show_sign: bool = False) -> str:
    """Format *value* as a compact currency string (e.g., +$1.2M, -$450K)."""
    sign = ""
    if show_sign:
        sign = "+" if value >= 0 else ""   # negative sign handled by format
    abs_val = abs(value)
    if abs_val >= 1_000_000:
        return f"{sign}{'-' if value < 0 else ''}${abs_val / 1_000_000:.1f}M"
    if abs_val >= 1_000:
        return f"{sign}{'-' if value < 0 else ''}${abs_val / 1_000:.0f}K"
    return f"{sign}${value:,.0f}"


# ---------------------------------------------------------------------------
# 5. get_material_items
# ---------------------------------------------------------------------------

def get_material_items(
    df: pd.DataFrame,
    n: int = TOP_N_VARIANCES,
    severity_filter: list[str] | None = None,
) -> pd.DataFrame:
    """
    Return the top-N material variances sorted by absolute dollar impact.

    This is the primary data source for the variance table and the AI
    commentary prompt — it surfaces the items a CFO would scrutinise first.

    Parameters
    ----------
    df : pd.DataFrame
        Output of :func:`compute_variances`.
    n : int
        Maximum number of rows to return.  Defaults to TOP_N_VARIANCES in config.
    severity_filter : list[str] | None
        If provided, only rows whose Severity is in this list are returned.
        Example: ``["Critical", "Major"]`` to surface only high-severity items.

    Returns
    -------
    pd.DataFrame
        Filtered and sorted DataFrame with columns:
        Line Item, Category, Budget, Actual, Variance ($), Variance (%), Favorable, Severity, Material.
        Index is reset.

    Examples
    --------
    >>> material = get_material_items(enriched_df, n=5, severity_filter=["Critical", "Major"])
    >>> material[["Line Item", "Variance ($)", "Severity"]]
    """
    material = df[df["Material"]].copy()

    if severity_filter:
        material = material[material["Severity"].isin(severity_filter)]

    material["_abs_var_dollar"] = material["Variance ($)"].abs()
    material.sort_values("_abs_var_dollar", ascending=False, inplace=True)
    material.drop(columns=["_abs_var_dollar"], inplace=True)

    result = material.head(n).reset_index(drop=True)

    # Keep only the canonical display columns (plus any extras that exist)
    display_cols = [
        "Line Item", "Category", "Budget", "Actual",
        "Variance ($)", "Variance (%)", "Favorable", "Severity", "Material",
    ]
    available = [c for c in display_cols if c in result.columns]
    return result[available]
