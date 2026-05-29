"""Six-intent classifier — Phase 2 / Step 2.2.

Classifies each query into one of six intents (see :data:`INTENT_LABELS`) so
the agentic-retrieval graph can route it to the matching subgraph. Each
intent triggers a different retrieval profile downstream (Step 2.3).

Strategy: **keyword-first with LLM fallback on ambiguity.**

  1. Compute a per-intent keyword score by counting matched trigger phrases
     from the plan's Step-2.3 table (lines 186-194 of the geologist plan).
  2. Pick the top-scoring intent; its confidence is the normalised gap to
     the second-best. No matches → confidence 0 → safe default = synthesis.
  3. Two intents within 0.1 → tiebreak by retrieval-breadth priority
     (hypothesis > synthesis > decision > uncertainty > anomaly > factual).
     The plan: "route to the higher-retrieval intent — retrieve more, not less."
  4. Confidence < 0.6 → optional LLM fallback (a single small Qwen call via
     :func:`app.agent.llm_calls._call_llm`). The fallback is opt-in by passing
     an HTTP client; when omitted the classifier stays fully rule-based and
     returns its keyword answer (route to synthesis if no signal).

Phase 1.4's :mod:`app.agent.decision_support_classifier` remains the source of
truth for the decision-support intent's secondary signals (regulatory_touch,
"drill" weak-signal rules). This classifier delegates to it for that single
intent and replicates the simpler keyword shape for the other five.

The classifier is **stateless and side-effect-free** — fine to call from any
LangGraph node or unit test without setup.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal

from app.agent.decision_support_classifier import classify as classify_decision_support

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Intent labels
# ---------------------------------------------------------------------------


Intent = Literal[
    "factual_lookup",
    "synthesis",
    "hypothesis_generation",
    "anomaly_detection",
    "uncertainty_quantification",
    "decision_support",
    # ADR-0007 PR-1 — chat cards + structured-aggregation intents
    "project_summary",
    "coverage_gap",
]

INTENT_LABELS: tuple[Intent, ...] = (
    "factual_lookup",
    "synthesis",
    "hypothesis_generation",
    "anomaly_detection",
    "uncertainty_quantification",
    "decision_support",
    "project_summary",
    "coverage_gap",
)


# Retrieval-breadth priority used to break ties. Higher index = broader
# retrieval → preferred when scores are within the tiebreak window. From the
# plan: "retrieve more, not less" on ambiguous cases.
#
# ADR-0007: project_summary slots at 2 (broader than factual, narrower than
# decision); coverage_gap slots at 3 (broader still — gap analysis touches
# many tables). Both fall below synthesis / hypothesis_generation because
# those open the full hybrid retrieval surface.
_BREADTH_RANK: dict[Intent, int] = {
    "factual_lookup": 0,
    "anomaly_detection": 1,
    "project_summary": 2,
    "coverage_gap": 3,
    "uncertainty_quantification": 4,
    "decision_support": 5,
    "synthesis": 6,
    "hypothesis_generation": 7,
}

# Tiebreak window: scores within this delta are considered "tied" and broken
# by _BREADTH_RANK. The plan specifies 0.1.
_TIEBREAK_DELTA = 0.1

# Below this confidence the LLM fallback kicks in (when an HTTP client is
# provided). The plan specifies 0.6.
_LLM_FALLBACK_THRESHOLD = 0.6


# ---------------------------------------------------------------------------
# Keyword triggers
# ---------------------------------------------------------------------------
# Each entry is a regex that matches if any of the intent's trigger phrases
# appear in the query. Word-boundary anchored so substrings inside other
# words don't false-fire. Case-insensitive at compile time.


def _compile(*patterns: str) -> re.Pattern[str]:
    joined = "|".join(patterns)
    return re.compile(rf"(?:{joined})", re.IGNORECASE)


# Order intentionally narrow → broad so the regex bias matches the plan's
# trigger list verbatim. "drill" is HANDLED via decision_support_classifier
# (which has the weak-signal logic) — we don't include it here.
_TRIGGERS: dict[Intent, re.Pattern[str]] = {
    "factual_lookup": _compile(
        r"\bwhat\s+is\b",
        r"\bwhat\s+are\b",
        r"\bwhat\s+does\b",  # broad — catches "what does the CRIRSCO template require"
        r"\bdefine\b",
        r"\bdefinition\s+of\b",
        r"\bformal\s+name\b",
        r"\bstandard\b",
        r"\bclassification\s+(?:for|of|under|require)\b",
        r"\bni\s*43-?101\s+(?:§|section|clause)\b",
    ),
    "synthesis": _compile(
        r"\bintegrate\b",
        r"\bacross\s+(?:wells?|holes?|sites?|sources?|the\s+\w+)\b",  # narrow "across" so "constrained areas" doesn't false-fire
        r"\bsummari[sz]e\b",
        r"\bcompare\s+(?:holes?|wells?|targets?|sources?)\b",
        r"\bwhat\s+does\s+the\s+evidence\s+show\b",
        r"\bsynthesi[sz]e\b",
        r"\boverall\s+picture\b",
    ),
    "hypothesis_generation": _compile(
        r"\bcould\s+explain\b",
        r"\bwhat\s+(?:geological\s+)?models?\b",
        r"\balternative(?:\s+hypothes[ei]s)?\b",
        r"\bwhat\s+if\b",
        r"\bpossible\s+causes?\b",
        r"\bpossible\s+explanations?\b",
        r"\bhypothes[ei]s\b",
        r"\bmore\s+consistent\s+with\b",
    ),
    "anomaly_detection": _compile(
        r"\boutliers?\b",
        r"\banomal(?:y|ies|ous)\b",
        r"\bflag\b",
        r"\bqa[\/-]?qc\b",
        r"\bblanks?\b",
        r"\bcrm(?:s)?\b",
        r"\bduplicates?\b",
        r"\bdetection\s+limits?\b",
        r"\bre-?assay\b",
        r"\brerun\b",
    ),
    "uncertainty_quantification": _compile(
        r"\bhow\s+certain\b",
        r"\bconfidence\b",
        r"\bsensitivity\b",
        r"\bsensitive\b",
        r"\brange\b",
        r"\bassumptions?\b",
        r"\bhow\s+reliable\b",
        r"\bhow\s+robust\b",
        r"\buncertainty\b",
        r"\bcapping\b",
        r"\bdirect\s+measurements?\b",
        r"\bindirect\s+(?:correlations?|measurements?|inferences?)\b",
        r"\bconstraints?\b",
    ),
    # ADR-0007 PR-1: project_summary triggers — structured-aggregation
    # questions about what techniques / contractors / geologists collected
    # data, and when. "Breakdown / by year / contractors" are the strongest
    # signals; "summary" + "overview" are weaker (synthesis can also match)
    # but the tiebreak rank lets synthesis win when the query is broader.
    "project_summary": _compile(
        r"\bbreakdown\b",
        r"\bproject\s+summary\b",
        r"\bdata\s+collection\b",
        r"\btechniques?\b",
        r"\bcollection\s+techniques?\b",
        r"\bwhat.{0,20}collected\b",
        r"\bwho\s+worked\b",
        r"\bcontractors?\b",
        r"\bgeologists?\b",
        r"\bby\s+year\b",
        r"\bover\s+time\b",
        r"\bdrill(?:ing)?\s+history\b",
        r"\bcampaign\s+history\b",
        r"\bhistorical\s+work\b",
    ),
    # ADR-0007 PR-1: coverage_gap triggers — gap analysis / missing-data
    # questions. Strong signals: "gap", "missing", "incomplete", "coverage";
    # weaker phrasings like "what don't we have / haven't we" rely on the
    # context-word lookahead so they don't false-fire on unrelated negation.
    "coverage_gap": _compile(
        r"\bgaps?\b",
        r"\bmissing\b",
        r"\bincomplete\b",
        r"\bcoverage\b",
        r"\bwhat.{0,10}don'?t\s+(?:we\s+)?have\b",
        r"\bwhat.{0,10}haven'?t\s+(?:we\s+)?(?:got|collected|done)\b",
        r"\bholes?\s+in\s+(?:the\s+)?data\b",
        r"\bdata\s+gaps?\b",
        r"\bnot\s+yet\s+(?:collected|sampled|logged)\b",
        r"\bunder-?sampled\b",
        r"\bwhere.{0,20}sparse\b",
    ),
    # decision_support is computed via classify_decision_support() — see
    # _decision_support_score() below.
}


# Extra decision-support phrases the plan's verbatim trigger list misses but
# that are unambiguously decision-flavoured in geological context. Applied
# in addition to the Phase 1.4 classifier so the canonical 7-phrase list
# stays the source of truth and these are augmentation only.
_DECISION_SUPPORT_AUGMENT_RE = _compile(
    r"\bwhat\s+(?:material\s+)?(?:documentation\s+)?gaps?\s+(?:would|might)\b",
    r"\bdocumentation\s+gaps?\b",
    r"\bwhich\s+drill\s+targets?\s+(?:would|should|will|might)\b",
    r"\bwould\s+(?:most\s+)?(?:reduce|prevent|block)\b",
)


def _keyword_count(pattern: re.Pattern[str], query: str) -> int:
    """Count distinct matches of *pattern* in *query*."""
    return len(pattern.findall(query))


def _decision_support_score(query: str) -> tuple[float, tuple[str, ...], bool]:
    """Use Phase 1.4's classifier as the decision-support score source.

    Returns ``(score, matched_triggers, regulatory_touch)``. Score is the
    number of matched triggers (after applying the "drill needs decision-verb
    companion" rule from Phase 1.4) — keeps scoring consistent with the
    other intents' counters. We also add a single point per match of the
    augmentation regex (decision-flavoured phrasings the canonical 7-phrase
    list misses) so queries like "what documentation gaps would prevent…"
    classify correctly without the LLM fallback.
    """
    sig = classify_decision_support(query)
    augment_matches = _DECISION_SUPPORT_AUGMENT_RE.findall(query)
    score = float(len(sig.matched_triggers) + len(augment_matches))
    triggers = sig.matched_triggers + tuple(
        m.lower() if isinstance(m, str) else m[0].lower() for m in augment_matches
    )
    return score, triggers, sig.regulatory_touch


# ---------------------------------------------------------------------------
# IntentResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentResult:
    """Output of :func:`classify_intent`.

    Attributes:
        intent: The selected intent.
        confidence: 0–1. Computed from the normalised gap between the top
            intent's keyword score and the runner-up. 0 = no triggers
            matched anywhere; 1 = top intent matched and no other intent
            matched.
        matched_triggers: The actual trigger strings (lowercased) the top
            intent matched. Surfaced into telemetry + the lineage artifact.
        second_choice: The runner-up intent (None when only one intent
            matched or when the keyword classifier returned no signal).
        second_confidence: Score for the runner-up, on the same scale as
            ``confidence``.
        used_llm_fallback: True when an LLM call broke a low-confidence tie.
            False when the result came from the keyword pass alone.
        regulatory_touch: Carried from the decision-support classifier —
            indicates the query references resource classification,
            drilling, sampling, or QA/QC and therefore requires an
            NI 43-101 / reporting-code implication in the answer.
    """

    intent: Intent
    confidence: float
    matched_triggers: tuple[str, ...]
    second_choice: Intent | None
    second_confidence: float
    used_llm_fallback: bool
    regulatory_touch: bool


# ---------------------------------------------------------------------------
# Scoring + tiebreak
# ---------------------------------------------------------------------------


def _score_all_intents(query: str) -> tuple[
    dict[Intent, float],
    dict[Intent, tuple[str, ...]],
    bool,
]:
    """Return (scores, matched_triggers_per_intent, regulatory_touch).

    Scores are raw match counts; normalisation happens in ``classify_intent``.
    """
    scores: dict[Intent, float] = {}
    matches: dict[Intent, tuple[str, ...]] = {}

    # Decision-support uses Phase 1.4's classifier (it has the "drill" weak
    # signal handling we don't want to duplicate).
    ds_score, ds_triggers, regulatory_touch = _decision_support_score(query)
    scores["decision_support"] = ds_score
    matches["decision_support"] = ds_triggers

    # Other five intents: regex match counts.
    for intent, pattern in _TRIGGERS.items():
        if intent == "decision_support":
            continue
        ms = pattern.findall(query)
        scores[intent] = float(len(ms))
        matches[intent] = tuple(m.lower() if isinstance(m, str) else m[0].lower() for m in ms)

    return scores, matches, regulatory_touch


def _pick_top_intent(
    scores: dict[Intent, float],
) -> tuple[Intent | None, Intent | None]:
    """Apply tiebreak rules and return ``(top, second)``.

    The "top" is the highest-scoring intent. If the second-best is within
    :data:`_TIEBREAK_DELTA`, the breadth-priority order wins. ``None`` when
    no intent has any keyword match.
    """
    nonzero = [(i, s) for i, s in scores.items() if s > 0]
    if not nonzero:
        return None, None

    ranked = sorted(nonzero, key=lambda kv: kv[1], reverse=True)
    top, top_score = ranked[0]
    if len(ranked) == 1:
        return top, None
    second, second_score = ranked[1]

    # Tiebreak: scores within delta → pick the broader retrieval intent.
    if (top_score - second_score) <= _TIEBREAK_DELTA:
        if _BREADTH_RANK[second] > _BREADTH_RANK[top]:
            top, second = second, top
    return top, second


def _confidence(scores: dict[Intent, float], top: Intent, second: Intent | None) -> float:
    """Map (top_score, second_score) → confidence on a 0..1 scale.

    Heuristic: confidence = (top - second) / max(top, 1).  Hits 1.0 when
    only the top intent matched; trends toward 0 as the runner-up catches up.
    """
    top_s = scores[top]
    if top_s <= 0:
        return 0.0
    second_s = scores[second] if second is not None else 0.0
    gap = top_s - second_s
    return max(0.0, min(1.0, gap / max(top_s, 1.0)))


# ---------------------------------------------------------------------------
# LLM fallback
# ---------------------------------------------------------------------------


_LLM_FALLBACK_SYSTEM_PROMPT = """You are an intent classifier for geological queries.

Pick EXACTLY ONE intent label from this list:

  factual_lookup          — a specific term, standard, or formal definition
  synthesis               — integrate or compare across multiple sources
  hypothesis_generation   — propose competing geological models / explanations
  anomaly_detection       — find outliers, QA/QC failures, or assay artifacts
  uncertainty_quantification — assess how reliable / sensitive an estimate is
  decision_support        — recommend, rank, or prioritise next actions
  project_summary         — structured breakdown of data collection techniques,
                            contractors, geologists, or campaigns over time
  coverage_gap            — find missing or under-sampled data; what holes
                            exist in the collected dataset

Reply with ONE WORD only: the chosen label. No explanation, no punctuation.
""".strip()


async def _llm_fallback(
    query: str,
    *,
    openai_http_client,
) -> Intent | None:
    """Ask a small Qwen model to pick an intent.

    Called only when the keyword classifier returned confidence below
    :data:`_LLM_FALLBACK_THRESHOLD` AND a client was supplied. The fallback
    is a single short call with response_format=json discipline relaxed to
    a one-word output — Qwen3-14B-AWQ handles this reliably.

    Returns ``None`` on any LLM error so the caller can fall back to the
    keyword-pass answer or the safe default (synthesis).
    """
    if openai_http_client is None:
        return None
    try:
        from app.agent.llm_calls import _call_llm
    except Exception:  # pragma: no cover — defensive
        logger.exception("intent_classifier: _call_llm import failed")
        return None
    try:
        raw = await _call_llm(
            query=query,
            context="(no context — intent classification only)",
            temperature=0.0,
            openai_http_client=openai_http_client,
            system_prompt=_LLM_FALLBACK_SYSTEM_PROMPT,
            audit_label="intent_classifier",
        )
    except Exception:
        logger.exception("intent_classifier: LLM fallback call failed")
        return None

    if not raw:
        return None
    cleaned = raw.strip().lower().split()[0] if raw.strip() else ""
    # Strip punctuation and bracketing the model might still emit.
    cleaned = re.sub(r"[^a-z_]", "", cleaned)
    if cleaned in INTENT_LABELS:
        return cleaned  # type: ignore[return-value]
    logger.info(
        "intent_classifier: LLM fallback returned unrecognised label %r — ignoring", raw[:80]
    )
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def classify_intent(
    query: str,
    *,
    openai_http_client=None,
) -> IntentResult:
    """Classify *query* into one of six intents.

    Args:
        query: Raw user query text.
        openai_http_client: Optional vLLM-compatible HTTP client (the one
            the orchestrator already pools). When provided, low-confidence
            keyword scores trigger a single small Qwen call to disambiguate.
            When omitted, the classifier is fully rule-based.

    Returns:
        :class:`IntentResult`. Always returns a valid intent — defaults to
        ``synthesis`` (the plan's safe default) when no signal is found.
    """
    if not query or not query.strip():
        return IntentResult(
            intent="synthesis",
            confidence=0.0,
            matched_triggers=(),
            second_choice=None,
            second_confidence=0.0,
            used_llm_fallback=False,
            regulatory_touch=False,
        )

    scores, matches, regulatory_touch = _score_all_intents(query)

    # Hole-ID short-circuit. When the query names a specific drill hole AND
    # carries NO competing higher-retrieval intent signal, route to
    # factual_lookup → query_collar_details (a structured PostGIS lookup).
    # Without this rule, casual phrasings like "this hole please tell me
    # about it, 36-1085" score 0 across every keyword bucket, fall through
    # to the LLM fallback, and get mis-classed as coverage_gap (observed
    # in production 2026-05-25).
    #
    # The "no competing signal" guard preserves the existing labeled-set
    # behaviour: queries like "Integrate the drill logs for DDH-07 to
    # DDH-12 — what is the grade continuity interpretation?" name holes
    # but carry strong synthesis triggers ("integrate") and SHOULD route
    # to synthesis. The short-circuit only fires when the user really is
    # just asking about specific named holes.
    try:
        from app.agent.viz_builder import extract_hole_ids  # noqa: PLC0415
    except Exception:  # pragma: no cover — defensive
        logger.exception("intent_classifier: extract_hole_ids import failed")
    else:
        if extract_hole_ids(query):
            # Competing intents that, when present, should win over a bare
            # hole-id mention. factual_lookup itself is allowed — "what is
            # the depth of hole 36-1085" still routes here.
            competing = {
                "synthesis",
                "hypothesis_generation",
                "anomaly_detection",
                "uncertainty_quantification",
                "decision_support",
                "project_summary",
                "coverage_gap",
            }
            if not any(scores.get(i, 0) > 0 for i in competing):
                return IntentResult(
                    intent="factual_lookup",
                    confidence=1.0,
                    matched_triggers=("hole_id_detected",),
                    second_choice=None,
                    second_confidence=0.0,
                    used_llm_fallback=False,
                    regulatory_touch=regulatory_touch,
                )
    top, second = _pick_top_intent(scores)
    confidence = _confidence(scores, top, second) if top is not None else 0.0
    second_confidence = (
        _confidence(scores, second, top) if (second is not None and top is not None) else 0.0
    )

    # LLM fallback fires whenever confidence is below threshold AND a client
    # is supplied. This INCLUDES the no-signal case (top is None → confidence
    # 0) — the plan's "if intent_confidence < 0.6" rule applies uniformly.
    used_llm = False
    if confidence < _LLM_FALLBACK_THRESHOLD and openai_http_client is not None:
        llm_choice = await _llm_fallback(query, openai_http_client=openai_http_client)
        if llm_choice is not None and llm_choice != top:
            logger.info(
                "intent_classifier: LLM fallback %s → %s (kw conf=%.2f)",
                top,
                llm_choice,
                confidence,
            )
            # Promote the LLM choice as the top intent. The previous keyword
            # top (if any) becomes the runner-up. Confidence is re-derived
            # from the LLM choice's keyword score; if it was zero, set a
            # modest 0.5 so downstream knows this came from LLM
            # disambiguation rather than keyword certainty.
            old_top = top
            top = llm_choice
            second = old_top
            kw_score = scores.get(top, 0.0)
            if kw_score > 0:
                confidence = _confidence(scores, top, second)
            else:
                confidence = 0.5
            second_confidence = (
                _confidence(scores, second, top) if second is not None else 0.0
            )
            used_llm = True

    if top is None:
        # No keyword signal AND either no LLM client or fallback returned
        # nothing usable → safe default per plan Step 2.2.
        return IntentResult(
            intent="synthesis",
            confidence=0.0,
            matched_triggers=(),
            second_choice=None,
            second_confidence=0.0,
            used_llm_fallback=False,
            regulatory_touch=regulatory_touch,
        )

    return IntentResult(
        intent=top,
        confidence=confidence,
        matched_triggers=matches.get(top, ()),
        second_choice=second,
        second_confidence=second_confidence,
        used_llm_fallback=used_llm,
        regulatory_touch=regulatory_touch,
    )


def classify_intent_sync(query: str) -> IntentResult:
    """Synchronous shortcut over :func:`classify_intent` (no LLM fallback).

    Use from tests, the orchestrator's prompt-selection chain, or anywhere
    that needs a fast keyword-only classification without an async context.
    """
    import asyncio

    return asyncio.run(classify_intent(query, openai_http_client=None))
