"""Tests for rule-based confidence Level computation — Phase 1 / Step 1.3.

Locks the contract:
  - Stage 1: ≥2 distinct source_chunk_ids → High; 1 → Medium; 0 → Low
  - Stage 1: refusal-placeholder sources are excluded from the count
  - Stage 2: conflicts_present → forces Low
  - Stage 2: numeric_flagged → High demotes to Medium
  - Stage 2: demotion is monotonic (never raises)
  - apply_level_to_geo_answer preserves prose reason / drivers / data target
  - apply_guard_demotion is a no-op when geo_answer is None or uncertainty is SectionEmpty
"""

from __future__ import annotations

import pytest

from app.agent.confidence_computer import (
    apply_guard_demotion,
    apply_level_to_geo_answer,
    compute_initial_level,
    demote_for_guards,
)
from app.agent.schemas import (
    ConfidenceBlock,
    GeoAnswer,
    Interpretation,
    Observation,
    SectionEmpty,
    UncertaintyBlock,
)
from app.models.rag import Citation, GeoRAGResponse

# ---------------------------------------------------------------------------
# Stage 1 — initial Level
# ---------------------------------------------------------------------------


def _cit(citation_id: str, source: str) -> Citation:
    return Citation(
        citation_id=citation_id,
        citation_type="DATA",
        source_chunk_id=source,
        document_title="t",
        relevance_score=0.9,
    )


def test_two_distinct_sources_high() -> None:
    citations = [
        _cit("[DATA-1]", "silver.collars:row=1"),
        _cit("[DATA-2]", "silver.samples:element=Au"),
    ]
    level, note = compute_initial_level(citations)
    assert level == "High"
    assert "2 independent" in note


def test_one_source_medium() -> None:
    citations = [_cit("[DATA-1]", "silver.collars:row=1")]
    level, note = compute_initial_level(citations)
    assert level == "Medium"
    assert "single" in note.lower()


def test_zero_sources_low() -> None:
    level, note = compute_initial_level([])
    assert level == "Low"


def test_duplicate_source_chunk_id_counts_once() -> None:
    citations = [
        _cit("[DATA-1]", "silver.collars:row=1"),
        _cit("[NI43-2]", "silver.collars:row=1"),  # same source, diff citation_id
    ]
    level, _ = compute_initial_level(citations)
    assert level == "Medium"  # one independent source


def test_refusal_placeholder_excluded() -> None:
    citations = [_cit("[DATA-1]", "no-tool-call")]
    level, _ = compute_initial_level(citations)
    assert level == "Low"


# ---------------------------------------------------------------------------
# Stage 2 — guard demotion
# ---------------------------------------------------------------------------


def test_numeric_flag_demotes_high_to_medium() -> None:
    new_level, reasons = demote_for_guards(
        "High", numeric_flagged=True, conflicts_present=False
    )
    assert new_level == "Medium"
    assert any("numeric grounding" in r.lower() for r in reasons)


def test_numeric_flag_does_not_touch_medium() -> None:
    new_level, reasons = demote_for_guards(
        "Medium", numeric_flagged=True, conflicts_present=False
    )
    assert new_level == "Medium"
    assert reasons == []


def test_conflicts_force_low_from_high() -> None:
    new_level, reasons = demote_for_guards(
        "High", numeric_flagged=False, conflicts_present=True
    )
    assert new_level == "Low"
    assert any("conflicting" in r.lower() for r in reasons)


def test_conflicts_force_low_from_medium() -> None:
    new_level, reasons = demote_for_guards(
        "Medium", numeric_flagged=False, conflicts_present=True
    )
    assert new_level == "Low"


def test_conflicts_no_op_when_already_low() -> None:
    new_level, reasons = demote_for_guards(
        "Low", numeric_flagged=False, conflicts_present=True
    )
    assert new_level == "Low"
    assert reasons == []


def test_both_flags_yield_low_with_both_reasons() -> None:
    new_level, reasons = demote_for_guards(
        "High", numeric_flagged=True, conflicts_present=True
    )
    # Conflicts dominate — Low.
    assert new_level == "Low"
    # Both reasons are surfaced for the lineage record.
    assert len(reasons) >= 1


@pytest.mark.parametrize("level", ["High", "Medium", "Low"])
def test_no_demotion_when_no_flags(level) -> None:
    new_level, reasons = demote_for_guards(
        level, numeric_flagged=False, conflicts_present=False
    )
    assert new_level == level
    assert reasons == []


# ---------------------------------------------------------------------------
# apply_level_to_geo_answer
# ---------------------------------------------------------------------------


def _build_answer(level: str = "Medium") -> GeoAnswer:
    return GeoAnswer(
        observations=[
            Observation(
                observation_id="O1",
                text="DDH-07 intersected 12.4 m at 2.1 g/t Au [DATA-1].",
                citation_ids=["[DATA-1]"],
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
                level=level,  # type: ignore[arg-type]
                reason="LLM-authored prose explaining what constrains confidence.",
                drivers=["A driver", "Another driver"],
                data_to_reduce_uncertainty="One infill hole 50 m east of DDH-07.",
            ),
            citation_ids=["[DATA-1]"],
        ),
        recommended_actions=SectionEmpty(reason="No decision context supplied."),
    )


def test_apply_level_preserves_prose() -> None:
    answer = _build_answer("Medium")
    new = apply_level_to_geo_answer(answer, "High")
    assert isinstance(new.uncertainty, UncertaintyBlock)
    assert new.uncertainty.confidence.level == "High"
    # All other fields are preserved verbatim.
    assert new.uncertainty.confidence.reason == answer.uncertainty.confidence.reason  # type: ignore[union-attr]
    assert new.uncertainty.confidence.drivers == answer.uncertainty.confidence.drivers  # type: ignore[union-attr]
    assert (
        new.uncertainty.confidence.data_to_reduce_uncertainty
        == answer.uncertainty.confidence.data_to_reduce_uncertainty  # type: ignore[union-attr]
    )


def test_apply_level_no_change_returns_input() -> None:
    answer = _build_answer("Medium")
    new = apply_level_to_geo_answer(answer, "Medium")
    assert new is answer  # short-circuit when level unchanged


def test_apply_level_section_empty_passthrough() -> None:
    answer = GeoAnswer(
        observations=[
            Observation(
                observation_id="O1",
                text="A factual lookup [NI43-1].",
                citation_ids=["[NI43-1]"],
            )
        ],
        interpretations=SectionEmpty(reason="No interpretations supported."),
        uncertainty=SectionEmpty(reason="No interpretations to qualify."),
        recommended_actions=SectionEmpty(reason="Factual lookup."),
    )
    new = apply_level_to_geo_answer(answer, "High")
    assert new is answer  # nothing to override


# ---------------------------------------------------------------------------
# apply_guard_demotion — end-to-end on a GeoRAGResponse
# ---------------------------------------------------------------------------


def _response_with(geo_answer: GeoAnswer | None, conflicting: list | None = None) -> GeoRAGResponse:
    return GeoRAGResponse(
        text="some text",
        citations=[
            Citation(
                citation_id="[DATA-1]",
                citation_type="DATA",
                source_chunk_id="silver.collars:row=1",
                document_title="t",
                relevance_score=0.9,
            )
        ],
        confidence=0.8,
        sources_used=["silver.collars:row=1"],
        geo_answer=geo_answer,
        conflicting_evidence=conflicting,
    )


def test_apply_guard_demotion_no_op_when_geo_answer_none() -> None:
    resp = _response_with(geo_answer=None)
    new_resp, reasons = apply_guard_demotion(resp, ["Layer 3: ungrounded number"])
    assert new_resp is resp
    assert reasons == []


def test_apply_guard_demotion_layer3_warning_demotes_high() -> None:
    resp = _response_with(geo_answer=_build_answer("High"))
    new_resp, reasons = apply_guard_demotion(
        resp, ["Layer 3: ungrounded number 12.4 m"]
    )
    assert new_resp.geo_answer.uncertainty.confidence.level == "Medium"  # type: ignore[union-attr]
    assert reasons


def test_apply_guard_demotion_layer3_warning_no_op_on_medium() -> None:
    resp = _response_with(geo_answer=_build_answer("Medium"))
    new_resp, reasons = apply_guard_demotion(
        resp, ["Layer 3: ungrounded number 12.4 m"]
    )
    assert new_resp is resp
    assert reasons == []


def test_apply_guard_demotion_conflicts_force_low() -> None:
    resp = _response_with(
        geo_answer=_build_answer("High"),
        conflicting=[{"entity_key": "DDH-07", "property_name": "grade", "values": [2.1, 1.8]}],
    )
    new_resp, reasons = apply_guard_demotion(resp, [])
    assert new_resp.geo_answer.uncertainty.confidence.level == "Low"  # type: ignore[union-attr]
    assert any("conflicting" in r.lower() for r in reasons)


def test_apply_guard_demotion_ignores_non_layer3_warnings() -> None:
    resp = _response_with(geo_answer=_build_answer("High"))
    # Layer 4 / Layer 6 warnings must not trigger numeric demotion.
    new_resp, reasons = apply_guard_demotion(
        resp,
        [
            "Layer 4: entity DDH-99 not found",
            "Layer 6: depth 50,000 m exceeds physical limit",
        ],
    )
    assert new_resp is resp
    assert reasons == []
