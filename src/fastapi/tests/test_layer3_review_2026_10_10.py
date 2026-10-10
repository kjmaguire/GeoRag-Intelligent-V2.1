"""Layer 3 after the independent review of the unit-aware grounding (2026-10-10).

The review's verdict was "merge with fixes". Its findings, one class each:

* **Precision (HIGH).** Routine correct answers with structured evidence were
  being flagged, and any Layer 3 finding floors the confidence and prints the
  banner: a numbered list, "the 75th percentile", "the top 5 intervals",
  "Zone 5", a mean elevation, "7.3 m from 145.2 to 152.5 m", "a 320.1-m depth",
  a mean written without its unit, and the counts / percentages / multiples an
  analyst works out from the rows.
* **"million" grounds any unit (HIGH).** The scaled figure of "48.2 million
  tonnes" went into the unit-blind literal set, so "48,200,000 ounces" passed.
* **The derived window (MED-HIGH).** The tool's full-set aggregates sat in the
  same series as the LIMIT-capped rows, and any in-range number was accepted
  whatever the sentence was saying.
* **Sentinels (LOW-MED)** stretched the window; **oz/t** converted into itself.

Fabrications that must STAY flagged are asserted next to each acceptance: a
test that only shows the new passes would be satisfied by deleting the guard.

Integer claims are grounded by any evidence number within half a unit, so each
figure below is chosen away from the numbers in its evidence; the counts are
asserted not to be literal (`_assert_not_literal`) so that a pass here is the
recount's, not a coincidence's.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.hallucination.orchestrator_validators import (
    _collect_evidence,
    _conversion_factors,
    _scan_quantities,
    _strip_non_claims,
    run_post_assembly_validation,
    verify_numbers,
)
from app.models.rag import Citation, GeoRAGResponse

DEPTHS = (152.0, 187.5, 203.0, 240.0, 266.5, 301.0, 345.0, 378.0, 402.5, 431.0, 455.0, 480.0)


def _collars(
    depths: tuple[float, ...] = DEPTHS,
    *,
    elevations: tuple[float, ...] | None = None,
    dips: tuple[float, ...] | None = None,
) -> tuple[str, dict[str, Any]]:
    """A ``query_spatial_collars`` payload: one collar per depth."""
    elevations = elevations or tuple(512.0 + i for i in range(len(depths)))
    dips = dips or (-60.0,) * len(depths)
    return ("query_spatial_collars", {
        "count": len(depths),
        "collars": [
            {
                "hole_id": f"PLS-22-{i + 1:02d}", "total_depth": depth, "elevation": elevations[i],
                "azimuth": 45.0, "dip": dips[i], "easting": 512345.7 + i, "northing": 6543210.1,
            }
            for i, depth in enumerate(depths)
        ],
    })


#: 12 samples in 6 holes. Above 2.0 g/t: SIX samples (2.6, 2.9, 3.1, 3.3, 2.2,
#: 2.4) in FOUR holes (H2, H3, H4, H6). Below 1.0 g/t: five samples in four
#: holes. Above 2.5 g/t: four samples in three holes.
_SAMPLES = (
    ("H1", 0.35), ("H1", 0.8), ("H2", 2.6), ("H2", 2.9), ("H3", 3.1), ("H3", 0.15),
    ("H4", 3.3), ("H4", 0.95), ("H4", 2.2), ("H5", 1.75), ("H6", 0.65), ("H6", 2.4),
)


def _assays(element: str = "Au_ppm", scale: float = 1.0) -> tuple[str, dict[str, Any]]:
    values = [g * scale for _, g in _SAMPLES]
    return ("query_assay_data", {
        "element": element, "count": len(_SAMPLES), "total_count": len(_SAMPLES),
        "samples": [
            {"hole_id": hole, "element": element, "value": g * scale,
             "from_depth": 100.5 + 2.5 * i, "to_depth": 101.5 + 2.5 * i}
            for i, (hole, g) in enumerate(_SAMPLES)
        ],
        "min_value": min(values), "max_value": max(values),
        "mean_value": round(sum(values) / len(values), 2),
        "median_value": sorted(values)[len(values) // 2], "std_value": 1.05 * scale,
    })


def _docs(text: str) -> tuple[str, dict[str, Any]]:
    return ("search_documents", {"chunks": [{"text": text, "document_title": "NI 43-101 report"}]})


def _flagged(answer: str, *results: tuple[str, Any]) -> bool:
    return bool(verify_numbers(answer, list(results)))


def _assert_not_literal(number: float, *results: tuple[str, Any]) -> None:
    """Precondition: ``number`` is nowhere in the evidence, to within the half
    unit an integer claim is grounded by, so a pass is not a coincidence."""
    literal = _collect_evidence(list(results)).literal
    assert not any(abs(number - g) <= 0.6 for g in literal), number


# ---------------------------------------------------------------------------
# Finding 2 -- numbers that are not claims
# ---------------------------------------------------------------------------


class TestLabelsRanksAndListPositionsAreNotClaims:
    ANSWER = (
        "Key findings:\n"
        "1. Holes range from 152 m to 480 m deep [DATA-1].\n"
        "2. The mean elevation is 517.5 m [DATA-1].\n"
        "3. Most holes dip -60 degrees [DATA-1].\n"
        "4. The azimuth is 45 degrees [DATA-1].\n"
        "5. The shallowest hole is 152 m [DATA-1].\n"
        "6. The deepest hole is 480 m [DATA-1]."
    )

    def test_a_numbered_findings_list_is_not_ungrounded_numbers(self) -> None:
        """"Ungrounded number 5.0 / 6.0": the list positions."""
        assert verify_numbers(self.ANSWER, [_collars()]) == []

    @pytest.mark.parametrize(
        "answer",
        [
            "4) Holes range from 152 m to 480 m [DATA-1].\n5) The shallowest hole is 152 m [DATA-1].",
            "- 5. The deepest hole is 480 m [DATA-1].",
            "## 4. Results\nHoles range from 152 m to 480 m [DATA-1].",
            "### 4.2 Depth\nThe deepest hole is 480 m [DATA-1].",
            "**5.** The deepest hole is 480 m [DATA-1].",
        ],
    )
    def test_other_list_and_heading_markers(self, answer: str) -> None:
        assert verify_numbers(answer, [_collars()]) == []

    def test_a_list_marker_does_not_hide_the_claim_after_it(self) -> None:
        """The marker goes; the 405 m behind it is still checked."""
        assert _flagged("5. The deepest hole is 405 m [DATA-1].", _collars())
        assert _flagged("12. The deepest hole is 505 m [DATA-1].", _collars())

    def test_a_table_index_column_is_not_data(self) -> None:
        table = (
            "| # | Hole | Total depth (m) |\n|---|------|-----------------|\n"
            "| 5 | PLS-22-05 | 266.5 |\n| 6 | PLS-22-06 | 301 |\n| 9 | PLS-22-09 | 402.5 |"
        )
        assert verify_numbers(table, [_collars()]) == []

    def test_the_index_column_exemption_is_only_for_an_index_column(self) -> None:
        """A table whose first column is the DEPTH keeps every cell checked."""
        table = "| Depth (m) | Hole |\n|---|---|\n| 5 | PLS-22-05 |\n| 6 | PLS-22-06 |"
        assert _flagged(table, _collars())

    @pytest.mark.parametrize(
        "answer",
        [
            "The 75th percentile grade is 2.6 g/t Au [DATA-1].",
            "The 90th percentile grade is 3.3 g/t Au [DATA-1].",
            "Grades above the 95th percentile are anomalous [DATA-1].",
            "The 75 percentile grade is 2.6 g/t Au [DATA-1].",
            "The P90 grade is 3.3 g/t Au [DATA-1].",
        ],
    )
    def test_a_percentile_rank_is_not_a_claim(self, answer: str) -> None:
        assert verify_numbers(answer, [_assays()]) == []

    def test_a_percentile_rank_does_not_hide_the_value_it_ranks(self) -> None:
        assert _flagged("The 75th percentile grade is 9.9 g/t Au [DATA-1].", _assays())

    @pytest.mark.parametrize(
        "answer",
        [
            "The top 5 intervals by grade are listed below [DATA-1].",
            "Here are the top 10 samples [DATA-1].",
            "The first 4 holes and the last 9 holes were re-logged [DATA-1].",
            "The 5 highest-grade intervals are listed below [DATA-1].",
            "The top 10% of samples carry most of the metal [DATA-1].",
        ],
    )
    def test_the_size_of_a_selection_is_not_a_claim(self, answer: str) -> None:
        _assert_not_literal(9, _assays())
        assert verify_numbers(answer, [_assays()]) == []

    def test_a_length_after_first_or_last_is_a_claim(self) -> None:
        """"first 190 m" says how deep, not how many."""
        assert _flagged("The first 190 m of core was re-logged [DATA-1].", _assays())

    @pytest.mark.parametrize(
        "answer",
        [
            "Mineralisation is hosted in Zone 5 [DATA-1].",
            "Phase 4 drilling targeted the extension [DATA-1].",
            "Zones 4 and 5 are open to the north [DATA-1].",
            "The Vein 12 structure strikes north [DATA-1].",
            "Recommended action 8: infill drilling [DATA-1].",
        ],
    )
    def test_a_label_is_not_a_claim(self, answer: str) -> None:
        assert verify_numbers(answer, [_assays()]) == []

    @pytest.mark.parametrize(
        "answer",
        [
            "The zone 25 m wide is open at depth [DATA-1].",   # a width
            "The company targets 50 holes this year [DATA-1].",  # a count
            "Zone 5 is 90 m wide [DATA-1].",                    # label AND width
            "The licence covers an area 450 hectares in extent [DATA-1].",  # a quantity
            "The zone 25m wide is open at depth [DATA-1].",  # a glued unit
        ],
    )
    def test_a_label_word_does_not_hide_a_real_number(self, answer: str) -> None:
        assert _flagged(answer, _assays())

    @pytest.mark.parametrize("answer", ["Mineralisation is hosted in Lens 2A [DATA-1].", "Line 100E was flown [DATA-1]."])
    def test_a_lettered_label_is_still_a_label(self, answer: str) -> None:
        assert verify_numbers(answer, [_assays()]) == []

    def test_the_stripped_text_keeps_the_words(self) -> None:
        clean = _strip_non_claims("3. The 75th percentile of the top 5 intervals, Zone 5")
        assert "percentile" in clean and "top" in clean and "Zone" in clean
        assert not any(ch.isdigit() for ch in clean)


class TestAHyphenatedUnitIsAUnit:
    def test_n_dash_m_is_metres(self) -> None:
        assert [(q.value, q.family) for q in _scan_quantities("a 320.1-m depth")] == [
            (320.1, "length_m")
        ]
        assert [q.family for q in _scan_quantities("a 5-km strike and a 12-ft step")] == [
            "length_km", "length_ft",
        ]

    def test_a_word_that_merely_starts_with_a_unit_is_not_one(self) -> None:
        assert [q.family for q in _scan_quantities("a 12-month programme, a 5-minute stop")] == [
            None, None
        ]

    def test_a_range_is_still_a_range(self) -> None:
        assert [q.family for q in _scan_quantities("120-126 m")] == ["length_m"] * 2

    def test_the_hyphenated_mean_is_grounded_and_a_wrong_one_is_not(self) -> None:
        assert verify_numbers("The holes average a 328.4-m depth [DATA-1].", [_collars()]) == []
        assert _flagged("The holes average a 628.4-m depth [DATA-1].", _collars())


class TestElevationIsAveraged:
    def test_a_mean_elevation_is_a_statistic_of_the_elevations(self) -> None:
        assert verify_numbers("Average collar elevation is 517.5 m [DATA-1].", [_collars()]) == []

    def test_but_not_of_the_depths(self) -> None:
        """410 m lies in the depth series; the elevations are 512-523 m."""
        assert _flagged("Average collar elevation is 410 m [DATA-1].", _collars())
        assert _flagged("Average collar elevation is 700 m [DATA-1].", _collars())

    def test_nor_is_a_depth_a_statistic_of_the_elevations(self) -> None:
        assert _flagged("The average hole depth is 517.5 m [DATA-1].", _collars((152.0, 187.5)))


class TestAWidthIsTheDifferenceOfTwoStatedDepths:
    DOCS = _docs("The interval was sampled from 145.2 to 152.5 m.")

    def test_a_composite_width_from_its_two_depths(self) -> None:
        assert verify_numbers(
            "Mineralisation spans 7.3 m from 145.2 to 152.5 m [NI43-1].", [self.DOCS]
        ) == []
        assert verify_numbers(
            "The 7.3 m interval from 145.2 m to 152.5 m [NI43-1].", [self.DOCS]
        ) == []

    def test_a_wrong_width_is_flagged(self) -> None:
        assert _flagged("Mineralisation spans 8.1 m from 145.2 to 152.5 m [NI43-1].", self.DOCS)

    def test_the_depths_are_still_checked(self) -> None:
        """A width that is the difference of two INVENTED depths grounds
        nothing: the depths are flagged on their own."""
        warnings = verify_numbers(
            "Mineralisation spans 10.0 m from 140.2 to 150.2 m [NI43-1].", [self.DOCS]
        )
        assert any("140.2" in w for w in warnings) and any("150.2" in w for w in warnings)

    def test_the_difference_must_be_in_the_same_sentence(self) -> None:
        assert _flagged(
            "The interval runs from 145.2 to 152.5 m [NI43-1]. The zone is 7.3 m thick [NI43-1].",
            self.DOCS,
        )

    def test_units_are_converted_before_subtracting(self) -> None:
        docs = _docs("The interval was sampled from 100.4 to 110.4 m.")
        assert verify_numbers(
            "The interval from 100.4 to 110.4 m is 32.8 ft wide [NI43-1].", [docs]
        ) == []
        assert _flagged("The interval from 100.4 to 110.4 m is 41.8 ft wide [NI43-1].", docs)


# ---------------------------------------------------------------------------
# Finding 2 -- the counts and percentages an analyst works out from the rows
# ---------------------------------------------------------------------------


class TestCountsAndSharesAreRecountedFromTheRows:
    def test_a_correct_count_of_samples_is_grounded(self) -> None:
        _assert_not_literal(6, _assays())
        assert verify_numbers("6 samples returned more than 2.0 g/t Au [DATA-1].", [_assays()]) == []

    def test_a_correct_count_of_holes_is_grounded(self) -> None:
        _assert_not_literal(4, _assays())
        assert verify_numbers("4 holes intersected more than 2.0 g/t Au [DATA-1].", [_assays()]) == []

    @pytest.mark.parametrize("count", ["5", "7", "8", "9", "10", "11"])
    def test_a_wrong_count_is_flagged(self, count: str) -> None:
        assert _flagged(f"{count} holes intersected more than 2.0 g/t Au [DATA-1].", _assays())

    def test_the_bound_may_be_strict_or_not(self) -> None:
        """Samples of 2.2 g/t and up: six. Strictly above it: five. A sentence
        does not say which it means, so either recount grounds the figure --
        and nothing else does."""
        _assert_not_literal(5, _assays())
        assert verify_numbers("6 samples returned at least 2.2 g/t Au [DATA-1].", [_assays()]) == []
        assert verify_numbers("5 samples returned more than 2.2 g/t Au [DATA-1].", [_assays()]) == []
        assert _flagged("7 samples returned more than 2.2 g/t Au [DATA-1].", _assays())
        assert _flagged("8 samples returned at least 2.2 g/t Au [DATA-1].", _assays())

    def test_a_bound_below(self) -> None:
        """Below 1.0 g/t: five samples in four holes."""
        assert verify_numbers("5 samples returned less than 1.0 g/t Au [DATA-1].", [_assays()]) == []
        assert verify_numbers("4 holes returned less than 1.0 g/t Au [DATA-1].", [_assays()]) == []
        assert _flagged("9 samples returned less than 1.0 g/t Au [DATA-1].", _assays())

    def test_a_correct_share_of_the_rows(self) -> None:
        # 6 of 12 samples exceed 2.0 g/t; 4 of 6 holes
        assert verify_numbers("About 50% of samples exceed 2.0 g/t Au [DATA-1].", [_assays()]) == []
        assert verify_numbers("67% of holes intersected more than 2.0 g/t Au [DATA-1].", [_assays()]) == []

    def test_a_wrong_share_is_flagged(self) -> None:
        assert _flagged("About 42% of samples exceed 2.0 g/t Au [DATA-1].", _assays())
        assert _flagged("About 90% of samples exceed 2.0 g/t Au [DATA-1].", _assays())

    def test_a_count_with_no_bound_is_not_recounted(self) -> None:
        """"87 drill holes": nothing in the sentence says what was counted."""
        assert _flagged("There are 87 drill holes [DATA-1].", _assays(), _collars())
        assert _flagged("9 holes were sampled [DATA-1].", _assays())

    def test_the_bound_must_be_a_bound(self) -> None:
        """"returned 2.0 g/t" is not "more than 2.0 g/t"."""
        assert _flagged("6 holes returned 2.0 g/t Au [DATA-1].", _assays())

    def test_depth_counts_use_the_depth_series(self) -> None:
        """Deeper than 300 m: 301, 345, 378, 402.5, 431, 455, 480 -- seven."""
        _assert_not_literal(8, _collars())
        assert verify_numbers("7 of the 12 holes are deeper than 300 m [DATA-1].", [_collars()]) == []
        assert _flagged("8 of the 12 holes are deeper than 300 m [DATA-1].", _collars())

    def test_a_share_of_two_stated_counts(self) -> None:
        assert verify_numbers(
            "7 of the 12 holes (58%) are deeper than 300 m [DATA-1].", [_collars()]
        ) == []
        assert _flagged("7 of the 12 holes (88%) are deeper than 300 m [DATA-1].", _collars())


class TestMultiplesOfTheToolsOwnAggregates:
    """"6 times the median" is max / median, "2.7 standard deviations" is
    (max - mean) / std: figures worked out from numbers the tool returned for
    the same rows. A ratio of ANY two grounded numbers would ground nearly any
    figure, which is why only one result's own aggregates are divided."""

    EVIDENCE = ("query_assay_data", {
        "element": "Au_ppm", "count": 9, "total_count": 9, "samples": [],
        "min_value": 0.4, "max_value": 9.6, "mean_value": 3.2, "median_value": 1.6, "std_value": 2.4,
    })

    @pytest.mark.parametrize(
        "answer",
        [
            "The peak grade is 6 times the median [DATA-1].",
            "The peak grade is about 3.0 times the mean [DATA-1].",
            "The peak grade is 2.7 standard deviations above the mean [DATA-1].",
        ],
    )
    def test_a_correct_multiple(self, answer: str) -> None:
        assert verify_numbers(answer, [self.EVIDENCE]) == []

    @pytest.mark.parametrize(
        "answer",
        [
            "The peak grade is 49 times the median [DATA-1].",
            "The peak grade is 4.4 standard deviations above the mean [DATA-1].",
        ],
    )
    def test_a_wrong_multiple_is_flagged(self, answer: str) -> None:
        assert _flagged(answer, self.EVIDENCE)

    def test_a_standard_deviation_needs_the_tools_std(self) -> None:
        no_std = ("query_assay_data", {
            "element": "Au_ppm", "count": 9, "total_count": 9, "samples": [],
            "min_value": 0.4, "max_value": 9.6, "mean_value": 3.2, "median_value": 1.6,
        })
        assert _flagged("The peak grade is 2.7 standard deviations above the mean [DATA-1].", no_std)

    def test_a_bare_number_is_not_a_multiple_unless_it_is_written_as_one(self) -> None:
        assert _flagged("The peak grade is 6 [DATA-1].", self.EVIDENCE)


# ---------------------------------------------------------------------------
# Finding 3 -- "million" is not a licence for any unit
# ---------------------------------------------------------------------------


class TestAScaledFigureKeepsItsUnit:
    @pytest.mark.parametrize(
        ("evidence", "claim"),
        [
            ("Indicated resources are 48.2 million tonnes.", "Contained gold is 48,200,000 ounces"),
            ("Contained U3O8 is 71 million pounds.", "The deposit holds 71,000,000 tonnes"),
            ("Contained gold is 2.5 million ounces.", "Resources are 2,500,000 tonnes"),
            ("Indicated resources are 48.2 Mt.", "Contained gold is 48,200,000 ounces"),
        ],
    )
    def test_another_dimension_is_flagged(self, evidence: str, claim: str) -> None:
        assert _flagged(f"{claim} [NI43-1].", _docs(evidence))

    @pytest.mark.parametrize(
        ("evidence", "claim"),
        [
            ("Indicated resources are 48.2 million tonnes.", "Indicated resources are 48,200,000 tonnes"),
            ("Indicated resources are 48.2 million tonnes.", "Indicated resources are 48.2 Mt"),
            ("Indicated resources are 48.2 million tonnes.", "Indicated resources are 48,200 kt"),
            ("Contained U3O8 is 71 million pounds.", "Contained U3O8 is 71,000,000 pounds"),
            ("Contained gold is 2.5 million ounces.", "Contained gold is 2,500,000 ounces"),
            ("Contained gold is 2.5 million ounces.", "Contained gold is 2.5 Moz"),
        ],
    )
    def test_the_same_dimension_still_matches(self, evidence: str, claim: str) -> None:
        assert verify_numbers(f"{claim} [NI43-1].", [_docs(evidence)]) == []

    def test_a_figure_with_no_unit_matches_a_scaled_one(self) -> None:
        """"US$425 million" is 425,000,000 -- and states no unit to contradict."""
        docs = _docs("The after-tax NPV is US$425 million.")
        assert verify_numbers("The NPV is 425,000,000 [NI43-1].", [docs]) == []
        assert verify_numbers("The NPV is US$425,000,000 [NI43-1].", [docs]) == []
        assert _flagged("The NPV is 4,250,000,000 [NI43-1].", docs)

    def test_the_scaled_figure_is_not_in_the_unit_blind_literals(self) -> None:
        ev = _collect_evidence([_docs("Indicated resources are 48.2 million tonnes.")])
        assert 48.2 in ev.literal and 48_200_000.0 not in ev.literal
        assert (48_200_000.0, "mass_t") in ev.scaled


# ---------------------------------------------------------------------------
# Finding 4 -- the derived window
# ---------------------------------------------------------------------------


class TestTheAggregatesAreNotPartOfTheWindow:
    CAPPED = ("query_assay_data", {
        "element": "Au_ppb", "count": 5, "total_count": 6000,
        "samples": [
            {"hole_id": f"H{i}", "element": "Au_ppb", "value": v}
            for i, v in enumerate((0.5, 1.2, 3.4, 8.8, 45.0))
        ],
        "min_value": 0.5, "max_value": 1_250_000.0, "mean_value": 208_509.8, "median_value": 6.1,
    })

    def test_a_full_set_mean_does_not_stretch_the_window_of_the_capped_rows(self) -> None:
        """Samples of 0.5-45 ppb with a full-set mean of 208,510 ppb: folding
        mean_value into the rows' series made 0.07 g/t (70 ppb) 'derivable'."""
        assert _flagged("The mean grade is 0.07 g/t Au [DATA-1].", self.CAPPED)
        assert _flagged("The mean grade is 0.24 g/t Au [DATA-1].", self.CAPPED)

    def test_the_aggregates_still_ground_themselves_and_convert(self) -> None:
        assert verify_numbers("The mean grade is 208.5 g/t Au [DATA-1].", [self.CAPPED]) == []
        assert verify_numbers("The maximum is 1,250 g/t Au [DATA-1].", [self.CAPPED]) == []

    def test_a_mean_of_the_rows_is_still_a_statistic_of_them(self) -> None:
        assert verify_numbers("The mean grade is 1.9 g/t Au [DATA-1].", [_assays()]) == []
        assert verify_numbers("The mean grade is 0.02 g/t Au [DATA-1].", [self.CAPPED]) == []


class TestADerivedFigureNeedsASentenceThatSaysSo:
    def test_a_peak_in_the_range_is_not_a_statistic(self) -> None:
        """2.75 g/t is inside [0.15, 3.3] and in no sample."""
        assert _flagged("The best intercept grades 2.75 g/t Au [DATA-1].", _assays())
        assert _flagged("The best interval runs from 212.5 to 218.0 m [DATA-1].", _collars())

    def test_the_same_figure_in_a_statistic_sentence_is_accepted(self) -> None:
        """The weak spot, stated plainly: a fabricated MEAN inside the range
        of the rows cannot be told from the mean of some subset of them."""
        assert verify_numbers("The mean grade is 2.75 g/t Au [DATA-1].", [_assays()]) == []
        assert verify_numbers("The average hole depth is 410 m [DATA-1].", [_collars()]) == []
        # ...and outside the range it is caught
        assert _flagged("The mean grade is 3.9 g/t Au [DATA-1].", _assays())
        assert _flagged("The average hole depth is 530 m [DATA-1].", _collars())

    @pytest.mark.parametrize("word", ["mean", "average", "median", "percentile", "quartile", "typical"])
    def test_the_statistic_words(self, word: str) -> None:
        assert verify_numbers(f"The {word} depth is 328 m [DATA-1].", [_collars()]) == []

    def test_a_threshold_inside_the_range_is_accepted(self) -> None:
        assert verify_numbers("Holes deeper than 250 m were re-surveyed [DATA-1].", [_collars()]) == []
        assert verify_numbers("Samples above 2.5 g/t Au were re-assayed [DATA-1].", [_assays()]) == []
        assert verify_numbers(
            "Samples above a 2.7 g/t cut-off were re-assayed [DATA-1].", [_assays()]
        ) == []

    def test_a_threshold_outside_the_range_is_not(self) -> None:
        assert _flagged("Holes deeper than 900 m were re-surveyed [DATA-1].", _collars())
        assert _flagged("Samples above 12.5 g/t Au were re-assayed [DATA-1].", _assays())

    def test_over_is_a_bound_for_a_grade_but_a_width_for_a_length(self) -> None:
        """"2.9 g/t over 250 m" is an intercept width; "samples over 2.7 g/t"
        is a bound."""
        assert verify_numbers("Samples over 2.7 g/t Au were re-assayed [DATA-1].", [_assays()]) == []
        assert _flagged("The hole returned 2.9 g/t Au over 250 m [DATA-1].", _assays(), _collars())

    def test_a_bare_number_is_a_statistic_only_if_it_is_the_statistic(self) -> None:
        assert verify_numbers("The average depth is 328.4 [DATA-1].", [_collars()]) == []
        assert verify_numbers("The holes average 328.4 deep [DATA-1].", [_collars()]) == []
        # a count of holes is not a depth, however deep the sentence is
        assert _flagged("The 250 holes average 328 m deep [DATA-1].", _collars())
        assert _flagged("The average is 328.4 [DATA-1].", _collars())  # of what?


class TestASentenceIsAboutItsSubject:
    COPPER = ("query_assay_data", {
        "element": "Cu_pct", "count": 4, "total_count": 4, "samples": [
            {"hole_id": f"H{i}", "element": "Cu_pct", "value": v}
            for i, v in enumerate((0.1, 0.5, 2.0, 3.5))
        ],
    })
    GOLD = ("query_assay_data", {
        "element": "Au_ppm", "count": 4, "total_count": 4, "samples": [
            {"hole_id": f"H{i}", "element": "Au_ppm", "value": v}
            for i, v in enumerate((0.02, 0.1, 0.3, 0.4))
        ],
    })

    def test_gold_is_not_averaged_over_copper(self) -> None:
        """Copper runs 1,000-35,000 ppm; gold 0.02-0.4 ppm."""
        assert _flagged("Gold averages 3,000 ppm Au [DATA-1].", self.COPPER, self.GOLD)
        assert verify_numbers("Gold averages 0.2 ppm Au [DATA-1].", [self.COPPER, self.GOLD]) == []
        assert verify_numbers("Copper averages 12,000 ppm Cu [DATA-1].", [self.COPPER, self.GOLD]) == []
        # 0.2 ppm is a mean of the gold samples, not of the copper ones
        assert _flagged("Copper averages 0.2 ppm Cu [DATA-1].", self.COPPER, self.GOLD)

    def test_a_sentence_about_dip_is_not_about_depth(self) -> None:
        dipping = _collars(
            dips=(-55.0, -60.0, -65.0, -70.0, -62.0, -58.0, -75.0, -80.0, -60.0, -60.0, -90.0, -65.0)
        )
        assert verify_numbers("The average dip is -64 degrees [DATA-1].", [dipping]) == []
        assert _flagged("The average dip is -30 degrees [DATA-1].", dipping)


# ---------------------------------------------------------------------------
# Finding 6 -- sentinels
# ---------------------------------------------------------------------------


class TestSentinelsDoNotStretchTheWindow:
    @pytest.mark.parametrize("sentinel", [9999.0, 99999.0, 999999.0])
    def test_a_positive_null_code_is_no_depth(self, sentinel: float) -> None:
        payload = _collars((152.0, 187.5, 203.0, 240.0, sentinel))
        assert _flagged("The average hole depth is 1,849 m [DATA-1].", payload)
        assert verify_numbers("The average hole depth is 195 m [DATA-1].", [payload]) == []

    def test_a_zero_metre_hole_is_no_depth(self) -> None:
        payload = _collars((0.0, 187.5, 203.0, 240.0, 266.5))
        assert _flagged("The average hole depth is 12.5 m [DATA-1].", payload)
        assert _flagged("The average hole depth is 40 m [DATA-1].", payload)

    def test_no_hole_is_deeper_than_15_km(self) -> None:
        payload = _collars((152.0, 187.5, 203.0, 240.0, 14_000.0, 20_000.0))
        assert _flagged("The average hole depth is 17,300 m [DATA-1].", payload)
        # 14,000 m is deep but possible
        assert verify_numbers("The average hole depth is 5,300 m [DATA-1].", [payload]) == []

    def test_a_null_code_is_still_a_literal(self) -> None:
        """It is in the data: quoting it back is not a fabrication."""
        payload = _collars((152.0, 187.5, 203.0, 240.0, 9999.0))
        assert verify_numbers("One hole is recorded as 9999 m deep [DATA-1].", [payload]) == []

    def test_a_grade_null_code_does_not_stretch_a_grade_window(self) -> None:
        payload = ("query_assay_data", {
            "element": "Au_ppm", "count": 4, "total_count": 4, "samples": [
                {"hole_id": f"H{i}", "element": "Au_ppm", "value": v}
                for i, v in enumerate((1.0, 2.0, 3.0, 99999.0))
            ],
        })
        assert _flagged("The mean grade is 400 g/t Au [DATA-1].", payload)

    def test_a_zero_elevation_is_not_a_collar_height(self) -> None:
        payload = _collars((152.0, 187.5, 203.0), elevations=(0.0, 515.0, 517.0))
        assert _flagged("Average collar elevation is 100 m [DATA-1].", payload)
        assert verify_numbers("Average collar elevation is 516 m [DATA-1].", [payload]) == []


# ---------------------------------------------------------------------------
# Finding 7 -- a family converts into itself with 1
# ---------------------------------------------------------------------------


class TestOuncesPerTonOnlyConvertIntoOtherUnits:
    def test_the_same_family_is_a_factor_of_one(self) -> None:
        assert _conversion_factors("conc_ozt", "conc_ozt") == (1.0,)
        assert _conversion_factors("length_m", "length_m") == (1.0,)

    def test_both_conventions_still_apply_to_a_conversion_out_of_oz_per_t(self) -> None:
        factors = _conversion_factors("conc_ozt", "conc_ppm")
        assert any(abs(f - 34.2857) < 1e-3 for f in factors)
        assert any(abs(f - 31.1035) < 1e-3 for f in factors)

    def test_one_ten_is_not_a_restatement_of_one_hundred(self) -> None:
        docs = _docs("Historic grade was 1.00 oz/t Au.")
        assert _flagged("Historic grade was 1.10 oz/t Au [NI43-1].", docs)
        assert _flagged("Historic grade was 0.91 oz/t Au [NI43-1].", docs)
        assert verify_numbers("Historic grade was 1.00 oz/t Au [NI43-1].", [docs]) == []

    def test_oz_per_t_still_converts_from_g_per_t(self) -> None:
        docs = _docs("Gold grade is 37.3 g/t.")
        assert verify_numbers("That is 1.09 oz/ton gold [NI43-1].", [docs]) == []  # short ton
        assert verify_numbers("That is 1.2 oz/t gold [NI43-1].", [docs]) == []  # metric tonne


# ---------------------------------------------------------------------------
# The lookups are bisects now; they must agree with the scan they replaced
# ---------------------------------------------------------------------------


class TestTheBisectLookupAgreesWithTheScan:
    def test_near_any_equals_matches_grounded(self) -> None:
        import random

        from app.agent.hallucination.orchestrator_validators import _matches_grounded, _near_any

        rng = random.Random(7)
        magnitudes = sorted(abs(rng.uniform(0, 1000)) for _ in range(500))
        for _ in range(3000):
            value = rng.choice([-1, 1]) * rng.uniform(0, 1100)
            tolerance = rng.choice([0.05, 0.5, 5.0, 25.0])
            assert _near_any(value, tolerance, magnitudes) == _matches_grounded(
                value, tolerance, magnitudes
            )

    def test_the_edges_of_the_window_hold(self) -> None:
        from app.agent.hallucination.orchestrator_validators import _near_any

        assert _near_any(12.0, 0.5, [12.5]) and _near_any(12.0, 0.5, [11.5])
        assert not _near_any(12.0, 0.5, [12.51]) and not _near_any(12.0, 0.5, [11.49])
        assert _near_any(-60.0, 0.5, [60.0])  # a dip is compared by magnitude
        assert not _near_any(12.0, 0.5, [])

    def test_a_long_answer_against_a_large_result_stays_fast(self) -> None:
        import time

        collars = _collars(tuple(100.0 + i for i in range(4000)))
        answer = " ".join(f"{300 + i * 1.1:.1f} m" for i in range(300)) + " [DATA-1]."
        start = time.perf_counter()
        verify_numbers(answer, [collars])
        assert time.perf_counter() - start < 3.0, "was ~4.5 s of event loop before the index"


# ---------------------------------------------------------------------------
# End to end: the review's three precision repros no longer force a retry
# ---------------------------------------------------------------------------


class _Deps:
    project_id = "5e2b8c1d-7a4f-4e39-b6d0-91c3a8f2e7b4"
    pg_pool = None
    neo4j_driver = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        TestLabelsRanksAndListPositionsAreNotClaims.ANSWER,
        "The 75th percentile depth is 402.5 m, [DATA-1] from the top 5 deepest holes [DATA-1].",
        "Holes average a 328.4-m depth and a 517.5 m collar elevation [DATA-1].",
    ],
)
async def test_the_answer_is_not_floored_and_bannered(answer: str) -> None:
    citation = Citation(
        citation_id="[DATA-1]", citation_type="DATA", source_chunk_id="query_spatial_collars:1",
        document_title="Collars", relevance_score=0.9,
    )
    response = GeoRAGResponse(
        text=answer, citations=[citation], confidence=0.9, sources_used=[citation.source_chunk_id]
    )
    _, warnings, should_retry = await run_post_assembly_validation(response, [_collars()], _Deps())  # type: ignore[arg-type]
    assert not [w for w in warnings if w.startswith("Layer 3")], warnings
    assert should_retry is False
