"""
commentary.py — Tone-controlled commentary, NL querying, and rule-based fallback.

Three capabilities
------------------
1. generate_commentary(enriched_df, cat_df, period, tone, api_key)
   Calls Claude with a tone-specific system prompt when an API key is present,
   otherwise falls back to a rule-based narrative built from the variance data.

2. answer_question(enriched_df, cat_df, period, question, chat_history, api_key, commentary)
   Sends the full variance context + conversation history to Claude so users
   can ask follow-up questions in a chat interface.  No API key → returns a
   prompt asking the user to add one.

3. Rule-based fallback
   Each of the three tones has a distinct structure and language register
   generated entirely from computed DataFrame values — no API call needed.

Model
-----
All API calls use claude-sonnet-4-20250514.
"""

from __future__ import annotations

import anthropic
import pandas as pd

from config import REVENUE_CATEGORY_KEYWORDS
from variance_engine import get_material_items


# ── Constants ─────────────────────────────────────────────────────────────────

COMMENTARY_MODEL = "claude-sonnet-4-20250514"

TONES = ["Board Presentation", "Internal Team Update", "Investor Call Prep"]
DEFAULT_TONE = "Board Presentation"

# Max chat turns kept in the context window for multi-turn queries
_MAX_HISTORY_TURNS = 10

# Projection narrative settings (short focused call — 2-3 sentences only)
_PROJECTION_MODEL      = "claude-sonnet-4-20250514"
_PROJECTION_MAX_TOKENS = 180

# ── Tone-specific system prompts ──────────────────────────────────────────────

_SYSTEM_PROMPTS: dict[str, str] = {

    "Board Presentation": """
You are a senior FP&A analyst preparing variance commentary for a board of directors presentation.

AUDIENCE: Board members — they want strategic signal, not a ledger recitation.

STRUCTURE your response in exactly these five sections:
1. BOTTOM LINE (2–3 sentences): Net financial position and directional judgment for the period.
2. REVENUE PERFORMANCE: Group all revenue variances. Lead with the strategic read, then the items.
3. COST DISCIPLINE: Split into structural overruns (ongoing risk) vs. one-time or investment spend.
4. INVESTMENT AREAS: Discretionary spend that deviated — explain the logic or flag the absence of one.
5. RECOMMENDATIONS: Exactly 3 specific, actionable bullets — one per major risk area.

RULES:
- Open every section with the aggregate $ and % impact before the narrative.
- Each finding needs a root-cause hypothesis, clearly labelled as such.
- End the commentary with one sentence on the forward implication for next quarter.
- Never start a sentence with "I". Lead with the finding, not the analyst.
- Avoid passive voice — say who did what or what happened.
- Do not hedge with "it appears" or "it seems" — state the most likely cause directly.
""".strip(),

    "Internal Team Update": """
You are an FP&A analyst writing a direct internal variance update for the operating team.

AUDIENCE: Functional leads and finance partners who know the business and speak the jargon.

STRUCTURE your response in exactly these five sections:
1. TL;DR (one sentence, bold): The single most important thing to know this period.
2. NEEDS ATTENTION: Each unfavorable material variance as a bullet — $ impact, likely cause, suggested action. Flag the probable functional owner (use role/team, not names).
3. WORKING WELL: Favorable variances worth calling out. One line each, be brief.
4. NET POSITION: One sentence on overall quarter health vs. budget.
5. TO-DOs: 3–5 concrete action items written as tasks (someone could paste these into Jira/Asana).

RULES:
- No pleasantries — go straight to numbers.
- Keep total length under 300 words. People will read the whole thing.
- Flag any variance that may affect the Q+1 forecast assumption.
- Use shorthand where appropriate (e.g. "CoGS", "S&M", "infra").
- End each NEEDS ATTENTION item with: "→ Owner: [functional area]".
""".strip(),

    "Investor Call Prep": """
You are a senior Investor Relations advisor preparing talking points for an earnings call.

AUDIENCE: Buy-side analysts and institutional investors who will hold you to every word.

STRUCTURE your response in exactly these six sections:
1. KEY MESSAGES (3 bullets): The 3 things you want investors to remember after the call.
2. REVENUE NARRATIVE: Beat/miss framing vs. budget-as-guidance. Explain drivers, note sustainability.
3. COST NARRATIVE: Frame overruns as investments where the data supports it; flag structural risks.
4. BEAT / MISS SUMMARY: Clean list — item name, $ variance, % variance, one-word characterisation.
5. ANTICIPATED Q&A: 3 hard questions analysts will ask, with a model response for each.
6. LANGUAGE GUIDANCE: 3 phrases to use; 3 phrases to avoid; one forward-looking statement caution.

RULES:
- Frame all variances against "our plan" or "our guidance" (treat budget as guidance proxy).
- Use precise hedging on causality: "we believe", "data suggests", "subject to".
- Never project future performance without a qualifier like "assuming current trends persist".
- For misses: Acknowledge, explain cause, state remediation. Do not minimise.
- For beats: Celebrate briefly, then explain whether the driver is sustainable.
- This document is DRAFT / INTERNAL — mark it clearly.
""".strip(),
}


# ── Public API ────────────────────────────────────────────────────────────────

def generate_commentary(
    enriched_df: pd.DataFrame,
    cat_df: pd.DataFrame,
    period: str,
    tone: str = DEFAULT_TONE,
    api_key: str = "",
) -> str:
    """
    Generate variance commentary for the given tone.

    When *api_key* is provided, calls Claude with the tone-specific system prompt.
    When no key is available, returns a rule-based narrative derived entirely
    from the variance DataFrames — useful for demos, previews, or offline use.

    Parameters
    ----------
    enriched_df : output of compute_variances()
    cat_df      : output of summarize_by_category()
    period      : reporting period label, e.g. "Q4 2024"
    tone        : one of TONES; defaults to DEFAULT_TONE
    api_key     : Anthropic API key; empty string → rule-based fallback

    Returns
    -------
    str — commentary text ready to render in the UI or PPTX
    """
    if tone not in TONES:
        tone = DEFAULT_TONE

    if api_key:
        return _claude_commentary(enriched_df, cat_df, period, tone, api_key)
    return _rule_based(enriched_df, cat_df, period, tone)


def ask_variance_question(
    enriched_df: pd.DataFrame,
    cat_df: pd.DataFrame,
    period: str,
    question: str,
    chat_history: list[dict],
    api_key: str,
    commentary: str | None = None,
) -> str:
    """
    Answer a natural-language question about the variance data.

    The full variance context is injected into the system prompt so Claude has
    access to all line items, not just material ones.  Prior conversation turns
    are passed as the messages list to support multi-turn follow-ups.

    Parameters
    ----------
    enriched_df  : output of compute_variances()
    cat_df       : output of summarize_by_category()
    period       : reporting period label
    question     : user's current question
    chat_history : list of {"role": "user"|"assistant", "content": str} dicts
                   representing prior turns in this session
    api_key      : Anthropic API key; empty string → returns a prompt message
    commentary   : previously generated commentary text (optional — included for
                   follow-up questions that reference specific statements)

    Returns
    -------
    str — answer text
    """
    if not api_key:
        return (
            "Add your Anthropic API key in the sidebar to ask questions "
            "about your variances."
        )

    system = _build_query_system(enriched_df, cat_df, period, commentary)

    # Cap history to avoid exceeding context window
    trimmed = chat_history[-(_MAX_HISTORY_TURNS * 2):]
    messages = trimmed + [{"role": "user", "content": question}]

    client   = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model=COMMENTARY_MODEL,
        max_tokens=512,
        system=system,
        messages=messages,
    )
    return response.content[0].text


def generate_projection_narrative(
    summary: dict,
    api_key: str,
) -> str | None:
    """
    Generate a 2–3 sentence AI risk narrative for the forward projection.

    Designed as a fast, focused call (max_tokens=180) — this should feel like
    a one-line analyst note appended to the Forward Look section, not a full
    commentary block.

    Parameters
    ----------
    summary : dict from projection.projection_summary()
    api_key : Anthropic API key; returns None if empty or on API error

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
            model=_PROJECTION_MODEL,
            max_tokens=_PROJECTION_MAX_TOKENS,
            system=(
                "You are a senior FP&A analyst writing a concise risk note. "
                "Be direct. Cite specific dollar amounts. No filler phrases."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text
    except Exception:
        return None


# ── Data context builder (shared by commentary + query prompts) ────────────────

def build_data_context(
    enriched_df: pd.DataFrame,
    cat_df: pd.DataFrame,
    period: str,
) -> str:
    """
    Serialise the variance data into a structured text block.

    Used by both the commentary prompt and the NL query system prompt so the
    model always has full access to all line items, categories, and key metrics.
    """
    material = get_material_items(enriched_df)
    total_budget = enriched_df["Budget"].sum()
    total_actual = enriched_df["Actual"].sum()
    net_var      = total_actual - total_budget

    lines = [
        f"PERIOD: {period}",
        f"Total Budget:  {_d(total_budget)}",
        f"Total Actual:  {_d(total_actual)}",
        f"Net Variance:  {_d(net_var, sign=True)}  ({_p(net_var / abs(total_budget) * 100) if total_budget else 'n/a'})",
        "",
        "ALL LINE ITEMS:",
    ]
    for _, row in enriched_df.iterrows():
        direction = "FAV" if row["Favorable"] else "UNF"
        material_flag = " [MATERIAL]" if row["Material"] else ""
        lines.append(
            f"  {row['Line Item']} | {row['Category']} | "
            f"Budget {_d(row['Budget'], compact=True)} | "
            f"Actual {_d(row['Actual'], compact=True)} | "
            f"Var {_d(row['Variance ($)'], compact=True, sign=True)} ({_p(row['Variance (%)'])}) | "
            f"{direction} | {row['Severity']}{material_flag}"
        )

    lines += ["", "CATEGORY SUMMARY:"]
    for _, row in cat_df.iterrows():
        lines.append(
            f"  {row['Category']}: "
            f"Budget {_d(row['Budget ($)'], compact=True)} | "
            f"Actual {_d(row['Actual ($)'], compact=True)} | "
            f"Var {_d(row['Variance ($)'], compact=True, sign=True)} ({_p(row['Variance (%)'])}) | "
            f"Dominant: {row['Dominant Severity']}"
        )

    return "\n".join(lines)


# ── Claude helpers ────────────────────────────────────────────────────────────

def _claude_commentary(
    enriched_df: pd.DataFrame,
    cat_df: pd.DataFrame,
    period: str,
    tone: str,
    api_key: str,
) -> str:
    """Call Claude for AI-generated commentary using the tone-specific system prompt."""
    client  = anthropic.Anthropic(api_key=api_key)
    context = build_data_context(enriched_df, cat_df, period)

    message = client.messages.create(
        model=COMMENTARY_MODEL,
        max_tokens=1024,
        system=_SYSTEM_PROMPTS[tone],
        messages=[{"role": "user", "content": context}],
    )
    return message.content[0].text


def _build_query_system(
    enriched_df: pd.DataFrame,
    cat_df: pd.DataFrame,
    period: str,
    commentary: str | None,
) -> str:
    """Build the system prompt for NL variance queries, injecting full data context."""
    context = build_data_context(enriched_df, cat_df, period)
    commentary_block = (
        f"\nPREVIOUSLY GENERATED COMMENTARY:\n{commentary}\n"
        if commentary else ""
    )
    return (
        "You are a concise FP&A analyst assistant with full access to the variance data below.\n\n"
        f"{context}"
        f"{commentary_block}\n\n"
        "Answer the user's questions with these rules:\n"
        "- Be concise: 2–4 sentences for most answers; bullet points for lists.\n"
        "- Always cite specific $ and % figures when relevant.\n"
        "- Distinguish observed fact (what the data shows) from hypothesis (likely cause).\n"
        "- If the question can't be answered from this data, say so rather than speculate.\n"
        "- For 'summarise for my CFO' requests, lead with the bottom-line number then one key risk."
    )


# ── Rule-based fallback ───────────────────────────────────────────────────────

def _rule_based(
    enriched_df: pd.DataFrame,
    cat_df: pd.DataFrame,
    period: str,
    tone: str,
) -> str:
    """Route to the appropriate rule-based generator for the given tone."""
    m = _get_metrics(enriched_df, cat_df)
    if tone == "Internal Team Update":
        return _team_commentary(m, period)
    if tone == "Investor Call Prep":
        return _investor_commentary(m, period)
    return _board_commentary(m, period)   # Board Presentation is default


def _board_commentary(m: dict, period: str) -> str:
    """
    Rule-based Board Presentation commentary.

    Structure: Bottom Line → Revenue Performance → Cost Discipline →
    Investment Areas → Recommendations.
    """
    net_dir   = "favorable" if m["net_var"] >= 0 else "unfavorable"
    net_sign  = "+" if m["net_var"] >= 0 else ""
    rev_dir   = "beat" if m["rev_var"] >= 0 else "missed"
    cost_dir  = "under" if m["cost_var"] <= 0 else "over"
    cost_word = "favorable" if m["cost_var"] <= 0 else "unfavorable"

    lines = [f"{period} — Board Variance Summary", ""]

    # ── Bottom Line ───────────────────────────────────────────────────────────
    lines += [
        "BOTTOM LINE",
        f"The company ended {period} with a net variance of "
        f"{net_sign}{_d(m['net_var'], compact=True)} ({net_sign}{_p(m['net_var_pct'])}) vs. budget — "
        f"directionally {net_dir}. "
        f"Revenue {rev_dir} plan by {_d(m['rev_var'], compact=True, sign=True)} "
        f"while total costs came in {cost_dir} budget by {_d(abs(m['cost_var']), compact=True)} "
        f"({cost_word}). "
        f"There are {m['n_critical']} critical-severity variances requiring immediate attention.",
        "",
    ]

    # ── Revenue Performance ───────────────────────────────────────────────────
    rev_pct_sign = "+" if m["rev_var"] >= 0 else ""
    lines += [
        f"REVENUE PERFORMANCE  "
        f"{_d(m['rev_actual'], compact=True)} actual vs. "
        f"{_d(m['rev_budget'], compact=True)} budget "
        f"({rev_pct_sign}{_d(m['rev_var'], compact=True)}, {rev_pct_sign}{_p(m['rev_var_pct'])})",
    ]
    if len(m["rev_items"]) > 0:
        for _, row in m["rev_items"].iterrows():
            tag = "▲" if row["Favorable"] else "▼"
            lines.append(
                f"  {tag} {row['Line Item']}: "
                f"{_d(row['Variance ($)'], compact=True, sign=True)} "
                f"({_p(row['Variance (%)'])}) — {row['Severity'].lower()} variance; "
                + _rev_hypothesis(row)
            )
    else:
        lines.append("  No material revenue variances in this period.")
    lines.append(
        "  Interpretation: "
        + _rev_interpretation(m)
    )
    lines.append("")

    # ── Cost Discipline ───────────────────────────────────────────────────────
    cost_pct_sign = "+" if m["cost_var"] >= 0 else ""
    lines += [
        f"COST DISCIPLINE  "
        f"{_d(m['cost_actual'], compact=True)} actual vs. "
        f"{_d(m['cost_budget'], compact=True)} budget "
        f"({cost_pct_sign}{_d(m['cost_var'], compact=True)}, {cost_pct_sign}{_p(m['cost_var_pct'])})",
    ]
    cost_unfav = m["cost_items"][~m["cost_items"]["Favorable"]]
    cost_fav   = m["cost_items"][m["cost_items"]["Favorable"]]

    if len(cost_unfav) > 0:
        lines.append("  Overruns (unfavorable):")
        for _, row in cost_unfav.head(4).iterrows():
            lines.append(
                f"    • {row['Line Item']} ({row['Category']}): "
                f"{_d(row['Variance ($)'], compact=True, sign=True)} "
                f"({_p(row['Variance (%)'])}) — {row['Severity']}; "
                + _cost_hypothesis(row)
            )
    if len(cost_fav) > 0:
        lines.append("  Underspend (favorable):")
        for _, row in cost_fav.head(3).iterrows():
            lines.append(
                f"    • {row['Line Item']} ({row['Category']}): "
                f"{_d(row['Variance ($)'], compact=True, sign=True)} "
                f"({_p(row['Variance (%)'])}) — {row['Severity']}"
            )
    lines.append("")

    # ── Investment Areas ──────────────────────────────────────────────────────
    discretionary_keywords = ["marketing", "events", "consulting", "contractor", "cloud", "legal"]
    invest_items = m["cost_items"][
        m["cost_items"]["Line Item"].str.lower().apply(
            lambda s: any(k in s for k in discretionary_keywords)
        )
    ]
    if len(invest_items) > 0:
        lines += ["INVESTMENT AREAS"]
        for _, row in invest_items.head(4).iterrows():
            direction = "increased" if not row["Favorable"] else "decreased"
            lines.append(
                f"  • {row['Line Item']}: spend {direction} by "
                f"{_d(abs(row['Variance ($)']), compact=True)} "
                f"({_p(abs(row['Variance (%)']))}) vs. plan. "
                + _invest_note(row)
            )
        lines.append("")

    # ── Recommendations ───────────────────────────────────────────────────────
    recs = _board_recommendations(m)
    lines += ["RECOMMENDATIONS"] + [f"  {i+1}. {r}" for i, r in enumerate(recs)]
    lines += [
        "",
        f"  Forward implication: "
        + _forward_implication(m),
    ]

    return "\n".join(lines)


def _team_commentary(m: dict, period: str) -> str:
    """
    Rule-based Internal Team Update commentary.

    Structure: TL;DR → Needs Attention → Working Well → Net Position → To-Dos.
    Direct, jargon-friendly, under 300 words.
    """
    net_sign  = "+" if m["net_var"] >= 0 else ""
    tldr      = _team_tldr(m, period)

    lines = [f"{period} — Variance Brief", ""]

    # ── TL;DR ─────────────────────────────────────────────────────────────────
    lines += [f"TL;DR: {tldr}", ""]

    # ── Needs Attention ───────────────────────────────────────────────────────
    lines.append("NEEDS ATTENTION")
    unfav = m["unfav_items"].head(6)
    if len(unfav) > 0:
        for _, row in unfav.iterrows():
            owner = _functional_owner(row["Category"])
            lines.append(
                f"  🔴 {row['Line Item']} "
                f"[{_d(row['Variance ($)'], compact=True, sign=True)}, {_p(row['Variance (%)'])}]: "
                + _cost_hypothesis(row) if not _is_rev(row["Category"])
                else _rev_hypothesis(row)
                + f" → Owner: {owner}"
            )
    else:
        lines.append("  No material unfavorable variances. Nice work.")
    lines.append("")

    # ── Working Well ──────────────────────────────────────────────────────────
    lines.append("WORKING WELL")
    fav = m["fav_items"].head(4)
    if len(fav) > 0:
        for _, row in fav.iterrows():
            lines.append(
                f"  🟢 {row['Line Item']}: "
                f"{_d(row['Variance ($)'], compact=True, sign=True)} ({_p(row['Variance (%)'])}) vs. plan"
            )
    else:
        lines.append("  No material favorable variances to highlight.")
    lines.append("")

    # ── Net Position ──────────────────────────────────────────────────────────
    net_word = "ahead of" if m["net_var"] >= 0 else "behind"
    lines += [
        "NET POSITION",
        f"  {period} closed {net_word} budget by "
        f"{_d(abs(m['net_var']), compact=True)} ({_p(abs(m['net_var_pct']))}) "
        f"on a {_d(m['total_budget'], compact=True)} base — "
        f"{m['n_critical']} critical items open.",
        "",
    ]

    # ── To-Dos ────────────────────────────────────────────────────────────────
    lines.append("TO-DOs")
    todos = _team_todos(m)
    lines += [f"  ☐ {t}" for t in todos]

    return "\n".join(lines)


def _investor_commentary(m: dict, period: str) -> str:
    """
    Rule-based Investor Call Prep commentary.

    Structure: Header → Key Messages → Revenue Narrative → Cost Narrative →
    Beat/Miss Summary → Anticipated Q&A → Language Guidance.
    """
    net_vs_plan = "above" if m["net_var"] >= 0 else "below"
    rev_vs_plan = "exceeded" if m["rev_var"] >= 0 else "came in below"

    lines = [
        f"{period} INVESTOR CALL PREPARATION — DRAFT",
        "[INTERNAL USE ONLY — NOT FOR DISTRIBUTION]",
        "",
    ]

    # ── Key Messages ──────────────────────────────────────────────────────────
    key_msgs = _investor_key_messages(m, period, net_vs_plan, rev_vs_plan)
    lines += ["KEY MESSAGES"] + [f"  • {msg}" for msg in key_msgs] + [""]

    # ── Revenue Narrative ─────────────────────────────────────────────────────
    rev_pct_sign = "+" if m["rev_var"] >= 0 else ""
    lines += [
        "REVENUE NARRATIVE",
        f"  Revenue {rev_vs_plan} our plan by "
        f"{_d(abs(m['rev_var']), compact=True)} ({rev_pct_sign}{_p(m['rev_var_pct'])}).",
    ]
    for _, row in m["rev_items"].iterrows():
        beat_miss = "beat" if row["Favorable"] else "missed"
        lines.append(
            f"  • {row['Line Item']} {beat_miss} plan by "
            f"{_d(abs(row['Variance ($)']), compact=True)} ({_p(abs(row['Variance (%)']))})."
            + (" We believe this reflects stronger-than-anticipated demand in the period."
               if row["Favorable"] else
               " We believe this reflects timing of contract closures and ongoing market dynamics.")
        )
    lines.append(
        "  ⚠️  FLS note: Any forward-looking statements regarding revenue recovery or "
        "acceleration should reference specific pipeline indicators, not this variance alone."
    )
    lines.append("")

    # ── Cost Narrative ────────────────────────────────────────────────────────
    cost_dir = "discipline" if m["cost_var"] <= 0 else "pressure"
    lines += [
        f"COST NARRATIVE  (framing: {cost_dir})",
        f"  Total costs came in {'under' if m['cost_var'] <= 0 else 'over'} plan by "
        f"{_d(abs(m['cost_var']), compact=True)} ({_p(abs(m['cost_var_pct']))}).",
    ]
    for _, row in m["cost_items"].head(5).iterrows():
        if row["Favorable"]:
            framing = "reflects our continued focus on cost efficiency"
        else:
            framing = "represents a planned investment in capacity" if row["Severity"] in ("Moderate", "Minor") \
                      else "is being actively reviewed with functional leadership"
        lines.append(
            f"  • {row['Line Item']}: {_d(row['Variance ($)'], compact=True, sign=True)} "
            f"({_p(row['Variance (%)'])}) — {framing}."
        )
    lines.append("")

    # ── Beat / Miss Summary ───────────────────────────────────────────────────
    lines.append("BEAT / MISS SUMMARY")
    lines.append(f"  {'Line Item':<38} {'Var ($)':>10}  {'Var (%)':>9}  {'Direction'}")
    lines.append(f"  {'-'*38} {'-'*10}  {'-'*9}  {'-'*12}")
    for _, row in get_material_items(m["_enriched"]).iterrows():
        tag = "Beat" if row["Favorable"] else "Miss"
        lines.append(
            f"  {row['Line Item']:<38} "
            f"{_d(row['Variance ($)'], compact=True):>10}  "
            f"{_p(row['Variance (%)']):>9}  {tag}"
        )
    lines.append("")

    # ── Anticipated Q&A ───────────────────────────────────────────────────────
    lines += ["ANTICIPATED Q&A"] + _investor_qa(m) + [""]

    # ── Language Guidance ─────────────────────────────────────────────────────
    lines += [
        "LANGUAGE GUIDANCE",
        "  Use:",
        '    • "Our plan assumed…" (not "We expected…")',
        '    • "We believe the driver is…" (acknowledge, then pivot)',
        '    • "We are actively managing this through…"',
        "  Avoid:",
        '    • "We missed guidance" — say "came in below our plan"',
        '    • Specific forward targets not already in guidance',
        '    • Attributing variance to macroeconomic factors without data',
        "  Forward-looking statement caution: Any statements about future performance "
        "should be qualified with 'subject to market conditions and execution'.",
    ]

    return "\n".join(lines)


# ── Rule-based content generators ────────────────────────────────────────────

def _rev_hypothesis(row: pd.Series) -> str:
    """Return a brief root-cause hypothesis for a revenue line item."""
    name = row["Line Item"].lower()
    if "product" in name or "license" in name:
        return "likely deal timing or pricing pressure in the period." if not row["Favorable"] \
               else "deal pull-forward or stronger close rates likely driver."
    if "service" in name or "professional" in name:
        return "capacity constraints or project delays may have limited delivery." if not row["Favorable"] \
               else "demand exceeded headcount plan; utilisation likely above target."
    if "maintenance" in name or "support" in name or "subscription" in name:
        return "churn or downsell in the installed base." if not row["Favorable"] \
               else "strong renewal rates or upsell in recurring base."
    return ("demand shortfall or competitive pressure." if not row["Favorable"]
            else "demand outpaced plan.")


def _cost_hypothesis(row: pd.Series) -> str:
    """Return a brief root-cause hypothesis for a cost line item."""
    name = row["Line Item"].lower()
    if "cloud" in name or "infrastructure" in name or "hosting" in name:
        return "usage scaling ahead of plan; review committed spend vs. on-demand mix."
    if "marketing" in name or "paid" in name or "digital" in name:
        return "campaign spend accelerated; validate attribution vs. pipeline impact."
    if "event" in name or "trade show" in name:
        return ("events cancelled or deferred; budget not redeployed." if not row["Favorable"]
                else "events rescheduled; one-time saving, not structural.")
    if "contractor" in name or "consulting" in name:
        return ("headcount gap filled with contractors above plan." if not row["Favorable"]
                else "project delays or lower headcount gap reduced contractor dependency.")
    if "legal" in name or "compliance" in name:
        return "elevated legal activity — M&A, regulatory, or litigation likely."
    if "salary" in name or "benefits" in name or "headcount" in name:
        return ("hiring ahead of plan or above-plan compensation adjustments." if not row["Favorable"]
                else "open roles or delayed hiring reduced payroll spend.")
    return ("spend ran above plan; investigate run-rate vs. one-time nature." if not row["Favorable"]
            else "spend came in below plan.")


def _invest_note(row: pd.Series) -> str:
    """Return an investment framing note for discretionary spend items."""
    if row["Favorable"]:
        return "Budget not deployed — assess whether underspend reflects timing or permanent reduction."
    sev = row["Severity"]
    if sev == "Critical":
        return "Investigate root cause; determine whether run-rate or one-time before adjusting forecast."
    return "Evaluate ROI; if investment-driven, update forecast assumptions accordingly."


def _rev_interpretation(m: dict) -> str:
    if m["rev_var"] >= 0 and abs(m["rev_var_pct"]) < 3:
        return "Revenue performance is broadly on-plan with mixed composition — monitor product mix trend."
    if m["rev_var"] < 0:
        return (
            f"Revenue shortfall of {_d(abs(m['rev_var']), compact=True)} ({_p(abs(m['rev_var_pct']))}) "
            "requires investigation into pipeline health and whether this is a timing or structural issue."
        )
    return (
        f"Revenue beat of {_d(m['rev_var'], compact=True)} ({_p(m['rev_var_pct'])}) "
        "reflects above-plan demand — assess capacity and whether the run-rate is sustainable."
    )


def _board_recommendations(m: dict) -> list[str]:
    """Generate three specific board-level recommendations from the variance data."""
    recs = []
    # Recommendation driven by the top unfavorable item
    if len(m["unfav_items"]) > 0:
        top = m["unfav_items"].iloc[0]
        recs.append(
            f"Investigate {top['Line Item']} variance ({_d(top['Variance ($)'], compact=True, sign=True)}): "
            f"determine run-rate vs. one-time before incorporating into Q+1 forecast."
        )
    # Second recommendation driven by revenue performance
    if m["rev_var"] < 0:
        recs.append(
            f"Commission pipeline review to assess whether {_d(abs(m['rev_var']), compact=True)} "
            f"revenue shortfall is a timing issue or requires full-year guidance revision."
        )
    elif m["rev_var"] > 0 and len(m["rev_items"]) > 1:
        recs.append(
            "Assess sustainability of revenue beat: identify whether outperformance is "
            "pull-forward demand or genuine acceleration, and update capacity plan accordingly."
        )
    else:
        recs.append(
            "Review revenue mix: ensure product/services balance is on track with "
            "full-year targets and adjust Q+1 resource allocation if needed."
        )
    # Third: cost or forward-looking
    cost_overruns = m["cost_items"][~m["cost_items"]["Favorable"]]
    if len(cost_overruns) >= 2:
        top_cost = cost_overruns.iloc[0]
        recs.append(
            f"Establish monthly review of {top_cost['Category']} spend; "
            f"{_d(top_cost['Variance ($)'], compact=True, sign=True)} overrun in "
            f"{top_cost['Line Item']} suggests forecast model needs recalibration."
        )
    else:
        recs.append(
            "Present updated full-year forecast at next board meeting incorporating "
            "observed run-rate changes; flag any items where Q1 assumptions have shifted."
        )
    return recs[:3]


def _forward_implication(m: dict) -> str:
    if m["n_critical"] >= 3:
        return (
            f"With {m['n_critical']} critical variances, Q+1 forecasting assumptions "
            "should be revisited before being locked — particularly on revenue mix and the two largest cost overruns."
        )
    if m["rev_var"] < 0:
        return (
            "Revenue shortfall creates a headwind into next quarter unless pipeline "
            "recovery is confirmed; hold cost assumptions flat until revenue trajectory clarifies."
        )
    return (
        "Performance is broadly manageable; prioritise resolving the top unfavorable "
        "variances before they compound into Q1."
    )


def _team_tldr(m: dict, period: str) -> str:
    worst = m["unfav_items"].iloc[0] if len(m["unfav_items"]) > 0 else None
    net_word = "favorable" if m["net_var"] >= 0 else "unfavorable"
    tldr = f"{period} closed {net_word} by {_d(abs(m['net_var']), compact=True)}"
    if worst is not None:
        tldr += (
            f"; biggest issue is {worst['Line Item']} at "
            f"{_d(worst['Variance ($)'], compact=True, sign=True)} ({worst['Severity']})."
        )
    return tldr


def _team_todos(m: dict) -> list[str]:
    """Generate 3-5 actionable to-do items from the variance data."""
    todos = []
    for _, row in m["unfav_items"].head(3).iterrows():
        owner = _functional_owner(row["Category"])
        todos.append(
            f"[{owner}] Investigate {row['Line Item']} overrun "
            f"({_d(row['Variance ($)'], compact=True, sign=True)}): determine if run-rate or one-time"
        )
    if m["rev_var"] < 0:
        todos.append(
            f"[Finance + Sales] Quantify {_d(abs(m['rev_var']), compact=True)} revenue shortfall — "
            "timing vs. structural; update Q+1 forecast assumption"
        )
    todos.append(
        "[Finance] Refresh full-quarter forecast with actuals; flag any line where "
        f"budget assumption needs revision before next review"
    )
    return todos[:5]


def _investor_key_messages(m, period, net_vs_plan, rev_vs_plan) -> list[str]:
    msgs = []
    msgs.append(
        f"We {rev_vs_plan} our revenue plan for {period} by "
        f"{_d(abs(m['rev_var']), compact=True)} ({_p(abs(m['rev_var_pct']))}) — "
        + ("driven by stronger-than-planned services demand."
           if m["rev_var"] >= 0 else
           "reflecting deal timing; our pipeline remains healthy.")
    )
    if m["cost_var"] > 0:
        msgs.append(
            f"Cost investment exceeded plan by {_d(abs(m['cost_var']), compact=True)} "
            f"({_p(abs(m['cost_var_pct']))}), primarily in growth-oriented areas; "
            "we are actively reviewing run-rate sustainability."
        )
    else:
        msgs.append(
            f"We maintained cost discipline, coming in {_d(abs(m['cost_var']), compact=True)} "
            f"({_p(abs(m['cost_var_pct']))}) under plan, while preserving key investments."
        )
    msgs.append(
        f"We are {'on track with' if abs(m['net_var_pct']) < 5 else 'reviewing'} our "
        f"full-year outlook; {m['n_critical']} item(s) require resolution before next guidance update."
    )
    return msgs


def _investor_qa(m: dict) -> list[str]:
    """Generate 2-3 anticipated investor questions with suggested responses."""
    qa = []
    if m["rev_var"] < 0:
        qa += [
            f"  Q: You missed revenue by {_d(abs(m['rev_var']), compact=True)} — "
            "is this a demand problem or an execution problem?",
            "  A: We believe this reflects specific deal timing rather than a structural demand shift. "
            "Our pipeline entering the next quarter is [X]% above prior year on a comparable basis. "
            "We are not changing our full-year guidance at this time, subject to execution.",
            "",
        ]
    if m["cost_var"] > 0:
        top_cost = m["cost_items"][~m["cost_items"]["Favorable"]].iloc[0] \
                   if len(m["cost_items"][~m["cost_items"]["Favorable"]]) > 0 else None
        if top_cost is not None:
            qa += [
                f"  Q: {top_cost['Line Item']} ran {_d(top_cost['Variance ($)'], compact=True, sign=True)} "
                "over plan — is this a recurring issue?",
                f"  A: We believe approximately [X]% of this overrun is one-time in nature, "
                "related to [specific driver]. The remaining portion represents a revised run-rate "
                "which is reflected in our updated cost forecast.",
                "",
            ]
    qa += [
        "  Q: What gives you confidence in the full-year forecast?",
        "  A: Our underlying business fundamentals remain intact. We are focused on "
        "[top 2 operational priorities]. Any guidance revision would be communicated "
        "with at least [X] weeks notice.",
    ]
    return qa


def _functional_owner(category: str) -> str:
    """Map a Category string to a likely functional owner for team commentary."""
    cat_lower = category.lower()
    if any(k in cat_lower for k in ["revenue", "sales", "income"]):
        return "Sales / RevOps"
    if "r&d" in cat_lower or "engineering" in cat_lower or "product" in cat_lower:
        return "Engineering / Product"
    if "marketing" in cat_lower:
        return "Marketing"
    if "g&a" in cat_lower or "finance" in cat_lower or "legal" in cat_lower:
        return "Finance / Legal"
    if "cost of revenue" in cat_lower or "cogs" in cat_lower:
        return "Operations / Delivery"
    return "Finance (classify)"


# ── Metrics extractor ─────────────────────────────────────────────────────────

def _get_metrics(enriched_df: pd.DataFrame, cat_df: pd.DataFrame) -> dict:
    """
    Compute all aggregate metrics needed by the rule-based commentary generators.

    Returns a flat dict so each generator can access values by key without
    repeating the filtering logic.
    """
    rev_mask  = enriched_df["Category"].apply(_is_rev)
    cost_mask = ~rev_mask

    rev_df    = enriched_df[rev_mask]
    cost_df   = enriched_df[cost_mask]

    material  = enriched_df[enriched_df["Material"]]

    # Revenue / cost aggregates
    total_budget = enriched_df["Budget"].sum()
    total_actual = enriched_df["Actual"].sum()
    net_var      = total_actual - total_budget

    rev_budget = rev_df["Budget"].sum()
    rev_actual = rev_df["Actual"].sum()
    rev_var    = rev_actual - rev_budget

    cost_budget = cost_df["Budget"].sum()
    cost_actual = cost_df["Actual"].sum()
    cost_var    = cost_actual - cost_budget   # positive = over budget = unfavorable

    # Material items split by direction
    unfav_items = (
        material[~material["Favorable"]]
        .assign(_abs=lambda d: d["Variance ($)"].abs())
        .sort_values("_abs", ascending=False)
        .drop(columns=["_abs"])
    )
    fav_items = (
        material[material["Favorable"]]
        .assign(_abs=lambda d: d["Variance ($)"].abs())
        .sort_values("_abs", ascending=False)
        .drop(columns=["_abs"])
    )

    rev_items  = material[material["Category"].apply(_is_rev)].copy()
    cost_items = material[~material["Category"].apply(_is_rev)].copy()
    # Re-sort cost items: unfavorable first, then favorable, within each group by |var$|
    cost_items = pd.concat([
        cost_items[~cost_items["Favorable"]].assign(_abs=lambda d: d["Variance ($)"].abs())
            .sort_values("_abs", ascending=False).drop(columns=["_abs"]),
        cost_items[cost_items["Favorable"]].assign(_abs=lambda d: d["Variance ($)"].abs())
            .sort_values("_abs", ascending=False).drop(columns=["_abs"]),
    ])

    critical = material[material["Severity"] == "Critical"]

    return {
        "total_budget":  total_budget,
        "total_actual":  total_actual,
        "net_var":       net_var,
        "net_var_pct":   net_var / abs(total_budget) * 100 if total_budget else 0,
        "rev_budget":    rev_budget,
        "rev_actual":    rev_actual,
        "rev_var":       rev_var,
        "rev_var_pct":   rev_var / abs(rev_budget) * 100 if rev_budget else 0,
        "cost_budget":   cost_budget,
        "cost_actual":   cost_actual,
        "cost_var":      cost_var,
        "cost_var_pct":  cost_var / abs(cost_budget) * 100 if cost_budget else 0,
        "rev_items":     rev_items,
        "cost_items":    cost_items,
        "unfav_items":   unfav_items,
        "fav_items":     fav_items,
        "critical":      critical,
        "n_material":    len(material),
        "n_unfav":       len(unfav_items),
        "n_fav":         len(fav_items),
        "n_critical":    len(critical),
        "cat_df":        cat_df,
        "_enriched":     enriched_df,   # kept for beat/miss table in investor commentary
    }


def _is_rev(category: str) -> bool:
    """Return True if *category* follows the revenue sign convention."""
    cat_lower = str(category).lower()
    return any(kw in cat_lower for kw in REVENUE_CATEGORY_KEYWORDS)


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


def _p(value: float) -> str:
    """Format a float as a signed percentage string."""
    if pd.isna(value):
        return "—"
    return f"{value:+.1f}%"
