"""Tests for the decision-support keyword classifier — Phase 1 / Step 1.4."""

from __future__ import annotations

import pytest

from app.agent.decision_support_classifier import (
    DecisionSupportSignals,
    classify,
    is_decision_support_query,
)

# ---------------------------------------------------------------------------
# Positive triggers — each of the 7 plan keywords flips the classifier on
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "Should we drill DDH-13 next?",
        "What are the next steps for resource definition?",
        "Prioritise the infill targets at section 5+50N.",
        "Recommend a drilling sequence for the eastern lens.",
        "Rank the four candidate targets by uncertainty reduction.",
        "What should we do before applying for NI 43-101 sign-off?",
        "Recommend infill spacing.",  # "Recommend" → trigger
    ],
)
def test_each_keyword_triggers_decision_support(query: str) -> None:
    assert is_decision_support_query(query) is True


def test_blank_query_is_not_decision_support() -> None:
    assert is_decision_support_query("") is False
    assert is_decision_support_query("   ") is False


# ---------------------------------------------------------------------------
# "drill" is a weak signal — alone is not enough
# ---------------------------------------------------------------------------


def test_drill_alone_in_factual_lookup_does_not_trigger() -> None:
    # Mentions the word "drill" but is clearly a factual / lookup question
    # — no decision verb companion.
    q = "How deep is drill hole DDH-07?"
    sig = classify(q)
    assert sig.is_decision_support is False


def test_drill_with_decision_verb_triggers() -> None:
    q = "Where should we plan the next drill targets?"
    sig = classify(q)
    assert sig.is_decision_support is True
    assert "should we" in sig.matched_triggers
    # "drill" should also light up given "plan ... drill targets"
    assert "drill" in sig.matched_triggers


def test_drill_program_phrase_triggers_via_prioritise() -> None:
    q = "Prioritise the next phase of the drill program."
    sig = classify(q)
    assert sig.is_decision_support is True


# ---------------------------------------------------------------------------
# Negative cases — pure factual / synthesis queries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "What is the deepest hole in this project?",
        "How many drill holes are in this project?",
        "Summarise the lithology log for DDH-07.",
        "What deposit does this project host?",
        "Integrate the assay data and drill logs for the eastern lens.",
    ],
)
def test_non_decision_queries_do_not_trigger(query: str) -> None:
    assert is_decision_support_query(query) is False


# ---------------------------------------------------------------------------
# Regulatory-touch detection
# ---------------------------------------------------------------------------


def test_regulatory_touch_resource_classification() -> None:
    sig = classify(
        "Should we apply for a Measured Resource classification on the eastern lens?"
    )
    assert sig.is_decision_support is True
    assert sig.regulatory_touch is True


def test_regulatory_touch_drilling() -> None:
    sig = classify("Recommend a drill spacing for the next program.")
    assert sig.is_decision_support is True
    assert sig.regulatory_touch is True


def test_regulatory_touch_sampling() -> None:
    sig = classify("Prioritise re-assay of batches with failed CRMs.")
    assert sig.is_decision_support is True
    assert sig.regulatory_touch is True


def test_regulatory_touch_false_for_pure_targeting_question() -> None:
    sig = classify("Rank the four candidate targets by structural complexity.")
    assert sig.is_decision_support is True
    # No NI 43-101 / classification / sampling / QA-QC terms — regulatory
    # touch stays off.
    assert sig.regulatory_touch is False


# ---------------------------------------------------------------------------
# Signals dataclass behaviour
# ---------------------------------------------------------------------------


def test_signals_dataclass_truthiness() -> None:
    pos = classify("Should we drill DDH-13?")
    neg = classify("How deep is DDH-07?")
    assert bool(pos) is True
    assert bool(neg) is False
    assert isinstance(pos, DecisionSupportSignals)
