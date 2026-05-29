"""Tests for the 6-intent classifier — Phase 2 / Step 2.2.

Includes a labeled 20-query set covering all six intents drawn from the
plan's question-types matrix (lines 422-451). Run with:

    pytest tests/test_agentic_retrieval_intent_classifier.py -q
"""

from __future__ import annotations

import pytest

from app.agent.agentic_retrieval import (
    INTENT_LABELS,
    Intent,
    IntentResult,
    classify_intent,
    classify_intent_sync,
)


# ---------------------------------------------------------------------------
# Labeled query set (20 queries × 6 intents)
# ---------------------------------------------------------------------------


# Each entry: (query, expected_intent, expected_regulatory_touch).
# Drawn from the plan's Quick-reference question-types matrix (lines 422-451)
# plus a handful of phrasings Kyle is likely to actually type.
LABELED_QUERIES: list[tuple[str, Intent, bool]] = [
    # ── factual_lookup (3) ──
    ("What is the formally correct unit name for this interval under NI 43-101?", "factual_lookup", True),
    ("What does the CRIRSCO template require for a Measured Resource classification?", "factual_lookup", True),
    ("Define unconformity-related uranium deposit.", "factual_lookup", False),

    # ── synthesis (3) ──
    ("Integrate the drill logs, assay data, and technical report for DDH-07 to DDH-12 — what is the grade continuity interpretation?", "synthesis", True),
    ("Across wells A through F, which marker beds give the most defensible correlation?", "synthesis", False),
    ("Summarise the alteration assemblage across the Triple R deposit.", "synthesis", False),

    # ── hypothesis_generation (3) ──
    ("What geological models could explain the Cu-Au anomaly at the 300m horizon?", "hypothesis_generation", False),
    ("Which alteration assemblage is present, and is it more consistent with potassic or propylitic overprint?", "hypothesis_generation", False),
    ("What are the possible causes of the apparent grade discontinuity at 145 m?", "hypothesis_generation", False),

    # ── anomaly_detection (3) ──
    ("Which assay intervals are potential outliers after screening blanks, CRMs, duplicates, and detection-limit issues?", "anomaly_detection", True),
    ("Flag any batches with failed CRM tolerance.", "anomaly_detection", True),
    ("Which batches should be rerun before interpretation continues?", "anomaly_detection", True),

    # ── uncertainty_quantification (3) ──
    ("How sensitive is the grade estimate to the capping assumption at 5 g/t Au?", "uncertainty_quantification", False),
    ("How reliable is the eastern-contact location given the data we have?", "uncertainty_quantification", False),
    ("Which age constraints are direct measurements and which are indirect correlations?", "uncertainty_quantification", False),

    # ── decision_support (5) ──
    ("Which drill targets would most reduce geological uncertainty rather than merely add data in already constrained areas?", "decision_support", True),
    ("What material documentation gaps would prevent a technically defensible resource statement under NI 43-101?", "decision_support", True),
    ("Should we apply for a Measured Resource classification on the eastern lens?", "decision_support", True),
    ("Recommend infill spacing for the next program.", "decision_support", True),
    ("Rank the four candidate targets by uncertainty reduction.", "decision_support", False),

    # ── project_summary (ADR-0007 PR-1) ──
    ("Give me a breakdown of data collection techniques by year.", "project_summary", False),
    ("Who worked on this project? Show me the contractors and geologists by campaign.", "project_summary", False),

    # ── coverage_gap (ADR-0007 PR-1) ──
    ("What data gaps do we have for this project?", "coverage_gap", False),
    ("Which holes are missing assay coverage?", "coverage_gap", False),
]


# ---------------------------------------------------------------------------
# Top-level accuracy gate
# ---------------------------------------------------------------------------


def test_labeled_set_accuracy() -> None:
    """All 20 queries classify correctly via the keyword pass alone.

    Phase 2's completion gate requires the classifier to handle the
    representative labeled set without the LLM fallback (the fallback is a
    safety net, not a primary path). If this drops below 100% we either
    re-tune the keyword sets or add the failing case to the next iteration.
    """
    wrong: list[tuple[str, Intent, Intent]] = []
    for query, expected, _ in LABELED_QUERIES:
        got = classify_intent_sync(query)
        if got.intent != expected:
            wrong.append((query, expected, got.intent))
    assert not wrong, "\n".join(
        f"  expected={e!r} got={g!r} — {q!r}" for q, e, g in wrong
    )


@pytest.mark.parametrize(
    "query,expected_intent,expected_regulatory",
    LABELED_QUERIES,
    ids=[q[:60] for q, _, _ in LABELED_QUERIES],
)
def test_per_query_classification(
    query: str, expected_intent: Intent, expected_regulatory: bool
) -> None:
    got = classify_intent_sync(query)
    assert got.intent == expected_intent, (
        f"intent mismatch: expected={expected_intent} got={got.intent} "
        f"triggers={got.matched_triggers}"
    )
    if expected_intent == "decision_support":
        assert got.regulatory_touch == expected_regulatory, (
            f"regulatory_touch mismatch: expected={expected_regulatory} "
            f"got={got.regulatory_touch}"
        )


# ---------------------------------------------------------------------------
# Defaults / edge cases
# ---------------------------------------------------------------------------


def test_blank_query_defaults_to_synthesis() -> None:
    got = classify_intent_sync("")
    assert got.intent == "synthesis"
    assert got.confidence == 0.0
    assert got.matched_triggers == ()


def test_whitespace_only_query_defaults_to_synthesis() -> None:
    got = classify_intent_sync("   \n  ")
    assert got.intent == "synthesis"
    assert got.confidence == 0.0


def test_no_keyword_match_defaults_to_synthesis() -> None:
    """Plain factual-shaped query with no trigger words → synthesis."""
    got = classify_intent_sync("Tell me about the geology of the Athabasca basin.")
    assert got.intent == "synthesis"
    assert got.confidence == 0.0


def test_all_intent_labels_round_trip() -> None:
    """Every intent in INTENT_LABELS is reachable from at least one query."""
    reached: set[Intent] = set()
    for query, _, _ in LABELED_QUERIES:
        reached.add(classify_intent_sync(query).intent)
    assert reached == set(INTENT_LABELS)


# ---------------------------------------------------------------------------
# Tiebreak: broader retrieval wins
# ---------------------------------------------------------------------------


def test_tiebreak_prefers_higher_retrieval_intent() -> None:
    """When a query carries one synthesis trigger AND one hypothesis trigger,
    hypothesis wins because the plan says 'route to the higher-retrieval
    intent' — and hypothesis ranks broader than synthesis.
    """
    # "compare" is synthesis; "what models could explain" is hypothesis.
    query = "Compare the holes and tell me what geological models could explain the anomaly."
    got = classify_intent_sync(query)
    assert got.intent == "hypothesis_generation"


def test_tiebreak_synthesis_beats_factual() -> None:
    """Synthesis ranks broader than factual_lookup."""
    query = "What is the deepest hole, and integrate across all wells."
    got = classify_intent_sync(query)
    assert got.intent == "synthesis"


# ---------------------------------------------------------------------------
# Confidence values
# ---------------------------------------------------------------------------


def test_high_confidence_when_only_one_intent_matches() -> None:
    got = classify_intent_sync("Define unconformity-related uranium deposit.")
    assert got.intent == "factual_lookup"
    assert got.confidence == pytest.approx(1.0)
    assert got.second_choice is None


def test_lower_confidence_when_multiple_intents_match() -> None:
    """A mixed-signal query has confidence strictly below 1.0."""
    query = "Integrate the assays across DDH-07 to DDH-12 and recommend the next drill targets."
    got = classify_intent_sync(query)
    assert got.confidence < 1.0
    assert got.second_choice is not None


# ---------------------------------------------------------------------------
# Decision-support regulatory_touch (carried from Phase 1.4 classifier)
# ---------------------------------------------------------------------------


def test_regulatory_touch_propagates_from_decision_support_classifier() -> None:
    got = classify_intent_sync("Recommend a drill spacing for the next program.")
    assert got.intent == "decision_support"
    assert got.regulatory_touch is True


def test_regulatory_touch_off_when_no_classification_terms() -> None:
    got = classify_intent_sync("Rank the four candidate targets by structural complexity.")
    assert got.intent == "decision_support"
    assert got.regulatory_touch is False


# ---------------------------------------------------------------------------
# LLM fallback wiring
# ---------------------------------------------------------------------------


class _FakeAsyncClient:
    """Stand-in for the vLLM HTTP client. We never actually hit it."""


@pytest.mark.asyncio
async def test_llm_fallback_not_called_when_confidence_high(monkeypatch) -> None:
    """When the keyword classifier is confident, the LLM is NOT consulted."""
    called: list[str] = []

    async def fake_call_llm(*args, **kwargs):  # pragma: no cover — must not run
        called.append("yes")
        return "synthesis"

    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)
    got = await classify_intent(
        "Define unconformity-related uranium deposit.",
        openai_http_client=_FakeAsyncClient(),
    )
    assert got.intent == "factual_lookup"
    assert got.used_llm_fallback is False
    assert called == []


@pytest.mark.asyncio
async def test_llm_fallback_called_when_no_keyword_signal(monkeypatch) -> None:
    """Query with no triggers → confidence 0 → LLM fallback runs and wins."""
    async def fake_call_llm(*args, **kwargs):
        return "hypothesis_generation\n"

    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)
    got = await classify_intent(
        "Tell me about the geology of the Athabasca basin.",
        openai_http_client=_FakeAsyncClient(),
    )
    assert got.intent == "hypothesis_generation"
    assert got.used_llm_fallback is True


@pytest.mark.asyncio
async def test_llm_fallback_unrecognised_label_ignored(monkeypatch) -> None:
    """Garbled LLM output → keep the keyword answer (or default)."""
    async def fake_call_llm(*args, **kwargs):
        return "I think this is probably synthesis or maybe hypothesis"

    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)
    got = await classify_intent(
        "Tell me about the geology of the Athabasca basin.",
        openai_http_client=_FakeAsyncClient(),
    )
    # First word "I" is not a valid label → fall back to keyword answer
    # (synthesis default).
    assert got.intent == "synthesis"
    assert got.used_llm_fallback is False


@pytest.mark.asyncio
async def test_llm_fallback_swallows_call_exceptions(monkeypatch) -> None:
    async def boom(*args, **kwargs):
        raise RuntimeError("vLLM unreachable")

    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", boom)
    got = await classify_intent(
        "Tell me about the geology of the Athabasca basin.",
        openai_http_client=_FakeAsyncClient(),
    )
    # Falls through to the keyword answer / default. No raise.
    assert got.intent == "synthesis"
    assert got.used_llm_fallback is False


@pytest.mark.asyncio
async def test_no_client_no_llm_call(monkeypatch) -> None:
    """When no HTTP client is provided, the LLM path is bypassed entirely."""
    called: list[str] = []

    async def fake_call_llm(*args, **kwargs):  # pragma: no cover
        called.append("yes")
        return "factual_lookup"

    import app.agent.llm_calls as _llm_mod

    monkeypatch.setattr(_llm_mod, "_call_llm", fake_call_llm)
    got = await classify_intent(
        "Tell me about the geology of the Athabasca basin.",
        openai_http_client=None,
    )
    assert got.used_llm_fallback is False
    assert called == []


# ---------------------------------------------------------------------------
# Result dataclass shape
# ---------------------------------------------------------------------------


def test_intent_result_is_frozen_dataclass() -> None:
    r = classify_intent_sync("Rank the targets.")
    assert isinstance(r, IntentResult)
    with pytest.raises(Exception):  # dataclass(frozen=True) → FrozenInstanceError
        r.intent = "synthesis"  # type: ignore[misc]
