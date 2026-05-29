"""Tests for the context envelope + envelope-driven routing — Phase 2 / Step 2.4."""

from __future__ import annotations

import textwrap
from typing import Any

import pytest

from app.agent.agentic_retrieval import (
    AgenticRetrievalState,
    ContextEnvelope,
    DEFAULT_REPORTING_CODE,
    EMPTY_ENVELOPE,
    apply_envelope_overrides,
    profile_for_intent,
    unspecified_field_descriptions,
)
from app.agent.agentic_retrieval.nodes import (
    _attach_envelope_notes_to_uncertainty,
    route_node,
)
from app.agent.schemas import (
    ConfidenceBlock,
    GeoAnswer,
    Interpretation,
    Observation,
    RecommendedAction,
    SectionEmpty,
    UncertaintyBlock,
)
from app.models.rag import Citation, GeoRAGResponse


# ---------------------------------------------------------------------------
# ContextEnvelope basics
# ---------------------------------------------------------------------------


def test_empty_envelope_has_all_fields_unspecified() -> None:
    env = ContextEnvelope()
    assert env.populated_fields() == set()
    assert len(env.unspecified_fields()) == 12


def test_populated_envelope_reports_populated_fields() -> None:
    env = ContextEnvelope(
        area_of_interest="Within 5 km of DDH-07",
        crs_epsg=26913,
        specific_objects=["DDH-07", "DDH-12"],
        reporting_code="NI 43-101",
    )
    populated = env.populated_fields()
    assert populated == {
        "area_of_interest",
        "crs_epsg",
        "specific_objects",
        "reporting_code",
    }


def test_empty_list_counts_as_unspecified() -> None:
    env = ContextEnvelope(specific_objects=[])
    assert "specific_objects" not in env.populated_fields()


def test_blank_string_counts_as_unspecified() -> None:
    env = ContextEnvelope(area_of_interest="")
    assert "area_of_interest" not in env.populated_fields()


# ---------------------------------------------------------------------------
# Reporting-code default
# ---------------------------------------------------------------------------


def test_reporting_code_defaults_when_unspecified() -> None:
    env = ContextEnvelope()
    code, was_defaulted = env.effective_reporting_code()
    assert code == DEFAULT_REPORTING_CODE
    assert was_defaulted is True


def test_reporting_code_passes_through_when_set() -> None:
    env = ContextEnvelope(reporting_code="JORC")
    code, was_defaulted = env.effective_reporting_code()
    assert code == "JORC"
    assert was_defaulted is False


# ---------------------------------------------------------------------------
# Envelope routing — decision_support demotion
# ---------------------------------------------------------------------------


def test_decision_support_demotes_to_synthesis_when_no_decision_context() -> None:
    """The plan's Step 2.4 table: 'Decision to support' missing →
    route to synthesis instead of decision support."""
    decision = apply_envelope_overrides("decision_support", ContextEnvelope())
    assert decision.effective_intent == "synthesis"
    assert decision.override_reason
    assert any("decision context" in n.lower() for n in decision.notes)


def test_decision_support_stays_when_decision_context_supplied() -> None:
    env = ContextEnvelope(decision_to_support="Rank infill drill targets.")
    decision = apply_envelope_overrides("decision_support", env)
    assert decision.effective_intent == "decision_support"
    assert decision.override_reason is None


def test_non_decision_intents_are_never_overridden() -> None:
    for intent in (
        "factual_lookup",
        "synthesis",
        "hypothesis_generation",
        "anomaly_detection",
        "uncertainty_quantification",
    ):
        decision = apply_envelope_overrides(intent, ContextEnvelope())  # type: ignore[arg-type]
        assert decision.effective_intent == intent
        assert decision.override_reason is None


# ---------------------------------------------------------------------------
# Envelope notes — always surface unspecified-side-effects
# ---------------------------------------------------------------------------


def test_empty_envelope_emits_all_relevant_notes() -> None:
    decision = apply_envelope_overrides("synthesis", ContextEnvelope())
    notes_joined = " ".join(decision.notes).lower()
    # The plan's 5 explicit routing rules each produce a note.
    assert "area of interest unspecified" in notes_joined
    assert "crs / datum unspecified" in notes_joined
    assert "reporting code unspecified" in notes_joined
    assert "qa/qc constraints unspecified" in notes_joined


def test_populated_envelope_suppresses_corresponding_notes() -> None:
    env = ContextEnvelope(
        area_of_interest="Within 5 km of DDH-07",
        crs_epsg=26913,
        reporting_code="NI 43-101",
        qaqc_constraints="Exclude batches failing CRM tolerance.",
    )
    decision = apply_envelope_overrides("synthesis", env)
    notes_joined = " ".join(decision.notes).lower()
    assert "area of interest" not in notes_joined
    assert "crs / datum" not in notes_joined
    assert "reporting code" not in notes_joined
    assert "qa/qc constraints" not in notes_joined


# ---------------------------------------------------------------------------
# Field descriptions
# ---------------------------------------------------------------------------


def test_unspecified_field_descriptions_covers_every_unspecified_field() -> None:
    env = ContextEnvelope(crs_epsg=26913)  # only CRS set
    descs = unspecified_field_descriptions(env)
    # 11 unspecified fields → 11 descriptions.
    assert len(descs) == 11
    joined = " ".join(descs).lower()
    assert "crs / datum unspecified" not in joined
    assert "area of interest unspecified" in joined


def test_none_envelope_treated_as_empty() -> None:
    descs_none = unspecified_field_descriptions(None)
    descs_empty = unspecified_field_descriptions(EMPTY_ENVELOPE)
    assert descs_none == descs_empty


# ---------------------------------------------------------------------------
# Route node + envelope interaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_node_applies_demotion(monkeypatch) -> None:
    state = AgenticRetrievalState(
        query="Should we drill DDH-13 next?",
        deps=object(),  # unused in this node
        context_envelope=ContextEnvelope(),  # no decision_to_support
    )
    state = state.model_copy(
        update={"intent": "decision_support", "intent_result": None}
    )
    update = await route_node(state)
    assert update["effective_intent"] == "synthesis"
    assert update["envelope_override_reason"]
    assert any("decision context" in n.lower() for n in update["envelope_notes"])
    # Profile is now synthesis's, not decision_support's.
    assert update["retrieval_profile"].intent == "synthesis"


@pytest.mark.asyncio
async def test_route_node_no_demotion_when_decision_context_provided() -> None:
    state = AgenticRetrievalState(
        query="Should we drill DDH-13 next?",
        deps=object(),
        context_envelope=ContextEnvelope(
            decision_to_support="Choose between PLS-22-08 and PLS-22-12 for the next program."
        ),
    )
    state = state.model_copy(
        update={"intent": "decision_support", "intent_result": None}
    )
    update = await route_node(state)
    assert update["effective_intent"] == "decision_support"
    assert update["envelope_override_reason"] is None
    assert update["retrieval_profile"].intent == "decision_support"


# ---------------------------------------------------------------------------
# Assemble-side: envelope notes surface in OIUR uncertainty section
# ---------------------------------------------------------------------------


def _full_oiur_response() -> GeoRAGResponse:
    answer = GeoAnswer(
        observations=[
            Observation(
                observation_id="O1",
                text="DDH-07 intersected 12.4 m at 2.1 g/t Au [NI43-1].",
                citation_ids=["[NI43-1]"],
            )
        ],
        interpretations=[
            Interpretation(
                interpretation_id="I1",
                text="Continuity is plausible.",
                supporting_observation_ids=["O1"],
            )
        ],
        uncertainty=UncertaintyBlock(
            confidence=ConfidenceBlock(
                level="Medium",
                reason="Single hole constrains the contact.",
                data_to_reduce_uncertainty="One infill hole 100 m east of DDH-07.",
            ),
            missing_or_conflicting=["Pre-existing note kept as-is."],
            citation_ids=["[NI43-1]"],
        ),
        recommended_actions=SectionEmpty(reason="No decision context."),
    )
    return GeoRAGResponse(
        text="some text",
        citations=[
            Citation(
                citation_id="[NI43-1]",
                citation_type="NI43",
                source_chunk_id="report:1:section:3.2:chunk:c1",
                document_title="t",
                relevance_score=0.9,
            )
        ],
        confidence=0.7,
        sources_used=["report:1:section:3.2:chunk:c1"],
        geo_answer=answer,
    )


def test_attach_envelope_notes_merges_into_uncertainty() -> None:
    resp = _full_oiur_response()
    notes = ["AOI unspecified — retrieved project-wide."]
    descriptions = ["CRS / datum unspecified — no spatial filtering applied."]
    new = _attach_envelope_notes_to_uncertainty(
        resp,
        envelope_notes=notes,
        unspecified_descriptions=descriptions,
    )
    assert new is not resp  # new model
    missing = new.geo_answer.uncertainty.missing_or_conflicting  # type: ignore[union-attr]
    assert "Pre-existing note kept as-is." in missing  # preserved
    assert any("AOI unspecified" in m for m in missing)
    assert any("CRS / datum" in m for m in missing)


def test_attach_envelope_notes_dedups() -> None:
    resp = _full_oiur_response()
    # Same note in both lists — should appear once.
    same = "AOI unspecified — retrieved project-wide."
    new = _attach_envelope_notes_to_uncertainty(
        resp,
        envelope_notes=[same],
        unspecified_descriptions=[same],
    )
    matches = [
        m
        for m in new.geo_answer.uncertainty.missing_or_conflicting  # type: ignore[union-attr]
        if "AOI unspecified" in m
    ]
    assert len(matches) == 1


def test_attach_envelope_notes_skips_when_geo_answer_none() -> None:
    resp = _full_oiur_response().model_copy(update={"geo_answer": None})
    new = _attach_envelope_notes_to_uncertainty(
        resp,
        envelope_notes=["x"],
        unspecified_descriptions=["y"],
    )
    assert new is resp


def test_attach_envelope_notes_skips_when_uncertainty_section_empty() -> None:
    """An OIUR answer with no interpretations has uncertainty = SectionEmpty;
    we cannot append envelope notes to a SectionEmpty marker — skip cleanly."""
    answer = GeoAnswer(
        observations=[
            Observation(
                observation_id="O1",
                text="x [NI43-1].",
                citation_ids=["[NI43-1]"],
            )
        ],
        interpretations=SectionEmpty(reason="Factual lookup."),
        uncertainty=SectionEmpty(reason="No interpretations to qualify."),
        recommended_actions=SectionEmpty(reason="Factual lookup."),
    )
    resp = _full_oiur_response().model_copy(update={"geo_answer": answer})
    new = _attach_envelope_notes_to_uncertainty(
        resp,
        envelope_notes=["x"],
        unspecified_descriptions=["y"],
    )
    assert new is resp


def test_attach_envelope_notes_no_op_when_no_notes() -> None:
    resp = _full_oiur_response()
    new = _attach_envelope_notes_to_uncertainty(
        resp,
        envelope_notes=[],
        unspecified_descriptions=[],
    )
    assert new is resp
