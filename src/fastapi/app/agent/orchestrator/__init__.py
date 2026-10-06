"""Query-path entry point and system-prompt selection.

``run_deterministic_rag`` is the single entry the queries router (and the eval
harness) call. It parses the request-scoped conversation history, serves or
fills the short-TTL exact-match Redis response cache, and dispatches to the
agentic-retrieval LangGraph (``app.agent.agentic_retrieval``), which does the
classification, tool dispatch, LLM synthesis and the six hallucination guards.
The legacy hand-rolled tool-dispatch body that gave this module its name was
deleted 2026-08-04; the name stays because routers and tests import it.

This module also owns the system-prompt text (DEFAULT / NUMERIC / NARRATIVE,
dash and colon citation forms) and ``_select_system_prompt``, and re-exports a
few helpers that tests import from here.
"""

import contextlib
import contextvars
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.agent.deps import AgentDeps
from app.agent.prompts.orchestrator_shared_preamble_colon import (
    SYSTEM_PROMPT as _SHARED_PREAMBLE_COLON,
)
from app.agent.prompts.orchestrator_shared_preamble_dash import (
    SYSTEM_PROMPT as _SHARED_PREAMBLE_DASH,
)
from app.config import settings
from app.models.rag import GeoRAGResponse

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# P1 #14 — Global per-query LLM-call cap.
# ---------------------------------------------------------------------------
# A single user query can invoke the LLM multiple times: classifier
# escalation, query rephrasing, primary synthesis, retry-on-validation-fail,
# one-shot failover, follow-ups generation. The contextvar lets us count
# every call without plumbing a counter through every helper signature.
# `run_deterministic_rag` resets the counter at the start of every run.
# `_call_llm` increments and enforces the cap.

# ---------------------------------------------------------------------------
# Phase F.12 — LLM-call machinery extracted to app/agent/llm_calls.py.
# The counter, the budget exception, the OpenAI-compat + Anthropic
# wire-format callers, and the dispatch helper all live there now. We
# re-export every name below so external callers that import
#   `from app.agent.orchestrator import _llm_call_counter`
#   `from app.agent.orchestrator import _call_llm` (etc.)
# keep working unchanged. See docs/master_plan_orchestrator_refactor.md.
# ---------------------------------------------------------------------------
# LLM helpers remain re-exported for existing live callers and tests.
from app.agent.llm_calls import (  # noqa: E402, F401
    LLMCallBudgetExceeded,
    _call_anthropic_llm,
    _call_llm,
    _call_openai_compatible_llm,
    _llm_call_counter,
)

# System-prompt text extracted to a module constant so it can be sent as the
# cacheable block when LLM_BACKEND=anthropic (Anthropic prompt caching requires
# stable, large, identical prefixes across requests). See _call_anthropic_llm.
#
# If you edit this, increment _SYSTEM_PROMPT_VERSION so the cache key differs
# from any in-flight cached entries on the Anthropic side.
# v4 — P1 #18 added GRAPH variant; P1 #19 diversified few-shots and added
#       refusal examples to every variant.
# v5 — P1 wave-4 follow-up: added RULE 10 (impossible-premise refusal) to
#       the shared preamble so smaller models (qwen2.5:14b) get explicit
#       guidance, not just few-shot patterns. Also extended _is_refusal
#       in response_assembler.py with the corresponding refusal phrases.
# v6 — 2026-04-21 Module 5 Phase B PROMPT-01 fix: tightened citation
#       discipline in DEFAULT and NUMERIC variants from "at least one
#       citation per response" to "every factual claim must carry a
#       citation marker". This is a Global Invariant 1 compliance fix
#       (hallucination prevention Layer 2). RETRIEVAL_STRATEGY_VERSION
#       bumped to v2.1 in query_classifier.py to bust any cached
#       retrieval contexts that predate the prompt change.
# v7 — 2026-04-21 Module 5 Chunk 2 (model flip to qwen3:30b-a3b MoE).
#       New model may produce different response shapes even with unchanged
#       prompt text; bumping invalidates Anthropic prompt caches and
#       downstream version-keyed caches. Paired with RETRIEVAL_STRATEGY_VERSION
#       bump to v3-qwen3-moe-2026-04-21 in query_classifier.py.
#       Also adds enable_thinking param to _call_openai_compatible_llm
#       (forward-looking Qwen3 thinking-mode discipline).
# v8 — 2026-04-21 TOOL-CALL-01 fix
#       Grounded synthesis now disables thinking (saves 1000-2000 tokens per call).
#       Empty-content guard returns structured fallback instead of silent empty.
#       Context raised to 16K. Cache invalidation intentional.
# v9 — 2026-04-22 Module 6 Phase B Chunk 3
#       Citation span resolver (CITATION_SPAN_RESOLVER_ENABLED=true), colon-form
#       markers, four §04i guards (numeric tightened, entity expanded, completeness
#       new, refusal meta-guard new).  Cache invalidation required: prompts changed
#       + guards now reject on failure.  Paired with CITATION_SPAN_RESOLVER_ENABLED
#       flag flip in .env.  response.text ← normalized_text (C1 close-out).
#       Items+spans now write in a single transaction (C3 close-out).
# v11 - 2026-08-21 audit remediation.
#       (a) The shared preamble now declares the CONTEXT section untrusted,
#       not just the USER QUESTION. Retrieved passages are third-party
#       document text; the fence markers around them (see
#       PROMPT_INJECTION_DELIMITING_ENABLED, on in production since
#       2026-08-21) mark WHERE that text is, but nothing told the model
#       what to do about it.
#       (b) The preamble is no longer defined here. It comes from
#       app/agent/prompts/orchestrator_shared_preamble_{dash,colon}.py,
#       which used to be self-declared mirrors and had drifted on rule 5.
#       Prompt text changed -> the Anthropic cache key must change.
_SYSTEM_PROMPT_VERSION = 11

# C5 — system prompts split by query shape. The shared preamble (role +
# security rules + citation rules) is identical across variants so the
# Anthropic cache-control block stays stable and cache-friendly. Only the
# EXAMPLES and task-specific guidance differ.
#
# Variants:
#   DEFAULT   — safe fallback; used when the classifier output doesn't
#               clearly prefer a variant. Mixed-mode answers.
#   NUMERIC   — emphasises "quote verbatim from HIGH-CONFIDENCE SUMMARIES"
#               for count/aggregate/metadata queries.
#   NARRATIVE — emphasises citation discipline and paraphrase fidelity
#               for document-heavy / PGEO queries.

_SYSTEM_PROMPT_SHARED_PREAMBLE = _SHARED_PREAMBLE_DASH

_SYSTEM_PROMPT_DEFAULT = _SYSTEM_PROMPT_SHARED_PREAMBLE + """
TASK PROFILE: general geological query (mixed-mode answers).
Every factual sentence in your answer must carry at least one inline citation marker. \
Do not make unsupported factual claims. When the Evidence Set provides data, cite it \
on the specific sentence that uses it — not only at the end of the answer.

EXAMPLES:
Q: "How many drill holes are in this project?"
A: "There are 20 drill holes in this project [DATA-1]."

Q: "What is the deepest hole?"
A: "PLS-22-08 has the deepest total depth at 510 metres [DATA-1]."

Q: "What deposit does this project host?"
A: "The project hosts the Triple R deposit, a classic unconformity-related uranium deposit [NI43-1]."

Q: "Which holes intersected uranium mineralisation above 1% U3O8?"
A: "PLS-22-08 and PLS-22-12 each intersected uranium grades above 1% U3O8, with peak \
assays of 4.3% and 2.1% U3O8 respectively [DATA-1]."

Q: "What's the weather in Toronto today?"
A: "I can only answer geological questions about this project's exploration data."

If retrieval returned no passages, or the passages are genuinely unrelated to \
the user's question, do NOT respond with a canned refusal. Instead: (a) briefly \
list what topics the retrieved passages DO cover (e.g. "I found passages \
about Rowan QA/QC, Madsen PFS resources, and Dixie historic drilling, but \
nothing specifically about X"), and (b) ask the user to clarify or rephrase. \
Give the user something actionable, not a dead end.
"""

_SYSTEM_PROMPT_NUMERIC = _SYSTEM_PROMPT_SHARED_PREAMBLE + """
TASK PROFILE: numerical / factoid.
The user is asking for a count, aggregate, min/max, or specific numeric attribute.
Your answer must:
  - Quote the HIGH-CONFIDENCE SUMMARIES block verbatim. Do NOT do arithmetic yourself.
  - Lead with the number. Keep the sentence short and direct.
  - Cite EVERY numeric claim with [DATA-X] or [NI43-X] on the same sentence. \
Every factual claim — not just the first — must carry an inline citation marker.
  - If the summaries block is absent BUT narrative passages discuss the topic \
(e.g. NI 43-101 text describes the figure or value in prose), summarise the \
narrative answer with citations. Only emit a clarification request (not a \
canned refusal) if no passages are relevant.
  - If the user's question contains a physically impossible numeric premise \
(e.g. "uranium grade above 500%", "drill hole depth above 50,000 m", \
"ages above 5 billion years"), refuse and explain the unit/range that the \
data actually uses — do NOT try to convert or accommodate the impossible value.

EXAMPLES:
Q: "How many drill holes are in this project?"
A: "This project has 20 drill holes [DATA-1]."

Q: "What is the deepest hole and how deep?"
A: "PLS-22-08 is the deepest at 510 metres total depth [DATA-1]."

Q: "What is the mean gold grade?"
A: "The mean gold grade across 348 samples is 1.27 g/t [DATA-1]."

Q: "How many DD vs RC holes were drilled in 2023?"
A: "In 2023 the program comprised 12 DD holes and 8 RC holes for 20 total [DATA-1]."

Q: "What's the project's mean depth-to-mineralisation?"
A: "I don't have a mineralisation-depth aggregate in this project's summary."

Q: "Which hole has uranium grade above 500 percent?"
A: "No hole can have a uranium grade above 500 percent — that's not a possible \
value. Uranium grades in this project are reported in ppm (parts per million) \
in the U3O8_ppm column; the highest assay is 52,000 ppm (5.2 percent) [DATA-1]. \
If you meant 5 percent or 50,000 ppm, the answer is PLS-22-08."

Q: "What holes were drilled deeper than 50000 metres?"
A: "No drill holes go that deep — 50,000 m is well beyond physical drilling \
limits and the deepest hole in this project is 510 m [DATA-1]."

Q: "Tell me a joke."
A: "I can only answer geological questions about this project's exploration data."

If retrieval returned no passages, or the passages are genuinely unrelated to \
the user's question, do NOT respond with a canned refusal. Instead: (a) briefly \
list what topics the retrieved passages DO cover (e.g. "I found passages \
about Rowan QA/QC, Madsen PFS resources, and Dixie historic drilling, but \
nothing specifically about X"), and (b) ask the user to clarify or rephrase. \
Give the user something actionable, not a dead end.
"""

_SYSTEM_PROMPT_NARRATIVE = _SYSTEM_PROMPT_SHARED_PREAMBLE + """
TASK PROFILE: document-anchored narrative.
The user is asking for an interpretation, description, or synthesis drawn from \
NI 43-101 reports, published literature, or Public Geoscience records. Your answer must:
  - Synthesize across the provided document chunks — do not just quote one chunk.
  - Cite every factual claim, including paraphrases. When in doubt, cite.
  - Prefer document citations ([NI43-X], [PUB-X], [PGEO-X]) over database ones \
for interpretive claims.
  - Keep the tone technical but readable. Define jargon on first use if the \
query implies a less-technical reader.

EXAMPLES:
Q: "What deposit does this project host?"
A: "The project hosts the Triple R deposit, a classic unconformity-related uranium deposit [NI43-1]. \
Mineralisation sits at the contact between Athabasca Group sandstones and the underlying basement \
pelitic gneisses [NI43-1], with grade control exerted by post-Athabasca reactivated faults [PUB-1]."

Q: "What is the published uranium grade range in Saskatchewan Athabasca deposits?"
A: "Saskatchewan Athabasca unconformity deposits typically range from 0.5 to over 18 percent U3O8 \
[PGEO-1], with the highest grades concentrated at the sandstone-basement unconformity [PGEO-2]."

Q: "What's the structural setting of the deposit?"
A: "The deposit lies along the reactivated Patterson Lake corridor, a NE-trending shear zone \
that offsets the sandstone-basement unconformity by roughly 50 m [NI43-1]. Late brittle faults \
control fluid pathways and concentrate uranium mineralisation in the basement graphitic units \
[NI43-2], a setting analogous to the McArthur River deposit [PUB-1]."

Q: "Summarise the QP-signed conclusions on resource potential."
A: "I don't have report sections discussing resource-potential conclusions for this project."

Q: "What are your political views?"
A: "I can only answer geological questions about this project's exploration data."

If retrieval returned no passages, or the passages are genuinely unrelated to \
the user's question, do NOT respond with a canned refusal. Instead: (a) briefly \
list what topics the retrieved passages DO cover (e.g. "I found passages \
about Rowan QA/QC, Madsen PFS resources, and Dixie historic drilling, but \
nothing specifically about X"), and (b) ask the user to clarify or rephrase. \
Give the user something actionable, not a dead end.
"""

# ---------------------------------------------------------------------------
# Module 6 Phase B Chunk 2 — Colon-form prompt variants (DRAFT, flag-gated)
#
# These are activated ONLY when settings.CITATION_SPAN_RESOLVER_ENABLED=True.
# The sole difference from the dash-form variants above is the citation marker
# format in RULES FOR CITATIONS (rules 6–9) and in the EXAMPLES.
#
# _SYSTEM_PROMPT_VERSION is NOT bumped here — that bump happens in the apply
# dispatch after senior-reviewer approval, per Chunk 2 scope constraints.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_SHARED_PREAMBLE_COLON = _SHARED_PREAMBLE_COLON

_SYSTEM_PROMPT_DEFAULT_COLON = _SYSTEM_PROMPT_SHARED_PREAMBLE_COLON + """
TASK PROFILE: general geological query (mixed-mode answers).
Every factual sentence in your answer must carry at least one inline citation marker. \
Do not make unsupported factual claims. When the Evidence Set provides data, cite it \
on the specific sentence that uses it — not only at the end of the answer.

EXAMPLES:
Q: "How many drill holes are in this project?"
A: "There are 20 drill holes in this project [DATA:1]."

Q: "What is the deepest hole?"
A: "PLS-22-08 has the deepest total depth at 510 metres [DATA:1]."

Q: "What deposit does this project host?"
A: "The project hosts the Triple R deposit, a classic unconformity-related uranium deposit [NI43:1]."

Q: "Which holes intersected uranium mineralisation above 1% U3O8?"
A: "PLS-22-08 and PLS-22-12 each intersected uranium grades above 1% U3O8, with peak \
assays of 4.3% and 2.1% U3O8 respectively [DATA:1]."

Q: "What's the weather in Toronto today?"
A: "I can only answer geological questions about this project's exploration data."

If retrieval returned no passages, or the passages are genuinely unrelated to \
the user's question, do NOT respond with a canned refusal. Instead: (a) briefly \
list what topics the retrieved passages DO cover (e.g. "I found passages \
about Rowan QA/QC, Madsen PFS resources, and Dixie historic drilling, but \
nothing specifically about X"), and (b) ask the user to clarify or rephrase. \
Give the user something actionable, not a dead end.
"""

_SYSTEM_PROMPT_NUMERIC_COLON = _SYSTEM_PROMPT_SHARED_PREAMBLE_COLON + """
TASK PROFILE: numerical / factoid.
The user is asking for a count, aggregate, min/max, or specific numeric attribute.
Your answer must:
  - Quote the HIGH-CONFIDENCE SUMMARIES block verbatim. Do NOT do arithmetic yourself.
  - Lead with the number. Keep the sentence short and direct.
  - Cite EVERY numeric claim with [DATA:X] or [NI43:X] on the same sentence. \
Every factual claim — not just the first — must carry an inline citation marker.
  - If the summaries block is absent BUT narrative passages discuss the topic \
(e.g. NI 43-101 text describes the figure or value in prose), summarise the \
narrative answer with citations. Only emit a clarification request (not a \
canned refusal) if no passages are relevant.
  - If the user's question contains a physically impossible numeric premise \
(e.g. "uranium grade above 500%", "drill hole depth above 50,000 m", \
"ages above 5 billion years"), refuse and explain the unit/range that the \
data actually uses — do NOT try to convert or accommodate the impossible value.

EXAMPLES:
Q: "How many drill holes are in this project?"
A: "This project has 20 drill holes [DATA:1]."

Q: "What is the deepest hole and how deep?"
A: "PLS-22-08 is the deepest at 510 metres total depth [DATA:1]."

Q: "What is the mean gold grade?"
A: "The mean gold grade across 348 samples is 1.27 g/t [DATA:1]."

Q: "How many DD vs RC holes were drilled in 2023?"
A: "In 2023 the program comprised 12 DD holes and 8 RC holes for 20 total [DATA:1]."

Q: "What's the project's mean depth-to-mineralisation?"
A: "I don't have a mineralisation-depth aggregate in this project's summary."

Q: "Which hole has uranium grade above 500 percent?"
A: "No hole can have a uranium grade above 500 percent — that's not a possible \
value. Uranium grades in this project are reported in ppm (parts per million) \
in the U3O8_ppm column; the highest assay is 52,000 ppm (5.2 percent) [DATA:1]. \
If you meant 5 percent or 50,000 ppm, the answer is PLS-22-08."

Q: "What holes were drilled deeper than 50000 metres?"
A: "No drill holes go that deep — 50,000 m is well beyond physical drilling \
limits and the deepest hole in this project is 510 m [DATA:1]."

Q: "Tell me a joke."
A: "I can only answer geological questions about this project's exploration data."

If retrieval returned no passages, or the passages are genuinely unrelated to \
the user's question, do NOT respond with a canned refusal. Instead: (a) briefly \
list what topics the retrieved passages DO cover (e.g. "I found passages \
about Rowan QA/QC, Madsen PFS resources, and Dixie historic drilling, but \
nothing specifically about X"), and (b) ask the user to clarify or rephrase. \
Give the user something actionable, not a dead end.
"""

_SYSTEM_PROMPT_NARRATIVE_COLON = _SYSTEM_PROMPT_SHARED_PREAMBLE_COLON + """
TASK PROFILE: document-anchored narrative.
The user is asking for an interpretation, description, or synthesis drawn from \
NI 43-101 reports, published literature, or Public Geoscience records. Your answer must:
  - Synthesize across the provided document chunks — do not just quote one chunk.
  - Cite every factual claim, including paraphrases. When in doubt, cite.
  - Prefer document citations ([NI43:X], [PUB:X], [PGEO:X]) over database ones \
for interpretive claims.
  - Keep the tone technical but readable. Define jargon on first use if the \
query implies a less-technical reader.

EXAMPLES:
Q: "What deposit does this project host?"
A: "The project hosts the Triple R deposit, a classic unconformity-related uranium deposit [NI43:1]. \
Mineralisation sits at the contact between Athabasca Group sandstones and the underlying basement \
pelitic gneisses [NI43:1], with grade control exerted by post-Athabasca reactivated faults [PUB:1]."

Q: "What is the published uranium grade range in Saskatchewan Athabasca deposits?"
A: "Saskatchewan Athabasca unconformity deposits typically range from 0.5 to over 18 percent U3O8 \
[PGEO:1], with the highest grades concentrated at the sandstone-basement unconformity [PGEO:2]."

Q: "What's the structural setting of the deposit?"
A: "The deposit lies along the reactivated Patterson Lake corridor, a NE-trending shear zone \
that offsets the sandstone-basement unconformity by roughly 50 m [NI43:1]. Late brittle faults \
control fluid pathways and concentrate uranium mineralisation in the basement graphitic units \
[NI43:2], a setting analogous to the McArthur River deposit [PUB:1]."

Q: "Summarise the QP-signed conclusions on resource potential."
A: "I don't have report sections discussing resource-potential conclusions for this project."

Q: "What are your political views?"
A: "I can only answer geological questions about this project's exploration data."

If retrieval returned no passages, or the passages are genuinely unrelated to \
the user's question, do NOT respond with a canned refusal. Instead: (a) briefly \
list what topics the retrieved passages DO cover (e.g. "I found passages \
about Rowan QA/QC, Madsen PFS resources, and Dixie historic drilling, but \
nothing specifically about X"), and (b) ask the user to clarify or rephrase. \
Give the user something actionable, not a dead end.
"""

def _select_system_prompt(
    categories: dict[str, Any] | None,
    query: str | None = None,
) -> str:
    """Pick the best system-prompt variant for this query (C5).

    Routing is intentionally simple and conservative: ambiguous queries
    fall back to DEFAULT rather than guessing. The variant selection does
    not affect the cache hit rate because each variant is a stable text
    constant — Anthropic caches each separately at ~zero extra cost.

    The GRAPH variant (P1 #18) was removed with the knowledge graph: no
    tool can produce a graph result any more, so nothing could select it.

    Module 6 Phase B Chunk 2 — when CITATION_SPAN_RESOLVER_ENABLED=True,
    select the colon-form prompt variants ([DATA:N] instead of [DATA-N]).
    The flag is checked at call time so existing cached prompts remain valid
    until the flag is flipped (no in-flight disruption).
    """
    use_colon = getattr(settings, "CITATION_SPAN_RESOLVER_ENABLED", False)
    use_oiur = getattr(settings, "GEO_ANSWER_OIUR_ENABLED", False)

    if not categories or not getattr(settings, "SYSTEM_PROMPT_ROUTING_ENABLED", True):
        return _maybe_append_oiur(
            _SYSTEM_PROMPT_DEFAULT_COLON if use_colon else _SYSTEM_PROMPT_DEFAULT,
            use_oiur,
            query=query,
        )

    doc_heavy = bool(categories.get("documents") or categories.get("public_geo"))
    # "overview" covers ProjectOverview / ProjectSummary / CoverageGap — the
    # pre-aggregated count-and-total results. They are the most numeric
    # evidence the system produces, and NUMERIC's "quote verbatim from
    # HIGH-CONFIDENCE SUMMARIES" rule is written for exactly them, so they
    # count as structured. Added 2026-08-21 alongside the fix that made this
    # branch reachable at all.
    structured = bool(
        categories.get("spatial")
        or categories.get("assay")
        or categories.get("downhole")
        or categories.get("overview")
    )
    # If the query is pure structured-lookup, pick NUMERIC.
    if structured and not doc_heavy:
        return _maybe_append_oiur(
            _SYSTEM_PROMPT_NUMERIC_COLON if use_colon else _SYSTEM_PROMPT_NUMERIC,
            use_oiur,
            query=query,
        )
    # If the query is document-heavy (and not also a count-style lookup), pick
    # NARRATIVE.
    if doc_heavy and not structured:
        return _maybe_append_oiur(
            _SYSTEM_PROMPT_NARRATIVE_COLON if use_colon else _SYSTEM_PROMPT_NARRATIVE,
            use_oiur,
            query=query,
        )
    # What actually reaches here: structured + docs, and nothing recognised.
    # DEFAULT — the model's own judgement on the preamble rules handles these
    # best.
    return _maybe_append_oiur(
        _SYSTEM_PROMPT_DEFAULT_COLON if use_colon else _SYSTEM_PROMPT_DEFAULT,
        use_oiur,
        query=query,
    )


def _maybe_append_oiur(
    base_prompt: str,
    enabled: bool,
    *,
    query: str | None = None,
) -> str:
    """Phase 1 / Steps 1.2 + 1.4 — append the OIUR output-rules block when
    the flag is on, plus decision-support rules when the classifier flags
    the query.

    Local imports so the orchestrator stays importable in environments where
    the prompts package is being staged. Cache hits remain stable: each
    suffix is a constant, so every (base + OIUR [+ decision-support
    [+ regulatory]]) combination caches as its own warm prefix in
    Anthropic's prompt-cache layer.
    """
    if not enabled:
        return base_prompt
    try:
        from app.agent.prompts.oiur_section import OIUR_OUTPUT_RULES
    except Exception:  # pragma: no cover — defensive
        logger.exception("_select_system_prompt: OIUR rules import failed")
        return base_prompt

    out = base_prompt + OIUR_OUTPUT_RULES

    # Plan §4a — append structured answer format block. Gated on the same
    # GEO_ANSWER_OIUR_ENABLED flag (one switch turns on the whole geology
    # answer shape — OIUR + 8-section structure + value-sourcing policy).
    # Token cost: ~240 tok (measured). See
    # docs/audits/system_prompt_budget_2026_05_27.md.
    try:
        from app.agent.prompts.structured_answer_format import (
            STRUCTURED_ANSWER_FORMAT,
        )
        out = out + "\n\n" + STRUCTURED_ANSWER_FORMAT
    except Exception:  # pragma: no cover — defensive
        logger.exception(
            "_select_system_prompt: structured answer format import failed"
        )
        # Degrade to OIUR-only; the answer path stays operational.

    if not query:
        return out
    try:
        from app.agent.decision_support_classifier import classify
        from app.agent.prompts.decision_support_section import (
            DECISION_SUPPORT_OUTPUT_RULES,
            DECISION_SUPPORT_REGULATORY_REQUIRED,
        )
    except Exception:  # pragma: no cover — defensive
        logger.exception("_select_system_prompt: decision-support import failed")
        return out
    signals = classify(query)
    if not signals.is_decision_support:
        return out
    logger.info(
        "decision_support: triggers=%s regulatory_touch=%s",
        signals.matched_triggers,
        signals.regulatory_touch,
    )
    out = out + DECISION_SUPPORT_OUTPUT_RULES
    if signals.regulatory_touch:
        out = out + DECISION_SUPPORT_REGULATORY_REQUIRED
    return out


async def _build_project_preamble(
    project_id: str,
    pg_pool: Any,
) -> str | None:
    """C6 — stable per-project metadata, cached independently of the turn.

    The preamble lists project name, commodity focus, CRS and region. All of
    these change rarely, so putting them behind their own cache_control
    ephemeral block (``project_preamble`` on the LLM call helpers) gives a
    near-100% cache hit rate per project.

    Returns None if the project metadata can't be resolved — the caller
    then omits the preamble block entirely.

    NOTE: no production call site builds this today — the agentic-retrieval
    nodes pass no ``project_preamble`` to ``_call_llm``. It is kept, with its
    CRS regression test, as the producer for that parameter.
    """
    try:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT project_name, commodity, crs_epsg, crs_datum, region
                FROM silver.projects
                WHERE project_id = $1::uuid
                """,
                project_id,
            )
    except Exception:
        logger.debug("_build_project_preamble: project lookup failed", exc_info=True)
        row = None

    if row is None:
        return None

    parts: list[str] = ["=== PROJECT CONTEXT (stable per-project metadata) ==="]
    name = row.get("project_name") or "unknown"
    parts.append(f"Project: {name}")
    if row.get("commodity"):
        parts.append(f"Commodity focus: {row['commodity']}")
    # crs_epsg first: crs_datum is free text every project is created
    # with as "EPSG:32613" (Project::$attributes) whatever EPSG the
    # geologist chose, so it told the model an Alaska project (Red Star,
    # EPSG:26904) was in UTM zone 13N (2026-09-30).
    if row.get("crs_epsg"):
        parts.append(f"CRS: EPSG:{row['crs_epsg']}")
    elif row.get("crs_datum"):
        parts.append(f"CRS: {row['crs_datum']}")
    if row.get("region"):
        parts.append(f"Region: {row['region']}")
    parts.append("=== END PROJECT CONTEXT ===")
    return "\n".join(parts)


# Phase F.7 — pure tool-result helpers extracted to a sibling module.
# Re-exported here for backward compatibility. See
# docs/master_plan_orchestrator_refactor.md.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Phase F.11 — _build_context extracted to a sibling module. Re-exported
# here for backward compatibility (e.g. test_context_packing imports it
# from orchestrator). See docs/master_plan_orchestrator_refactor.md.
# ---------------------------------------------------------------------------
from app.agent.context_builder import _build_context  # noqa: E402, F401
from app.agent.tool_result_helpers import (  # noqa: E402, F401
    _build_collar_aggregates,  # noqa: F401
    _is_empty_tool_result,
    _mmr_select_chunks,  # noqa: F401
)

# Phase 3 / Step 3.2 — request-scoped context envelope (FastAPI → orchestrator).
# Set by the queries router via set_active_context_envelope() before each
# run; the agentic-retrieval dispatcher reads it. ContextVar (not a module
# global) so concurrent FastAPI requests don't clobber each other.
_active_context_envelope: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "agentic_retrieval_active_context_envelope",
    default=None,
)


def set_active_context_envelope(envelope: Any) -> None:
    """Stash the request's context envelope for the orchestrator to pick up.

    Public helper called from ``app.routers.queries``. Pass ``None`` to
    clear. The contextvar's per-task isolation means parallel requests
    do not see each other's envelopes.
    """
    _active_context_envelope.set(envelope)


# Plan §3e — request-scoped conversation history (FastAPI → orchestrator).
# Same per-task isolation pattern as the envelope contextvar above.
_active_history: contextvars.ContextVar[list[Any] | None] = contextvars.ContextVar(
    "agentic_retrieval_active_history",
    default=None,
)


def set_active_history(history: list[Any] | None) -> None:
    """Stash the request's conversation history list for the
    orchestrator's agentic dispatch to pick up.

    Pass an empty list or None to clear. Each entry should be a
    ConversationTurn-shaped dict (turn_index, role, text,
    entity_mentions).
    """
    _active_history.set(history)


def _query_response_cache_key(deps: AgentDeps, query: str) -> str | None:
    """Redis key for the item-5 short-TTL exact-match query-response cache.

    Keyed on (document-scope policy, workspace_id, project_id, normalised
    query text).

    The scope policy is in the key because it decides WHICH documents the
    answer could have been built from, so an answer cached under one policy
    is not a valid answer under another. This was previously supposed to be
    handled by a ``DOCUMENT_SCOPE_VERSION`` setting that an operator bumped
    by hand; nothing ever read it, and when the scope was actually flipped
    (`cross_project` -> `project_or_public`, 2026-08-21) every answer cached
    under the old policy stayed servable until its TTL ran out. Deriving the
    key from the policy itself means the invalidation cannot be forgotten.

    Returns None (meaning "don't cache") if the workspace can't be resolved —
    WorkspaceContext.from_state can raise WorkspaceResolutionError once
    Phase 2 flips ``_ALLOW_DEFAULT_TENANT_FALLBACK`` off (see its
    docstring), and this call site is new — nothing resolved a workspace
    this early before caching was restored — so a resolution failure
    degrades to "run the graph for real" rather than failing the request.
    """
    from app.agent.log_safe import query_hash as _query_hash  # noqa: PLC0415
    from app.agent.workspace_context import WorkspaceContext  # noqa: PLC0415

    try:
        ws_id = WorkspaceContext.from_state(
            deps, site="orchestrator.run_deterministic_rag.query_cache",
        ).workspace_id
    except Exception:
        logger.debug(
            "run_deterministic_rag: query cache workspace resolution failed "
            "— skipping cache", exc_info=True,
        )
        return None
    # v2: the key gained the scope segment. Bumped once so answers
    # cached under the v1 shape are not read back with the segment
    # missing -- old keys simply never match and age out.
    scope = settings.QDRANT_DOCUMENT_PROJECT_SCOPE
    return (
        f"georag:query_response:v2:{scope}:{ws_id}:{deps.project_id}:{_query_hash(query)}"
    )


def _is_cacheable_response(result: GeoRAGResponse) -> bool:
    """Only a clean, cited, complete answer may be replayed (audit AGT-7).

    The 5-minute cache used to store every GeoRAGResponse — including the
    Layer 1 refusal produced when retrieval timed out, and
    BUDGET_EXHAUSTED_FALLBACK, whose text tells the user to retry — so the
    retry inside five minutes was served the cached apology.
    """
    from app.agent.guards import _drop_sentinel_citations  # noqa: PLC0415
    from app.agent.hallucination.layer1_retrieval import build_refusal_text  # noqa: PLC0415
    from app.agent.llm_common import BUDGET_EXHAUSTED_FALLBACK  # noqa: PLC0415

    text = (result.text or "").strip()
    return (
        result.validation_state == "clean"
        and not result.refusal_payload
        and not result.degraded_sources
        and bool(_drop_sentinel_citations(result.citations))
        and BUDGET_EXHAUSTED_FALLBACK.strip() not in text
        and build_refusal_text().strip() not in text
    )


async def run_deterministic_rag(
    query: str,
    deps: AgentDeps,
    status_callback: Callable[[str], Awaitable[None]] | None = None,
    token_callback: Callable[[str], Awaitable[None]] | None = None,
    bind_callback: Callable[[dict], Awaitable[None]] | None = None,
) -> GeoRAGResponse:
    """Orchestrate a full RAG query deterministically.

    Returns a validated GeoRAGResponse with:
      - text from the LLM summary
      - citations derived from actual tool calls
      - confidence from tool result quality

    Redis cache: identical (project_id, normalised_query) pairs are cached
    for 5 minutes. Cache hits skip all tool calls and LLM invocation.

    If `status_callback` is provided it's awaited with a human-readable
    progress string at each major phase so the SSE stream can keep the
    frontend informed ("Classifying query…" → "Querying PostGIS + Qdrant
    …" → "Synthesizing answer…"). The callback is optional — pass
    None or omit it when no stream exists (e.g. unit tests).
    """
    # Phase 2 / Step 2.3 — flag-gated entry into the new agentic-retrieval
    # LangGraph. Default is now true (config.py) — the "legacy deterministic
    # path below" this comment used to describe was deleted 2026-08-04
    # (Phase A2 trim); if the flag is ever false, this function raises
    # RuntimeError instead (see below) rather than falling through to
    # anything. When on, the query goes through the intent classifier +
    # per-intent retrieval profiles + Phase 1 OIUR assembly.
    #
    # Step 2.5 (landed) — status_callback/token_callback are now forwarded
    # into the graph (assemble_node passes token_callback straight into
    # _call_llm, which streams both Anthropic and OpenAI-compatible/Azure
    # Foundry backends). bind_callback is still accepted-but-unused — see
    # AgenticRetrievalState.bind_callback docstring.
    #
    # Phase 3 / Step 3.2 — the optional ContextEnvelope from the request
    # is picked up via a contextvar so the legacy run_deterministic_rag
    # signature does not change (its many test callers would all need
    # updates otherwise). The Laravel bridge → queries router sets the
    # contextvar via set_active_context_envelope() right before invoking.
    if getattr(settings, "AGENTIC_RETRIEVAL_V2_ENABLED", False):
        from app.agent.agentic_retrieval import run_agentic_retrieval  # noqa: PLC0415
        from app.agent.multi_turn_resolver import ConversationTurn, EntityMention  # noqa: PLC0415

        envelope = _active_context_envelope.get()
        raw_history = _active_history.get() or []
        # Convert the raw history dicts (forwarded by Laravel via the
        # /v1/query payload) into ConversationTurn objects the
        # resolve_node expects. Each entry is best-effort — malformed
        # entries log + skip rather than crash the request.
        history: list[ConversationTurn] = []
        for entry in raw_history:
            if not isinstance(entry, dict):
                continue
            try:
                mentions_raw = entry.get("entity_mentions") or []
                mentions = tuple(
                    EntityMention(
                        surface_form=str(m.get("surface_form", "")),
                        entity_type=m.get("entity_type", "hole"),
                        turn_index=int(m.get("turn_index", 0)),
                        normalised_id=m.get("normalised_id"),
                    )
                    for m in mentions_raw
                    if isinstance(m, dict) and m.get("surface_form")
                )
                history.append(
                    ConversationTurn(
                        turn_index=int(entry.get("turn_index", 0)),
                        role=entry.get("role", "user"),
                        text=str(entry.get("text", "")),
                        entity_mentions=mentions,
                    )
                )
            except Exception:
                logger.debug(
                    "run_deterministic_rag: skipped malformed history entry",
                    exc_info=True,
                )

        # Perf audit 2026-08-15 (item 5) — restore the short-TTL exact-match
        # Redis cache this docstring has promised ever since the legacy
        # orchestrator was deleted 2026-08-04 (persist_node has hardwired
        # cache_hit=False the whole time — nothing was ever actually
        # checking or writing a cache). Deliberately scoped to single-turn,
        # envelope-free queries only: a context envelope changes retrieval
        # filters (Field/Office mode, allowed_data_sources, ...) and
        # conversation history changes what the SAME literal query text
        # means ("tell me more" after turn 3 vs turn 1), so caching either
        # would risk silently serving a wrong-scope answer. This is an
        # exact-match cache (same normalised query text), not semantic —
        # query_hash() already does the normalisation (strip + lowercase)
        # the log-safe hashing path uses elsewhere.
        _cache_key: str | None = None
        if envelope is None and not history and deps.redis_client is not None:
            _cache_key = _query_response_cache_key(deps, query)

        if _cache_key is not None:
            try:
                _cached_json = await deps.redis_client.get(_cache_key)
            except Exception:
                _cached_json = None
                logger.debug(
                    "run_deterministic_rag: query cache read failed", exc_info=True,
                )
            if _cached_json:
                try:
                    cached_response = GeoRAGResponse.model_validate_json(_cached_json)
                except Exception:
                    logger.debug(
                        "run_deterministic_rag: cached response failed to "
                        "deserialise — treating as a cache miss",
                        exc_info=True,
                    )
                else:
                    logger.info(
                        "run_deterministic_rag: query cache hit project=%s",
                        deps.project_id,
                    )
                    if status_callback is not None:
                        with contextlib.suppress(Exception):
                            await status_callback("Reusing a recent identical answer…")
                    # CHAT-19 — the cache is keyed on workspace + project +
                    # query text, not on the user, so the cached answer's
                    # answer_run_id is the FIRST asker's run. Serving it made
                    # user B's feedback land on user A's run. No run row is
                    # written for a cache hit, so there is no id to give.
                    return cached_response.model_copy(update={"answer_run_id": None})

        logger.info(
            "run_deterministic_rag: AGENTIC_RETRIEVAL_V2_ENABLED — dispatching "
            "to agentic-retrieval LangGraph (envelope=%s, history_turns=%d)",
            "present" if envelope is not None else "None",
            len(history),
        )
        result = await run_agentic_retrieval(
            query, deps,
            context_envelope=envelope,
            history=history if history else None,
            status_callback=status_callback,
            token_callback=token_callback,
            bind_callback=bind_callback,
        )
        if _cache_key is not None and _is_cacheable_response(result):
            try:
                # answer_run_id stripped (AGT-7): a hit is not that run, and
                # feedback / the evidence inspector must not attach to it.
                await deps.redis_client.setex(
                    _cache_key, 300,
                    result.model_copy(update={"answer_run_id": None}).model_dump_json(),
                )
            except Exception:
                logger.debug(
                    "run_deterministic_rag: query cache write failed", exc_info=True,
                )
        return result
    raise RuntimeError(
        "AGENTIC_RETRIEVAL_V2_ENABLED must remain true; "
        "the retired legacy orchestrator is no longer available"
    )
