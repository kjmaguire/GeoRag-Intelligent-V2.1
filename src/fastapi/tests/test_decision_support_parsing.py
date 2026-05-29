"""Tests for the decision-support parsing + schema extras — Phase 1 / Step 1.4."""

from __future__ import annotations

import textwrap

import pytest
from pydantic import ValidationError

from app.agent.oiur_parser import parse_oiur_markdown
from app.agent.schemas import (
    DecisionSupport,
    GeoAnswer,
    Interpretation,
    Observation,
    SectionEmpty,
    UncertaintyBlock,
)
from app.agent.schemas.geo_answer import (
    ConfidenceBlock,
    RecommendedAction,
)


def _dedent(text: str) -> str:
    return textwrap.dedent(text).strip()


# ---------------------------------------------------------------------------
# DecisionSupport schema
# ---------------------------------------------------------------------------


def test_decision_support_defaults_are_empty() -> None:
    ds = DecisionSupport()
    assert ds.unresolved_prerequisites == []
    assert ds.regulatory_constraints == []
    assert ds.ranking_deferred_reason is None


def test_decision_support_blank_prereq_rejected() -> None:
    with pytest.raises(ValidationError):
        DecisionSupport(unresolved_prerequisites=["", "valid item"])


def test_ranking_deferred_with_actions_list_rejected() -> None:
    """Schema invariant: ranking_deferred_reason and a non-empty
    recommended_actions list are mutually exclusive.
    """
    with pytest.raises(ValidationError, match="mutually exclusive"):
        GeoAnswer(
            observations=[
                Observation(
                    observation_id="O1",
                    text="Obs [DATA-1].",
                    citation_ids=["[DATA-1]"],
                )
            ],
            interpretations=[
                Interpretation(
                    interpretation_id="I1",
                    text="Interp [DATA-1].",
                    supporting_observation_ids=["O1"],
                )
            ],
            uncertainty=UncertaintyBlock(
                confidence=ConfidenceBlock(
                    level="Medium",
                    reason="r",
                    data_to_reduce_uncertainty="One infill hole between DDH-07 and DDH-12.",
                ),
            ),
            recommended_actions=[
                RecommendedAction(
                    rank=1,
                    action="Drill.",
                    rationale="Because.",
                    citation_ids=["[DATA-1]"],
                )
            ],
            decision_support=DecisionSupport(
                ranking_deferred_reason="Insufficient evidence to rank.",
            ),
        )


def test_ranking_deferred_with_section_empty_actions_accepted() -> None:
    answer = GeoAnswer(
        observations=[
            Observation(
                observation_id="O1",
                text="Obs [DATA-1].",
                citation_ids=["[DATA-1]"],
            )
        ],
        interpretations=[
            Interpretation(
                interpretation_id="I1",
                text="Interp [DATA-1].",
                supporting_observation_ids=["O1"],
            )
        ],
        uncertainty=UncertaintyBlock(
            confidence=ConfidenceBlock(
                level="Low",
                reason="Sparse evidence.",
                data_to_reduce_uncertainty="Two infill holes east of DDH-07.",
            ),
        ),
        recommended_actions=SectionEmpty(
            reason="Insufficient evidence to defensibly rank options."
        ),
        decision_support=DecisionSupport(
            ranking_deferred_reason="Insufficient evidence to defensibly rank options.",
            unresolved_prerequisites=["Confirmed CRM pass-rate for batch B-2024-17."],
        ),
    )
    assert answer.decision_support is not None


# ---------------------------------------------------------------------------
# Parser — H3 subsections lift into DecisionSupport
# ---------------------------------------------------------------------------


DECISION_SUPPORT_ANSWER = _dedent(
    """
    ## Observations
    (O1) DDH-07 intersected 12.4 m at 2.1 g/t Au [DATA-1].
    (O2) DDH-12 intersected 4.8 m at 0.6 g/t Au [DATA-1].

    ## Interpretations
    (I1) supports: O1, O2. Grade continuity is plausible at 250 m spacing [DATA-1].

    ## Uncertainty
    **Confidence: Medium**
    Reason: Two holes constrain the eastern contact [DATA-1].
    Drivers:
    - Single-source western limit
    - No infill between DDH-07 and DDH-12
    Data to reduce uncertainty: One infill hole between DDH-07 and DDH-12 would resolve the question.

    ## Recommended actions
    1. Drill one infill hole between DDH-07 and DDH-12. Rationale: resolves grade-continuity gap [DATA-1]. Expected gain: confirms lateral continuity. Risk: adds ~3 weeks.

    ### Unresolved prerequisites
    - Confirmed CRM pass-rate for batch B-2024-17.
    - Updated topography along section 5+50N.

    ### Reporting / regulatory constraints
    - NI 43-101 §1.3 requires QP sign-off for any Measured Resource classification.
    """
)


def test_decision_support_h3_subsections_parsed() -> None:
    ans, warnings = parse_oiur_markdown(DECISION_SUPPORT_ANSWER)
    assert isinstance(ans, GeoAnswer), f"warnings={warnings}"
    assert ans.decision_support is not None
    ds = ans.decision_support
    assert ds.unresolved_prerequisites == [
        "Confirmed CRM pass-rate for batch B-2024-17.",
        "Updated topography along section 5+50N.",
    ]
    assert any("NI 43-101" in c for c in ds.regulatory_constraints)
    assert ds.ranking_deferred_reason is None
    # Recommended actions list still parsed normally.
    assert isinstance(ans.recommended_actions, list)
    assert len(ans.recommended_actions) == 1


def test_no_decision_support_subsections_yields_none() -> None:
    """A plain OIUR answer without the H3 subsections should leave
    decision_support as None — the H3s are the signal, not the classifier.
    """
    text = _dedent(
        """
        ## Observations
        (O1) Some observation [DATA-1].

        ## Interpretations
        (I1) supports: O1. Some interpretation [DATA-1].

        ## Uncertainty
        **Confidence: Medium**
        Reason: Some reason [DATA-1].
        Drivers:
        - One thing
        Data to reduce uncertainty: One specific infill hole.

        ## Recommended actions
        1. Some action. Rationale: some reason [DATA-1]. Expected gain: x. Risk: y.
        """
    )
    ans, _ = parse_oiur_markdown(text)
    assert isinstance(ans, GeoAnswer)
    assert ans.decision_support is None


def test_ranking_deferred_sentinel_lifts_to_section_empty() -> None:
    text = _dedent(
        """
        ## Observations
        (O1) DDH-07 intersected mineralisation [DATA-1].

        ## Interpretations
        (I1) supports: O1. Mineralisation open down-dip [DATA-1].

        ## Uncertainty
        **Confidence: Low**
        Reason: Single intersect [DATA-1].
        Drivers:
        - Only one hole
        Data to reduce uncertainty: One infill hole 50 m down-dip of DDH-07.

        ## Recommended actions
        _Ranking deferred: insufficient evidence to defensibly differentiate options._

        ### Unresolved prerequisites
        - One infill hole down-dip of DDH-07.
        - Topography along section 5+50N.

        ### Reporting / regulatory constraints
        - NI 43-101 §1.3 will apply once a Measured classification is sought.
        """
    )
    ans, warnings = parse_oiur_markdown(text)
    assert isinstance(ans, GeoAnswer), f"warnings={warnings}"
    assert isinstance(ans.recommended_actions, SectionEmpty)
    assert "Ranking deferred" not in ans.recommended_actions.reason  # the reason
    assert ans.decision_support is not None
    assert ans.decision_support.ranking_deferred_reason
    assert ans.decision_support.unresolved_prerequisites
    assert ans.decision_support.regulatory_constraints


def test_none_bullet_yields_empty_list() -> None:
    """The prompt allows ``- None — …`` as an explicit-empty marker. Parser
    should treat that as zero bullets, not as a single bullet named 'None'.
    """
    text = _dedent(
        """
        ## Observations
        (O1) Obs [DATA-1].

        ## Interpretations
        (I1) supports: O1. Interp [DATA-1].

        ## Uncertainty
        **Confidence: Medium**
        Reason: r [DATA-1].
        Drivers:
        - one
        Data to reduce uncertainty: Specific item near DDH-07.

        ## Recommended actions
        1. Action. Rationale: r [DATA-1]. Expected gain: x. Risk: y.

        ### Unresolved prerequisites
        - None — the retrieved evidence supports the ranking as stated.

        ### Reporting / regulatory constraints
        - NI 43-101 §1.3 requires QP sign-off.
        """
    )
    ans, _ = parse_oiur_markdown(text)
    assert isinstance(ans, GeoAnswer)
    assert ans.decision_support is not None
    assert ans.decision_support.unresolved_prerequisites == []
    assert len(ans.decision_support.regulatory_constraints) == 1
