"""
config.py — Central configuration for FP&A Variance Copilot.

All thresholds, mappings, and constants live here so they can be tuned
without touching analysis or UI code.
"""

# ---------------------------------------------------------------------------
# Materiality Thresholds
# ---------------------------------------------------------------------------

# A variance is "material" if it breaches EITHER threshold.
MATERIALITY_PCT = 0.05       # 5% of budget
MATERIALITY_ABS = 50_000     # $50,000 absolute

# ---------------------------------------------------------------------------
# Severity Bands  (evaluated on absolute % deviation)
# ---------------------------------------------------------------------------
# Structure: list of (label, lower_bound_inclusive) sorted high → low.
# The first band whose threshold is met wins.

SEVERITY_BANDS = [
    ("Critical",  0.15),   # |var%| >= 15%
    ("Major",     0.10),   # |var%| >= 10%
    ("Moderate",  0.05),   # |var%| >= 5%
    ("Minor",     0.00),   # everything else
]

# Ordered list used for sorting / categorical axes in charts
SEVERITY_ORDER = ["Critical", "Major", "Moderate", "Minor"]

# ---------------------------------------------------------------------------
# Sign Convention — what makes a variance "favorable"?
# ---------------------------------------------------------------------------
# For revenue / gross-profit line items: Actual > Budget  → favorable (more is better)
# For cost / expense line items:         Actual < Budget  → favorable (less is better)
#
# Any Category string that contains one of the keys below (case-insensitive)
# will use the REVENUE convention; everything else defaults to COST convention.

REVENUE_CATEGORY_KEYWORDS = [
    "revenue",
    "sales",
    "income",
    "gross profit",
    "gross margin",
    "other income",
]

# ---------------------------------------------------------------------------
# Required CSV Columns
# ---------------------------------------------------------------------------

REQUIRED_COLUMNS = ["Line Item", "Category", "Budget", "Actual"]
OPTIONAL_COLUMNS = ["Period", "Notes"]

# ---------------------------------------------------------------------------
# Color Palette  (slate / teal theme)
# ---------------------------------------------------------------------------

COLORS = {
    # Variance direction
    "favorable":    "#0d9488",   # teal-600
    "unfavorable":  "#e11d48",   # rose-600
    "neutral":      "#64748b",   # slate-500

    # Severity
    "critical":     "#9f1239",   # rose-800
    "major":        "#c2410c",   # orange-700
    "moderate":     "#d97706",   # amber-600
    "minor":        "#64748b",   # slate-500

    # Waterfall
    "budget_bar":   "#334155",   # slate-700
    "actual_bar":   "#0f766e",   # teal-700
    "increase":     "#e11d48",   # unfavorable cost increase / revenue decrease
    "decrease":     "#0d9488",   # favorable cost decrease / revenue increase

    # UI chrome
    "background":   "#0f172a",   # slate-900
    "surface":      "#1e293b",   # slate-800
    "border":       "#334155",   # slate-700
    "text_primary": "#f1f5f9",   # slate-100
    "text_muted":   "#94a3b8",   # slate-400
}

# ---------------------------------------------------------------------------
# Claude API Settings
# ---------------------------------------------------------------------------

CLAUDE_MODEL = "claude-sonnet-4-6"

# System prompt injected before every variance analysis request
CLAUDE_SYSTEM_PROMPT = """You are a senior FP&A analyst embedded in a CFO's budget-review tool.
When given variance data:
- Lead with the most material insight, not a summary of what the numbers say.
- Distinguish between one-time items and run-rate concerns.
- Flag any variance that may indicate a forecasting-methodology issue.
- Recommend one concrete action per critical variance.
- Be direct: CFOs do not want hedged, generic commentary.
- Format your response in clear sections: Key Findings, Root Cause Hypotheses, Recommended Actions."""

CLAUDE_MAX_TOKENS = 1024
CLAUDE_TEMPERATURE = 0.3     # Low temperature — we want consistent, analytical tone

# ---------------------------------------------------------------------------
# Chart / Display Settings
# ---------------------------------------------------------------------------

# Number of items shown in the top-variances table by default
TOP_N_VARIANCES = 10

# Waterfall: include items whose absolute variance is at least this % of total budget
WATERFALL_MIN_IMPACT_PCT = 0.01   # 1%

# Currency display
CURRENCY_SYMBOL = "$"
CURRENCY_LOCALE = "en_US"
