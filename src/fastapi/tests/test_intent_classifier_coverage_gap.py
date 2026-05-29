"""ADR-0007 PR-1 — coverage_gap intent classifier coverage.

Verifies that gap / missing-data queries route to coverage_gap rather
than falling through to synthesis. Run with:

    pytest tests/test_intent_classifier_coverage_gap.py -q
"""

from __future__ import annotations

import pytest

from app.agent.agentic_retrieval import INTENT_LABELS, classify_intent_sync


COVERAGE_GAP_QUERIES: tuple[str, ...] = (
    "What data gaps do we have for this project?",
    "Which holes are missing assay coverage?",
    "Where is our coverage incomplete?",
    "Show me the holes in the data — what haven't we collected?",
    "What's missing for this project? Any under-sampled dimensions?",
    "Are there ingest gaps between bronze and silver?",
)


@pytest.mark.parametrize("query", COVERAGE_GAP_QUERIES, ids=lambda q: q[:48])
def test_coverage_gap_intent_classification(query: str) -> None:
    """Each query routes to coverage_gap, not synthesis."""
    got = classify_intent_sync(query)
    assert got.intent == "coverage_gap", (
        f"expected coverage_gap; got {got.intent} (triggers={got.matched_triggers})"
    )


def test_coverage_gap_is_in_intent_labels() -> None:
    """ADR-0007 guarantees coverage_gap is a first-class intent."""
    assert "coverage_gap" in INTENT_LABELS


def test_coverage_gap_does_not_match_documentation_gap_decision_query() -> None:
    """A decision-support query with 'documentation gaps' should NOT be
    pulled into coverage_gap.

    The decision_support classifier has a stronger signal ('would prevent'
    + 'NI 43-101' + 'documentation gaps') so the tiebreak still routes
    to decision_support. coverage_gap is the *general* missing-data path,
    not the regulatory-defensibility path.
    """
    got = classify_intent_sync(
        "What material documentation gaps would prevent a technically "
        "defensible resource statement under NI 43-101?"
    )
    assert got.intent == "decision_support"


def test_coverage_gap_does_not_false_fire_on_uncertainty_query() -> None:
    """Uncertainty queries stay in uncertainty_quantification."""
    got = classify_intent_sync(
        "How sensitive is the grade estimate to the capping assumption?"
    )
    assert got.intent == "uncertainty_quantification"


def test_coverage_gap_matched_triggers_non_empty() -> None:
    """When coverage_gap fires, the trigger list reports what matched."""
    got = classify_intent_sync(
        "What data gaps exist in our coverage for this project?"
    )
    assert got.intent == "coverage_gap"
    assert got.matched_triggers, "matched_triggers should be non-empty for telemetry"
