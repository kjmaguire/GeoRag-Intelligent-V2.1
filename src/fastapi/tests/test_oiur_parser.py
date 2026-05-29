"""Tests for the OIUR markdown parser — Phase 1 / Step 1.2b.

Locks the parser contract:
  - happy path: full four-section answer parses cleanly
  - empty observations → (None, [refusal-routing warning])
  - missing H2 sections → (None, [missing-sections warning])
  - factual-lookup: Recommended actions = _Not applicable: ..._ → SectionEmpty
  - rank gaps in actions are renumbered, not rejected
  - duplicate observation ids → second is dropped with a warning
  - unknown observation refs in interpretations are dropped
  - generic 'more data' uncertainty target → falls back (None)
"""

from __future__ import annotations

import textwrap

from app.agent.oiur_parser import parse_oiur_markdown
from app.agent.schemas import GeoAnswer, SectionEmpty, UncertaintyBlock


def _dedent(text: str) -> str:
    return textwrap.dedent(text).strip()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


FULL_ANSWER = _dedent(
    """
    ## Observations
    (O1) DDH-07 intersected 12.4 m at 2.1 g/t Au [DATA:1].
    (O2) DDH-12 intersected 4.8 m at 0.6 g/t Au [DATA:1].

    ## Interpretations
    (I1) supports: O1, O2. Grade continuity between the two collared holes is plausible [DATA:1].

    ## Uncertainty
    **Confidence: Medium**
    Reason: Two holes constrain the eastern contact; western limit inferred from a single surface sample [DATA:1].
    Drivers:
    - Single-source western limit
    - No infill between DDH-07 and DDH-12
    Data to reduce uncertainty: One infill hole between DDH-07 and DDH-12 would resolve the grade-continuity question.

    ## Recommended actions
    1. Drill one infill hole between DDH-07 and DDH-12. Rationale: resolves grade-continuity gap noted in O1 [DATA:1]. Expected gain: confirms lateral continuity at 250 m spacing. Risk: adds ~3 weeks to the program.
    """
)


def test_full_answer_parses() -> None:
    ans, warnings = parse_oiur_markdown(FULL_ANSWER)
    assert isinstance(ans, GeoAnswer), f"warnings={warnings}"
    assert warnings == []
    assert isinstance(ans.observations, list) and len(ans.observations) == 2
    assert ans.observations[0].observation_id == "O1"
    assert ans.observations[0].citation_ids == ["[DATA:1]"]
    assert isinstance(ans.interpretations, list) and len(ans.interpretations) == 1
    assert ans.interpretations[0].supporting_observation_ids == ["O1", "O2"]
    assert isinstance(ans.uncertainty, UncertaintyBlock)
    assert ans.uncertainty.confidence.level == "Medium"
    assert len(ans.uncertainty.confidence.drivers) == 2
    assert isinstance(ans.recommended_actions, list) and len(ans.recommended_actions) == 1
    action = ans.recommended_actions[0]
    assert action.rank == 1
    assert action.citation_ids == ["[DATA:1]"]
    assert action.expected_information_gain is not None
    assert action.risk is not None


# ---------------------------------------------------------------------------
# Refusal contract — empty observations falls back so caller can refuse
# ---------------------------------------------------------------------------


def test_observations_section_empty_falls_back() -> None:
    text = _dedent(
        """
        ## Observations

        ## Interpretations
        _Not applicable: no observations._

        ## Uncertainty
        _Not applicable: no observations._

        ## Recommended actions
        _Not applicable: no observations._
        """
    )
    ans, warnings = parse_oiur_markdown(text)
    assert ans is None
    assert any("refusal" in w.lower() or "no parseable observations" in w for w in warnings)


def test_missing_sections_falls_back() -> None:
    text = _dedent(
        """
        ## Observations
        (O1) Some observation [DATA:1].

        ## Interpretations
        (I1) supports: O1. Some interpretation [DATA:1].
        """
    )
    ans, warnings = parse_oiur_markdown(text)
    assert ans is None
    assert any("missing H2 sections" in w for w in warnings)


# ---------------------------------------------------------------------------
# Partial-evidence: factual lookup with no decision context
# ---------------------------------------------------------------------------


def test_factual_lookup_actions_not_applicable() -> None:
    text = _dedent(
        """
        ## Observations
        (O1) NI 43-101 §1.3 requires QP sign-off on each Measured Resource block [NI43:1].

        ## Interpretations
        (I1) supports: O1. The clause applies regardless of commodity [NI43:1].

        ## Uncertainty
        **Confidence: High**
        Reason: NI 43-101 §1.3 is a primary regulatory clause cited verbatim from the standard [NI43:1].
        Drivers:
        - None — direct clause lookup
        Data to reduce uncertainty: A linked CIM Definition Standards version reference would tie the answer to the current 2014 wording.

        ## Recommended actions
        _Not applicable: the query is a factual lookup; no decision context was supplied._
        """
    )
    ans, warnings = parse_oiur_markdown(text)
    assert isinstance(ans, GeoAnswer), f"warnings={warnings}"
    assert isinstance(ans.recommended_actions, SectionEmpty)
    assert "factual lookup" in ans.recommended_actions.reason.lower()


# ---------------------------------------------------------------------------
# Rank gap handling
# ---------------------------------------------------------------------------


def test_rank_gaps_are_renumbered() -> None:
    text = _dedent(
        """
        ## Observations
        (O1) DDH-07 intersected mineralisation [DATA:1].

        ## Interpretations
        (I1) supports: O1. Mineralisation is open down-dip [DATA:1].

        ## Uncertainty
        **Confidence: Low**
        Reason: A single intersect does not constrain continuity [DATA:1].
        Drivers:
        - Only one hole
        Data to reduce uncertainty: One infill hole 50 m down-dip of DDH-07.

        ## Recommended actions
        1. Drill one infill hole. Rationale: resolves down-dip continuity [DATA:1]. Expected gain: confirms continuity. Risk: cost.
        3. Re-log existing core. Rationale: free, may surface missed intervals [DATA:1]. Expected gain: more obs. Risk: time.
        """
    )
    ans, warnings = parse_oiur_markdown(text)
    assert isinstance(ans, GeoAnswer), f"warnings={warnings}"
    assert isinstance(ans.recommended_actions, list)
    assert [a.rank for a in ans.recommended_actions] == [1, 2]
    assert any("renumbered" in w.lower() for w in warnings)


# ---------------------------------------------------------------------------
# Validation drop-paths
# ---------------------------------------------------------------------------


def test_unknown_observation_in_interpretation_is_dropped() -> None:
    text = _dedent(
        """
        ## Observations
        (O1) Sole observation [DATA:1].

        ## Interpretations
        (I1) supports: O5. Bogus observation reference [DATA:1].
        (I2) supports: O1. Valid interpretation [DATA:1].

        ## Uncertainty
        **Confidence: Low**
        Reason: Single observation only [DATA:1].
        Drivers:
        - Sparse data
        Data to reduce uncertainty: Add holes at 100 m spacing around O1.

        ## Recommended actions
        1. Plan infill. Rationale: addresses sparse coverage [DATA:1]. Expected gain: continuity. Risk: cost.
        """
    )
    ans, warnings = parse_oiur_markdown(text)
    assert isinstance(ans, GeoAnswer), f"warnings={warnings}"
    assert isinstance(ans.interpretations, list)
    assert [i.interpretation_id for i in ans.interpretations] == ["I2"]
    assert any("unknown observation ids" in w for w in warnings)


def test_observation_without_citation_is_dropped() -> None:
    text = _dedent(
        """
        ## Observations
        (O1) Properly cited observation [DATA:1].
        (O2) Uncited observation — should be skipped.

        ## Interpretations
        (I1) supports: O1. Valid interpretation [DATA:1].

        ## Uncertainty
        **Confidence: Low**
        Reason: One observation only [DATA:1].
        Drivers:
        - Sparse
        Data to reduce uncertainty: Specific infill location around DDH-07.

        ## Recommended actions
        1. Plan infill. Rationale: address gap [DATA:1]. Expected gain: x. Risk: y.
        """
    )
    ans, warnings = parse_oiur_markdown(text)
    assert isinstance(ans, GeoAnswer), f"warnings={warnings}"
    assert isinstance(ans.observations, list)
    assert [o.observation_id for o in ans.observations] == ["O1"]
    assert any("no citation marker" in w for w in warnings)


def test_generic_reduce_uncertainty_target_falls_back() -> None:
    text = _dedent(
        """
        ## Observations
        (O1) Observation [DATA:1].

        ## Interpretations
        (I1) supports: O1. Interpretation [DATA:1].

        ## Uncertainty
        **Confidence: Medium**
        Reason: Reasonable evidence [DATA:1].
        Drivers:
        - sparse
        Data to reduce uncertainty: more data

        ## Recommended actions
        1. Plan. Rationale: hint [DATA:1]. Expected gain: x. Risk: y.
        """
    )
    ans, warnings = parse_oiur_markdown(text)
    # ConfidenceBlock rejects 'more data', so uncertainty becomes
    # SectionEmpty. Interpretations is still a list, which trips the
    # cross-section fallback — top-level returns None so the caller falls
    # back to the flat-text path.
    assert ans is None
    assert any("too generic" in w or "Confidence block invalid" in w for w in warnings)


def test_duplicate_observation_id_dropped() -> None:
    text = _dedent(
        """
        ## Observations
        (O1) First [DATA:1].
        (O1) Duplicate [DATA:1].

        ## Interpretations
        (I1) supports: O1. Interpretation [DATA:1].

        ## Uncertainty
        **Confidence: Low**
        Reason: Single observation [DATA:1].
        Drivers:
        - sparse
        Data to reduce uncertainty: Add a second hole at section 5+50N.

        ## Recommended actions
        1. Plan. Rationale: hint [DATA:1]. Expected gain: x. Risk: y.
        """
    )
    ans, warnings = parse_oiur_markdown(text)
    assert isinstance(ans, GeoAnswer), f"warnings={warnings}"
    assert isinstance(ans.observations, list)
    assert len(ans.observations) == 1
    assert any("duplicate O1" in w for w in warnings)


def test_competes_with_clause_parsed() -> None:
    text = _dedent(
        """
        ## Observations
        (O1) Potassic + propylitic mineral assemblages visible in core [NI43:1].

        ## Interpretations
        (I1) supports: O1. Potassic alteration overprints earlier propylitic [NI43:1].
        (I2) supports: O1. competes-with: I1. Propylitic and potassic are coeval phases [NI43:1].

        ## Uncertainty
        **Confidence: Medium**
        Reason: Two competing interpretations from the same observations [NI43:1].
        Drivers:
        - Two valid readings
        - No crosscutting relationships logged
        Data to reduce uncertainty: Thin sections at the alteration contact in DDH-07 between 145–155 m.

        ## Recommended actions
        1. Request thin sections at 145–155 m. Rationale: differentiates I1 vs I2 [NI43:1]. Expected gain: crosscutting relationships. Risk: lab turnaround.
        """
    )
    ans, warnings = parse_oiur_markdown(text)
    assert isinstance(ans, GeoAnswer), f"warnings={warnings}"
    assert isinstance(ans.interpretations, list)
    competing = {i.interpretation_id: i.competing_with for i in ans.interpretations}
    assert competing["I2"] == ["I1"]
