"""Decision-support intent classifier — Phase 1 / Step 1.4.

Regex over the 7 trigger phrases the plan specifies (line 121 of the
Phase 1 plan):

    "should we," "next steps," "prioritise," "drill," "recommend,"
    "rank," "what should"

This is a deliberate **first-pass** classifier. Phase 2 (§04j Agentic
Retrieval) absorbs it as one feature input to the real LangGraph intent
classifier — at which point this module's role becomes "fast structural
hint" rather than "sole classifier". See ``georag-geologist-question-plan.md``
Step 2.2 for the Phase-2 boundary.

The classifier also detects whether the decision touches resource
classification, drilling, or sampling (the plan's "≥1 NI 43-101 implication
must be surfaced" criterion) so the orchestrator can flag the
``regulatory_constraints`` requirement to the LLM via the prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Word-boundary-anchored regex of the plan's 7 trigger phrases. "drill" is
# kept as a verb-or-noun match because the plan's keyword list reads "drill"
# unqualified — "should we drill DDH-13" and "rank the drill targets" both
# qualify. The "drill" trigger has high false-positive risk on its own
# (e.g. "the drill log shows 12 m of mineralisation") so it ONLY counts
# when paired with at least one other signal OR when followed/preceded by
# decision-flavoured context within a small window.
_TRIGGERS: tuple[str, ...] = (
    r"should\s+we\b",
    r"next\s+steps?\b",
    r"prioriti[sz]e\b",
    r"recommend(?:s|ed|ing|ation)?\b",
    r"\brank(?:s|ed|ing)?\b",
    r"what\s+should\b",
)

_TRIGGER_RE = re.compile("(?:" + "|".join(_TRIGGERS) + ")", re.IGNORECASE)

# The "drill" keyword is treated as a weak signal because it's also the
# subject of many factual lookups. It only flips the classifier on when
# paired with a decision verb ("plan", "where", "which", "next", "best") or
# the question literally asks for action ("should I", "what's the best").
_DRILL_RE = re.compile(r"\bdrill(?:ing|ed|s)?\b", re.IGNORECASE)
_DRILL_DECISION_VERBS = re.compile(
    r"\b(?:plan|where|which|target(?:s)?|best|next|prioriti[sz]e|infill)\b",
    re.IGNORECASE,
)

# Regulatory-touch detector. When the query mentions any of these terms,
# the prompt must instruct the LLM to surface ≥1 NI 43-101 implication in
# ``decision_support.regulatory_constraints``. The list is intentionally
# narrow — false positives here just add a (usually-relevant) reminder to
# the prompt; false negatives skip a check the plan requires.
_REGULATORY_TOUCH_RE = re.compile(
    r"\b("
    r"resource(?:\s+(?:estimate|classification|category))?"
    r"|measured|indicated|inferred"
    r"|ni\s*43-?101|cim|crirsco|jorc"
    r"|qp(?:'s)?(?:\s+sign(?:-?off)?)?"
    r"|qualified\s+person"
    r"|drill\s+(?:programs?|plans?|targets?|holes?|spacings?)"
    r"|infill"
    r"|sampl(?:e|ing|es)"
    r"|assay(?:s|ing)?"
    r"|qa[\/-]?qc"
    r")\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class DecisionSupportSignals:
    """Result of classifying a single query."""

    is_decision_support: bool
    matched_triggers: tuple[str, ...]
    regulatory_touch: bool

    def __bool__(self) -> bool:  # convenience for ``if classify(q):`` paths
        return self.is_decision_support


def classify(query: str) -> DecisionSupportSignals:
    """Return the full signal bundle for *query*.

    Use :func:`is_decision_support_query` when only the boolean is needed.
    """
    if not query or not query.strip():
        return DecisionSupportSignals(False, (), False)

    text = query.strip()
    matched = tuple(m.group(0).lower() for m in _TRIGGER_RE.finditer(text))

    # "drill" is a weak signal — only counts when paired with a decision verb.
    if _DRILL_RE.search(text) and _DRILL_DECISION_VERBS.search(text):
        # Avoid duplicating an existing trigger match.
        if "drill" not in matched:
            matched = matched + ("drill",)

    is_ds = bool(matched)
    reg = bool(_REGULATORY_TOUCH_RE.search(text))
    return DecisionSupportSignals(
        is_decision_support=is_ds,
        matched_triggers=matched,
        regulatory_touch=reg,
    )


def is_decision_support_query(query: str) -> bool:
    """Boolean shortcut over :func:`classify`."""
    return classify(query).is_decision_support


__all__ = [
    "DecisionSupportSignals",
    "classify",
    "is_decision_support_query",
]
