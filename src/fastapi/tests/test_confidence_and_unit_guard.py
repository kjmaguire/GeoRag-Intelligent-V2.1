"""Three ways the answer path scored a good answer as a bad one.

1. `_is_refusal` substring-scanned the ENTIRE answer against a table that
   includes "no data", "not found", "insufficient", "not available" and
   "no drill hole" -- ordinary geological vocabulary. Most real answers say
   what they could not establish, so most real answers were classified as
   refusals and had confidence forced to 0.1. It also overrode the
   safeguard the starts-with branch documents itself as having: that
   comment says it exists so "No drill holes intersected mineralisation"
   reads as an answer, and "no drill hole" in the phrase table undid it.

2. `_is_empty_tool_result` -- documented as mandatory, exported, and never
   called anywhere. `_compute_confidence` takes the arithmetic MEAN of
   per-result relevance, so one zero-row structured lookup roughly halved
   the confidence of a strong document answer, and drew a citation marker
   for a tool that found nothing.

3. `_NUMBER_WITH_UNIT_RE` terminated on a bare word boundary, which makes
   "%" unmatchable: "%" is a non-word character, so the boundary demands a
   word character right after it, which never happens in prose. On a
   uranium platform, where grade is quoted in percent, the Layer-3
   unit-pair guard was blind to exactly the values it exists for -- and
   ppm-vs-% confusion is the 10,000x error class.
"""

from __future__ import annotations

import pytest

from app.agent.hallucination.orchestrator_validators import (
    _extract_number_unit_tuples,
)
from app.agent.response_assembler import _is_refusal

CITED_ANSWER_WITH_A_GAP = (
    "Hole PLS-22-08 returned 1.85 g/t Au over 12.5 m [DATA-1]. Core recovery "
    "data is not available for the upper 40 m [NI43-2]."
)

NEGATIVE_BUT_GROUNDED = (
    "No drill holes intersected mineralisation above the 0.5% cut-off in this "
    "project. The highest recorded intercept is 0.38% U3O8 over 2.1 m."
)


class TestRefusalDetection:
    @pytest.mark.parametrize(
        ("answer", "why"),
        [
            (CITED_ANSWER_WITH_A_GAP, "reports a gap after answering"),
            (
                "Assay certificates for two holes were not found in the project "
                "archive [DATA-3], but the remaining eleven carry complete "
                "certificates and average 0.42% U3O8 [DATA-3].",
                "gap in the first sentence, still a cited answer",
            ),
            (NEGATIVE_BUT_GROUNDED, "a negative finding is a finding"),
        ],
    )
    def test_an_answer_that_reports_a_gap_is_not_a_refusal(
        self, answer: str, why: str
    ) -> None:
        assert _is_refusal(answer) is False, why

    @pytest.mark.parametrize(
        "answer",
        [
            "I don't have data on that in this project.",
            "No data is available for the Triple R zone.",
            "Insufficient information to answer that.",
            "That is not a possible value for a uranium grade.",
            "I can only answer geological questions about this project.",
            # Short enough that the refusal IS the whole answer.
            "I checked silver.collars for the Triple R zone. No records were returned.",
        ],
    )
    def test_a_real_refusal_still_reads_as_one(self, answer: str) -> None:
        assert _is_refusal(answer) is True

    def test_an_impossible_premise_is_still_refused(self) -> None:
        """Caught by the starts-with branch, which requires a can/is verb.

        This is why "no drill hole" could be dropped from the phrase table
        without losing the refusal it was there for.
        """
        assert _is_refusal("No drill hole can be 3,000 m deep in this district.")

    def test_a_first_person_hedge_is_a_refusal_even_when_it_cites(self) -> None:
        answer = (
            "I searched the 2023 technical report [NI43-1] but I don't have "
            "assay data for that zone."
        )

        assert _is_refusal(answer) is True

    def test_empty_text_is_not_a_refusal(self) -> None:
        assert _is_refusal("") is False


class TestEmptyToolResultsAreDropped:
    """The filter that existed, was documented as mandatory, and never ran."""

    @staticmethod
    def _worth_citing():
        from app.agent.agentic_retrieval.nodes import _worth_citing

        return _worth_citing

    def test_a_zero_row_lookup_is_dropped(self) -> None:
        from app.agent.tools import AssayDataResult

        empty = AssayDataResult(
            samples=[],
            count=0,
            element="U3O8",
            available_elements=["U3O8"],
            min_value=None,
            max_value=None,
            mean_value=None,
            median_value=None,
            data_source="PostGIS silver.samples",
        )

        assert self._worth_citing()("query_assay_data", empty) is False

    def test_a_document_search_with_no_chunks_is_dropped(self) -> None:
        from app.agent.tools import DocumentSearchResult

        assert self._worth_citing()("search_documents", DocumentSearchResult(
            chunks=[], count=0, data_source="qdrant:georag_chunks",
        )) is False

    def test_none_is_dropped(self) -> None:
        assert self._worth_citing()("search_documents", None) is False

    def test_an_unknown_result_type_is_kept(self) -> None:
        """Visualization cards must survive — they are not evidence."""
        assert self._worth_citing()("query_stereonet", object()) is True


class TestPercentUnitsAreExtractable:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("grade of 5% U3O8", [(5.0, "%")]),
            ("grade of 5%.", [(5.0, "%")]),
            ("5% and", [(5.0, "%")]),
            ("grade 0.45 wt% Cu", [(0.45, "wt%")]),
            ("2.1% U3O8 over 4.5 m", [(2.1, "%"), (4.5, "m")]),
        ],
    )
    def test_percent_values_are_extracted(
        self, text: str, expected: list[tuple[float, str]]
    ) -> None:
        assert _extract_number_unit_tuples(text) == expected

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("37 oz/t Au", [(37.0, "oz/t")]),
            ("12.5 m of core", [(12.5, "m")]),
            ("450 ppm U", [(450.0, "ppm")]),
        ],
    )
    def test_the_units_that_already_worked_still_do(
        self, text: str, expected: list[tuple[float, str]]
    ) -> None:
        assert _extract_number_unit_tuples(text) == expected

    @pytest.mark.parametrize("text", ["5 mm of core", "12 metres", "3 mtr"])
    def test_a_unit_that_runs_into_more_letters_is_not_a_match(
        self, text: str
    ) -> None:
        """"5 mm" must not read as 5 metres — what the old boundary bought."""
        assert _extract_number_unit_tuples(text) == []

    def test_citation_markers_do_not_break_extraction(self) -> None:
        assert _extract_number_unit_tuples("2.1% U3O8 over 4.5 m [DATA-1]") == [
            (2.1, "%"),
            (4.5, "m"),
        ]

    def test_small_round_values_are_still_excluded_by_design(self) -> None:
        """`_SMALL_NUMBERS` drops 0/1/2/3 as "too common to verify".

        Pinned so the exclusion is a visible decision rather than a
        surprise: "2% U3O8" is a plausible uranium grade and the unit-pair
        guard does not see it. That predates this fix and is unchanged by
        it — the percent arm simply could not see ANY value before.
        """
        assert _extract_number_unit_tuples("2% U3O8") == []


class TestTupleWarningsReachTheSeverityBucket:
    def test_the_advisory_predicate_covers_both_layer_3_shapes(self) -> None:
        """The numeric guard emits "Layer 3:"; the unit guard "Layer 3 tuple:".

        Bucketing on "Layer 3:" excluded every tuple warning, so the
        2026-08-14 shadow->warn promotion changed nothing: unit-pair
        mismatches never counted toward NUMERIC_RETRY_THRESHOLD and never
        set should_retry.
        """
        from app.agent.hallucination.orchestrator_validators import (
            LAYER3_WARNING_PREFIXES,
        )

        numeric = "Layer 3: Ungrounded number 4.2 in response"
        tuple_warning = (
            "Layer 3 tuple: value 5.2 reported as 'ppm' but source says 'g/t'"
        )

        assert numeric.startswith(LAYER3_WARNING_PREFIXES)
        assert tuple_warning.startswith(LAYER3_WARNING_PREFIXES), (
            "unit-pair warnings are excluded from the advisory bucket again"
        )

        # And the bucket really uses it, rather than a literal that has
        # drifted from the tuple.
        import inspect

        from app.agent.hallucination import orchestrator_validators as ov

        source = inspect.getsource(ov.run_post_assembly_validation)
        assert "LAYER3_WARNING_PREFIXES" in source

        # Narrow, not loose: `startswith("Layer 3")` also matches a
        # "Layer 30:" nobody has written, and this predicate decides
        # whether an ungrounded-number answer gets retried.
        assert not "Layer 30: hypothetical".startswith(LAYER3_WARNING_PREFIXES)
