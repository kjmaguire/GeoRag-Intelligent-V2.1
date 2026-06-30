"""Tests for the OIUR ``GeoAnswer`` schema — Phase 1 / Step 1.2.

Locks the contract:
  - empty observations → ValueError (must route to refusal path)
  - all four sections populated for a normal answer
  - partial-evidence sections use ``SectionEmpty(reason=...)``, never bare empty lists
  - recommended_actions must have ≥1 evidence-tied action
  - confidence ``data_to_reduce_uncertainty`` rejects generic stand-ins
  - cross-section id refs (interpretations → observations, actions → observations) are validated
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.agent.schemas import (
    GEO_ANSWER_SCHEMA_VERSION,
    ConfidenceBlock,
    GeoAnswer,
    Interpretation,
    Observation,
    RecommendedAction,
    SectionEmpty,
    UncertaintyBlock,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _obs(n: int = 1, citation: str = "[DATA-1]") -> Observation:
    return Observation(
        observation_id=f"O{n}",
        text=f"Drill hole DDH-{n:02d} intersected 12.4 m at 2.1 g/t Au.",
        citation_ids=[citation],
    )


def _interp(n: int, supports: list[str]) -> Interpretation:
    return Interpretation(
        interpretation_id=f"I{n}",
        text="Grade continuity between collared holes is plausible.",
        supporting_observation_ids=supports,
    )


def _conf(level: str = "Medium") -> ConfidenceBlock:
    return ConfidenceBlock(
        level=level,  # type: ignore[arg-type]
        reason="Two holes constrain the eastern contact; western limit inferred from one surface sample.",
        drivers=["Single-source western limit", "No infill between DDH-07 and DDH-12"],
        data_to_reduce_uncertainty=(
            "One infill hole between DDH-07 and DDH-12 would resolve the grade continuity question."
        ),
    )


def _uncertainty() -> UncertaintyBlock:
    return UncertaintyBlock(
        confidence=_conf(),
        missing_or_conflicting=["No CRM results recorded for batch B-2024-17."],
        citation_ids=["[DATA-1]"],
    )


def _action(rank: int = 1, citation: str = "[DATA-1]") -> RecommendedAction:
    return RecommendedAction(
        rank=rank,
        action="Drill one infill hole between DDH-07 and DDH-12.",
        rationale="Resolves the grade-continuity gap identified in O1.",
        citation_ids=[citation],
        supporting_observation_ids=["O1"],
        expected_information_gain="Confirms or refutes lateral continuity at the 250 m spacing.",
        risk="Adds ~3 weeks to the program and ~$120k cost.",
    )


# ---------------------------------------------------------------------------
# Happy path — all four sections populated
# ---------------------------------------------------------------------------


def test_full_oiur_answer_validates() -> None:
    ans = GeoAnswer(
        observations=[_obs(1), _obs(2, "[NI43-2]")],
        interpretations=[_interp(1, ["O1", "O2"])],
        uncertainty=_uncertainty(),
        recommended_actions=[_action()],
    )
    assert ans.schema_version == GEO_ANSWER_SCHEMA_VERSION
    assert ans.cited_marker_ids() == {"[DATA-1]", "[NI43-2]"}


def test_competing_interpretations_validate() -> None:
    ans = GeoAnswer(
        observations=[_obs(1)],
        interpretations=[
            Interpretation(
                interpretation_id="I1",
                text="Potassic alteration overprint.",
                supporting_observation_ids=["O1"],
                competing_with=["I2"],
            ),
            Interpretation(
                interpretation_id="I2",
                text="Propylitic alteration assemblage.",
                supporting_observation_ids=["O1"],
                competing_with=["I1"],
            ),
        ],
        uncertainty=_uncertainty(),
        recommended_actions=[_action()],
    )
    assert len(ans.interpretations) == 2  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Refusal-path contract — empty observations must fail
# ---------------------------------------------------------------------------


def test_empty_observations_list_rejected() -> None:
    with pytest.raises(ValidationError, match="empty observations|empty list"):
        GeoAnswer(
            observations=[],
            interpretations=SectionEmpty(reason="No evidence."),
            uncertainty=SectionEmpty(reason="No evidence."),
            recommended_actions=SectionEmpty(reason="No evidence."),
        )


def test_section_empty_on_observations_rejected() -> None:
    with pytest.raises(ValidationError, match="observations cannot be SectionEmpty"):
        GeoAnswer(
            observations=SectionEmpty(reason="No retrieved chunks."),
            interpretations=SectionEmpty(reason="No evidence."),
            uncertainty=SectionEmpty(reason="No evidence."),
            recommended_actions=SectionEmpty(reason="No evidence."),
        )


# ---------------------------------------------------------------------------
# Partial-evidence contract — empty lists rejected; SectionEmpty required
# ---------------------------------------------------------------------------


def test_factual_lookup_with_no_decision_context_uses_section_empty() -> None:
    """Factual lookup: observations + interpretations present, but no
    decision context → recommended_actions = SectionEmpty(reason=...).
    """
    ans = GeoAnswer(
        observations=[_obs(1, "[NI43-1]")],
        interpretations=[_interp(1, ["O1"])],
        uncertainty=_uncertainty(),
        recommended_actions=SectionEmpty(
            reason="The query was a factual lookup; no decision context was supplied."
        ),
    )
    assert isinstance(ans.recommended_actions, SectionEmpty)


def test_empty_recommended_actions_list_rejected() -> None:
    with pytest.raises(ValidationError, match="recommended_actions cannot be an empty list"):
        GeoAnswer(
            observations=[_obs(1)],
            interpretations=[_interp(1, ["O1"])],
            uncertainty=_uncertainty(),
            recommended_actions=[],
        )


def test_empty_interpretations_list_rejected() -> None:
    with pytest.raises(ValidationError, match="interpretations cannot be an empty list"):
        GeoAnswer(
            observations=[_obs(1)],
            interpretations=[],
            uncertainty=SectionEmpty(
                reason="No interpretations supported by the corpus."
            ),
            recommended_actions=SectionEmpty(reason="No decision context."),
        )


def test_uncertainty_section_empty_only_when_interpretations_empty() -> None:
    with pytest.raises(
        ValidationError,
        match="uncertainty cannot be SectionEmpty when interpretations are present",
    ):
        GeoAnswer(
            observations=[_obs(1)],
            interpretations=[_interp(1, ["O1"])],
            uncertainty=SectionEmpty(reason="Skipped."),
            recommended_actions=SectionEmpty(reason="No decision context."),
        )


# ---------------------------------------------------------------------------
# Recommended actions — ≥1 must be evidence-tied
# ---------------------------------------------------------------------------


def test_actions_without_any_citations_rejected() -> None:
    with pytest.raises(
        ValidationError, match="at least one action with non-empty citation_ids"
    ):
        GeoAnswer(
            observations=[_obs(1)],
            interpretations=[_interp(1, ["O1"])],
            uncertainty=_uncertainty(),
            recommended_actions=[
                RecommendedAction(
                    rank=1,
                    action="Re-log core.",
                    rationale="To resolve uncertainty.",
                    citation_ids=[],
                )
            ],
        )


def test_actions_with_non_contiguous_ranks_rejected() -> None:
    with pytest.raises(ValidationError, match="ranks must be contiguous"):
        GeoAnswer(
            observations=[_obs(1)],
            interpretations=[_interp(1, ["O1"])],
            uncertainty=_uncertainty(),
            recommended_actions=[
                _action(rank=1),
                _action(rank=3, citation="[NI43-2]"),
            ],
        )


# ---------------------------------------------------------------------------
# Cross-section ref integrity
# ---------------------------------------------------------------------------


def test_interpretation_referencing_unknown_observation_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown observation ids"):
        GeoAnswer(
            observations=[_obs(1)],
            interpretations=[_interp(1, ["O2"])],
            uncertainty=_uncertainty(),
            recommended_actions=[_action()],
        )


def test_action_referencing_unknown_observation_rejected() -> None:
    action = _action()
    action_bad = action.model_copy(update={"supporting_observation_ids": ["O7"]})
    with pytest.raises(ValidationError, match="unknown observation ids"):
        GeoAnswer(
            observations=[_obs(1)],
            interpretations=[_interp(1, ["O1"])],
            uncertainty=_uncertainty(),
            recommended_actions=[action_bad],
        )


def test_duplicate_observation_ids_rejected() -> None:
    with pytest.raises(ValidationError, match="observation_ids must be unique"):
        GeoAnswer(
            observations=[_obs(1), _obs(1, "[DATA-2]")],
            interpretations=[_interp(1, ["O1"])],
            uncertainty=_uncertainty(),
            recommended_actions=[_action()],
        )


def test_competing_with_unknown_interpretation_rejected() -> None:
    with pytest.raises(ValidationError, match="competes with unknown interpretation ids"):
        GeoAnswer(
            observations=[_obs(1)],
            interpretations=[
                Interpretation(
                    interpretation_id="I1",
                    text="Some interpretation.",
                    supporting_observation_ids=["O1"],
                    competing_with=["I9"],
                )
            ],
            uncertainty=_uncertainty(),
            recommended_actions=[_action()],
        )


# ---------------------------------------------------------------------------
# Citation marker format
# ---------------------------------------------------------------------------


def test_invalid_citation_marker_rejected() -> None:
    with pytest.raises(ValidationError, match="citation_id values must match"):
        Observation(
            observation_id="O1",
            text="A claim.",
            citation_ids=["DATA-1"],  # missing brackets
        )


def test_dash_variant_citation_marker_accepted() -> None:
    """Legacy dash variant ``[DATA-1]`` is still accepted (production
    runs the colon variant ``[DATA:1]`` but the dash variant is the
    fallback). See orchestrator_shared_preamble_dash.py / colon.py.
    """
    Observation(
        observation_id="O1",
        text="A claim.",
        citation_ids=["[DATA-1]", "[NI43:2]", "[PGEO-7]", "[PUB:3]"],
    )


# ---------------------------------------------------------------------------
# Confidence — generic data-to-reduce-uncertainty rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "generic",
    ["more data", "More Data", "additional information", "n/a", "TBD", "unknown"],
)
def test_generic_reduction_target_rejected(generic: str) -> None:
    with pytest.raises(ValidationError, match="too generic"):
        ConfidenceBlock(
            level="Medium",
            reason="Some reason.",
            data_to_reduce_uncertainty=generic,
        )


def test_drivers_capped_at_four() -> None:
    with pytest.raises(ValidationError):
        ConfidenceBlock(
            level="Low",
            reason="Reason.",
            drivers=["a", "b", "c", "d", "e"],
            data_to_reduce_uncertainty="One infill hole at section 5+50N.",
        )


# ---------------------------------------------------------------------------
# Marker collection helper
# ---------------------------------------------------------------------------


def test_cited_marker_ids_collects_across_sections() -> None:
    ans = GeoAnswer(
        observations=[
            Observation(observation_id="O1", text="x", citation_ids=["[DATA-1]"]),
            Observation(observation_id="O2", text="y", citation_ids=["[NI43-2]"]),
        ],
        interpretations=[_interp(1, ["O1", "O2"])],
        uncertainty=UncertaintyBlock(
            confidence=_conf(),
            citation_ids=["[PGEO-3]"],
        ),
        recommended_actions=[
            RecommendedAction(
                rank=1,
                action="x",
                rationale="y",
                citation_ids=["[DATA-1]", "[PUB-4]"],
            )
        ],
    )
    assert ans.cited_marker_ids() == {"[DATA-1]", "[NI43-2]", "[PGEO-3]", "[PUB-4]"}
