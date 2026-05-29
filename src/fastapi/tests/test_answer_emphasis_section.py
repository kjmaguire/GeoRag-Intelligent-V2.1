"""Tests for the per-intent answer-emphasis prompt fragments — Phase 2 / Step 2.5."""

from __future__ import annotations

import pytest

from app.agent.agentic_retrieval import INTENT_LABELS, profile_for_intent
from app.agent.prompts.answer_emphasis_section import (
    ANOMALY_TABLE_EMPHASIS,
    COMPETING_HYPOTHESES_EMPHASIS,
    EXACT_CITATION_EMPHASIS,
    RANKED_OPTIONS_EMPHASIS,
    SYNTHESIS_WITH_CONFLICTS_EMPHASIS,
    UNCERTAINTY_DRIVERS_EMPHASIS,
    fragment_for,
)


# ---------------------------------------------------------------------------
# Coverage — every intent's emphasis tag resolves to a non-empty fragment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("intent", list(INTENT_LABELS))
def test_every_intent_emphasis_has_a_fragment(intent) -> None:
    profile = profile_for_intent(intent)
    fragment = fragment_for(profile.answer_emphasis)
    assert fragment.strip(), f"empty fragment for intent={intent!r}"


def test_unknown_emphasis_returns_empty_string() -> None:
    assert fragment_for("does_not_exist") == ""  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Content guarantees per the plan's Step 2.3 / Step 2.5 contracts
# ---------------------------------------------------------------------------


def test_exact_citation_emphasises_clause_page_standard() -> None:
    f = EXACT_CITATION_EMPHASIS.lower()
    assert "clause" in f
    assert "page" in f
    assert "jurisdiction" in f or "version" in f


def test_synthesis_requires_conflict_subsection_header() -> None:
    """The plan: 'Conflicting evidence called out as a named sub-section
    under interpretations'."""
    assert "### Conflicting evidence" in SYNTHESIS_WITH_CONFLICTS_EMPHASIS
    assert (
        "_None detected in the retrieved corpus._"
        in SYNTHESIS_WITH_CONFLICTS_EMPHASIS
    )


def test_competing_hypotheses_requires_two_hypotheses() -> None:
    """The plan: 'Generate ≥2 hypotheses' + 'supporting + disconfirming
    evidence for each'."""
    f = COMPETING_HYPOTHESES_EMPHASIS.lower()
    assert "at least two" in f or "≥2" in f or "two interpretations" in f
    assert "evidence for" in f
    assert "evidence against" in f
    # Disconfirming retrieval is wired by tag.
    assert "search_documents_adversarial" in COMPETING_HYPOTHESES_EMPHASIS


def test_anomaly_emphasises_geological_vs_artifact_and_reassay() -> None:
    """Phase 2 completion gate: anomaly subgraph must emit a
    geological-vs-artifact classification + re-assay recommendation
    (plan Step 2.3 anomaly subgraph, plan Step 2.5 gate)."""
    f = ANOMALY_TABLE_EMPHASIS.lower()
    assert "geological signal" in f
    assert "qa/qc artifact" in f
    assert "re-assay" in f
    # The observation-side table shape is required.
    assert "interval" in f
    assert "threshold" in f
    assert "deviation" in f


def test_uncertainty_drivers_emphasises_sensitivity_and_range() -> None:
    f = UNCERTAINTY_DRIVERS_EMPHASIS.lower()
    assert "sensitivity" in f
    assert "range" in f


def test_ranked_options_references_phase14_rules() -> None:
    """The decision-support template (Phase 1.4) is referenced explicitly,
    so the LLM doesn't get conflicting instructions from two fragments."""
    f = RANKED_OPTIONS_EMPHASIS
    # Cross-reference to the Phase 1.4 rules (15-17).
    assert "rules 15-17" in f


# ---------------------------------------------------------------------------
# Integration: assemble_node appends the right fragment for each intent
# ---------------------------------------------------------------------------


def test_each_profile_emphasis_resolves_consistently() -> None:
    """Every retrieval profile's emphasis tag must point at the matching
    fragment we expect from the plan's table.
    """
    expectations = {
        "factual_lookup": EXACT_CITATION_EMPHASIS,
        "synthesis": SYNTHESIS_WITH_CONFLICTS_EMPHASIS,
        "hypothesis_generation": COMPETING_HYPOTHESES_EMPHASIS,
        "anomaly_detection": ANOMALY_TABLE_EMPHASIS,
        "uncertainty_quantification": UNCERTAINTY_DRIVERS_EMPHASIS,
        "decision_support": RANKED_OPTIONS_EMPHASIS,
    }
    for intent, expected in expectations.items():
        actual = fragment_for(profile_for_intent(intent).answer_emphasis)  # type: ignore[arg-type]
        assert actual == expected, f"intent={intent} got wrong fragment"
