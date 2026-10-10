"""Layer 3 reads a number with its unit (2026-10-10 audit, findings 1-3).

Three defects in how `verify_numbers` grounded a number, all of one kind: it
did not know what the number was.

1. **The derived-statistic window grounded fabrications.** Every int / float
   field of a structured tool result went into one list, and an answer number
   within 0.5x-2x of ANY of them was accepted whatever its field or unit. With
   12 assay samples and 12 collars in evidence, "7.44 g/t Au over 12.6 m",
   "87 drill holes", "a mean grade of 9.9 g/t" and "48.2 Mt at 3.37 g/t" all
   returned no warning at all.
2. **Unit-blind conversion factors.** 10,000 / 31.1035 / 3.28084 / 1,000 were
   applied to every grounded value, so "850 ppm" was grounded by "0.85 %"
   (x1000, the ppb factor) and "18,500 ppb" by "1.85 g/t" (x10,000).
3. **The common restatements were missing.** "48.2 Mt" for 48,200,000 tonnes,
   "71 million pounds", "2.5 million ounces" and "0.054 oz/ton" for 1.85 g/t
   were all reported as invented.

These tests use the real result dataclasses, with the identifiers production
carries, rather than one-key dicts.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.hallucination.orchestrator_validators import (
    _conversion_factors,
    _scan_quantities,
    verify_numbers,
)
from app.agent.tools import AssayDataResult, AssaySample, CollarRecord, SpatialQueryResult

# ---------------------------------------------------------------------------
# Evidence: the audit's payload -- 12 assay samples and 12 collars
# ---------------------------------------------------------------------------

_GRADES = (0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 0.9, 1.2, 1.8, 2.5, 3.2, 4.0)


def _assays(element: str = "Au_ppm", scale: float = 1.0) -> tuple[str, AssayDataResult]:
    """12 samples of 0.05-4.0 (g/t when ``element`` says ppm), from 0 to 110 m."""
    grades = [g * scale for g in _GRADES]
    return ("query_assay_data", AssayDataResult(
        samples=[
            AssaySample(
                hole_id=f"DDH-{i}", collar_id=f"6b1e0c52-4f7a-4d2b-9c3e-7a8f9d0e1b{i:02d}",
                from_depth=float(i * 10), to_depth=float(i * 10 + 1),
                element=element, value=value, sample_type="core",
            )
            for i, value in enumerate(grades)
        ],
        count=12, element=element, available_elements=[element],
        min_value=min(grades), max_value=max(grades), mean_value=sum(grades) / 12,
        median_value=(grades[5] + grades[6]) / 2,
        data_source="PostGIS silver.samples", total_count=12,
    ))


def _collars() -> tuple[str, SpatialQueryResult]:
    """12 collars, azimuth 45, dip -60, total depths 320-540 m, 20 in the project."""
    return ("query_spatial_collars", SpatialQueryResult(
        collars=[
            CollarRecord(
                hole_id=f"DDH-{i}", collar_id=f"0f7d3c2e-9b1a-4d8e-a6c5-3e2f1d0c9b{i:02d}",
                easting=512345.7 + i, northing=6543210.1 + i, elevation=410.0,
                total_depth=320.0 + i * 20.0, hole_type="DD", azimuth=45.0,
                dip=-60.0, status="complete", drill_date=None,
            )
            for i in range(12)
        ],
        count=12, data_source="PostGIS silver.collars", total_count=20,
    ))


def _docs(text: str) -> tuple[str, dict[str, Any]]:
    return ("search_documents", {"chunks": [{"text": text, "document_title": "NI 43-101 report"}]})


# ---------------------------------------------------------------------------
# Finding 1 -- the derived-statistic window
# ---------------------------------------------------------------------------


class TestTheAuditReprosAreFlagged:
    """Each of these returned [] on origin/main."""

    @pytest.mark.parametrize("element", ["Au_ppm", "Au_ppb", "Au"])
    @pytest.mark.parametrize(
        "answer",
        [
            "The best intercept grades 7.44 g/t Au over 12.6 m [DATA-1]",
            "There are 87 drill holes [DATA-2]",
            "The mean grade is 9.9 g/t [DATA-1]",
            "The resource is 48.2 Mt at 3.37 g/t Au [DATA-1]",
        ],
    )
    def test_the_answer_is_flagged(self, answer: str, element: str) -> None:
        # Au_ppb: the same samples read as ppb, so 7.44 / 9.9 g/t are 7,440 /
        # 9,900 ppb -- still outside 50-4,000 ppb. "Au": no unit in the key at
        # all, so a grade has no series to be derived from.
        scale = 1000.0 if element == "Au_ppb" else 1.0
        assert verify_numbers(answer, [_assays(element, scale), _collars()])

    def test_the_best_intercept_is_flagged_on_both_numbers(self) -> None:
        warnings = verify_numbers(
            "The best intercept grades 7.44 g/t Au over 12.6 m [DATA-1]", [_assays(), _collars()]
        )
        assert "7.44" in " ".join(warnings)

    def test_an_invented_count_is_flagged_not_absorbed_by_a_depth(self) -> None:
        warnings = verify_numbers("There are 87 drill holes [DATA-2]", [_assays(), _collars()])
        assert len(warnings) == 1
        assert "87" in warnings[0]


class TestWhatTheWindowWasFor:
    """The mean / median / percentile of a structured series still passes."""

    def test_the_average_depth_over_the_collars(self) -> None:
        """The Phase 5 follow-up case: "average depth is 375.3 m"."""
        assert verify_numbers(
            "The average depth is 375.3 m [DATA-2]", [_assays(), _collars()]
        ) == []

    @pytest.mark.parametrize("claim", ["320.4 m", "431.8 m", "539.6 m", "479 m"])
    def test_any_depth_between_the_shallowest_and_the_deepest(self, claim: str) -> None:
        assert verify_numbers(f"The median depth is {claim} [DATA-2]", [_collars()]) == []

    @pytest.mark.parametrize("claim", ["150 m", "250 m", "319 m", "541 m", "1,080 m"])
    def test_a_depth_outside_them_is_not_a_statistic_of_them(self, claim: str) -> None:
        """150 and 250 sit inside 0.5x-2x of a collar depth; 1,080 is exactly 2x."""
        assert verify_numbers(f"The average depth is {claim} [DATA-2]", [_collars()])

    @pytest.mark.parametrize("element", ["Au_ppm", "Au_ppb"])
    def test_a_grade_between_the_lowest_and_highest_sample(self, element: str) -> None:
        """A percentile of the samples, written in g/t whatever unit the key is."""
        scale = 1000.0 if element == "Au_ppb" else 1.0
        assert verify_numbers(
            "The upper-quartile grade is 1.45 g/t Au [DATA-1]", [_assays(element, scale)]
        ) == []

    def test_the_stored_aggregates_are_literal(self) -> None:
        assays = _assays()
        result = assays[1]
        assert verify_numbers(
            f"The maximum grade is {result.max_value} g/t Au and the minimum "
            f"{result.min_value} g/t Au [DATA-1]",
            [assays],
        ) == []

    def test_a_row_count_is_literal_and_so_is_the_project_total(self) -> None:
        assert verify_numbers("There are 20 drill holes [DATA-2]", [_collars()]) == []
        assert verify_numbers("Twelve of the 20 holes returned assays [DATA-2]", [_collars()]) == []

    def test_an_orientation_statistic_needs_a_unit_too(self) -> None:
        assert verify_numbers("The holes are drilled at -60 degrees [DATA-2]", [_collars()]) == []
        assert verify_numbers("The holes are drilled at -75 degrees [DATA-2]", [_collars()])
        assert verify_numbers("The holes are drilled at -75 [DATA-2]", [_collars()])

    def test_a_sample_interval_width_is_grounded_by_its_two_depths(self) -> None:
        """"over 1 m" for a sample from 30 to 31 m is arithmetic on two
        grounded numbers; 12.6 m is not."""
        assert verify_numbers("DDH-3 returned 0.3 g/t Au over 1.0 m [DATA-1]", [_assays()]) == []
        assert verify_numbers("DDH-3 returned 0.3 g/t Au over 12.6 m [DATA-1]", [_assays()])

    def test_long_lithology_intervals_do_not_widen_the_window_for_other_lengths(self) -> None:
        """An interval's width grounds a restatement of THAT width; it is not a
        series to take a range of. Lithology intervals run to tens of metres, so
        a [0.5, 150] range of widths would have grounded any intercept length."""
        logs = ("query_downhole_logs", {
            "count": 3,
            "intervals": [
                {"hole_id": "H-1", "from_depth": 0.0, "to_depth": 0.5, "lithology_code": "SS"},
                {"hole_id": "H-1", "from_depth": 0.5, "to_depth": 24.9, "lithology_code": "SS"},
                {"hole_id": "H-1", "from_depth": 24.9, "to_depth": 174.9, "lithology_code": "SH"},
            ],
        })
        assert verify_numbers("A 24.4 m thick sandstone unit [DATA-1].", [logs]) == []
        assert verify_numbers("The shale is 150 m thick [DATA-1].", [logs]) == []
        assert verify_numbers("A 12.6 m thick sandstone unit [DATA-1].", [logs])
        assert verify_numbers("An 88 m thick sandstone unit [DATA-1].", [logs])

    def test_document_prose_has_no_series_to_derive_from(self) -> None:
        docs = _docs("The programme comprised 14 holes to a maximum depth of 455 m.")
        assert verify_numbers("The average depth was 383 m [NI43-1].", [docs])


# ---------------------------------------------------------------------------
# Finding 2 -- a factor belongs to a pair of units
# ---------------------------------------------------------------------------


class TestFactorsBelongToUnitPairs:
    def test_the_x1000_error_between_ppm_and_percent_is_flagged(self) -> None:
        docs = _docs("The Indicated resource averages 0.85% U3O8.")
        assert verify_numbers("The grade is 850 ppm U3O8 [NI43-1].", [docs])
        # the real conversion, 1 % = 10,000 ppm, is fine
        assert verify_numbers("The grade is 8,500 ppm U3O8 [NI43-1].", [docs]) == []

    def test_the_x10000_error_between_ppb_and_g_per_t_is_flagged(self) -> None:
        docs = _docs("Channel samples averaged 1.85 g/t Au.")
        assert verify_numbers("Channel samples averaged 18,500 ppb Au [NI43-1].", [docs])
        assert verify_numbers("Channel samples averaged 1,850 ppb Au [NI43-1].", [docs]) == []

    def test_a_structured_grade_converts_by_the_unit_in_its_element_key(self) -> None:
        assays = _assays("Au_ppb", 1000.0)  # 50 ... 4,000 ppb
        assert verify_numbers("The highest sample is 4.0 g/t Au [DATA-1]", [assays]) == []
        # 45 g/t is 45,000 ppb, ten times the highest sample (and, unlike 40,
        # not the depth of a sample interval)
        assert verify_numbers("The highest sample is 45 g/t Au [DATA-1]", [assays])

    def test_a_bare_number_is_never_converted(self) -> None:
        """No unit written, no unit known: it is a literal match or nothing.
        "656" is 200 m in feet, but nothing says the answer means feet."""
        docs = _docs("The hole reached 200 m.")
        assert verify_numbers("The hole reached 656 [NI43-1].", [docs])
        assert verify_numbers("The hole reached 656 ft [NI43-1].", [docs]) == []

    def test_a_range_shares_its_unit(self) -> None:
        docs = _docs("The zone extends from 200 m to 300 m.")
        assert verify_numbers("The zone extends from 656 to 984 ft [NI43-1].", [docs]) == []
        assert verify_numbers("The zone extends between 656 and 984 ft [NI43-1].", [docs]) == []

    @pytest.mark.parametrize(
        ("source", "target", "factor"),
        [
            ("conc_pct", "conc_ppm", 1e4),
            ("conc_ppm", "conc_pct", 1e-4),
            ("conc_ppm", "conc_ppb", 1e3),
            ("conc_ppb", "conc_pct", 1e-7),
            ("length_m", "length_ft", 3.28084),
            ("length_km", "length_m", 1e3),
            ("mass_t", "mass_mt", 1e-6),
            ("mass_kt", "mass_mt", 1e-3),
            ("mass_kg", "mass_lb", 2.20462),
            ("mass_t", "mass_st", 1.10231),
            ("oz", "oz_m", 1e-6),
            ("mass_lb", "mass_mlb", 1e-6),
        ],
    )
    def test_the_factor_between_two_families_is_the_real_one(
        self, source: str, target: str, factor: float
    ) -> None:
        assert any(
            abs(f - factor) <= 1e-4 * factor for f in _conversion_factors(source, target)
        ), _conversion_factors(source, target)

    @pytest.mark.parametrize(
        ("source", "target"),
        [
            ("conc_pct", "length_m"),
            ("mass_t", "oz"),
            ("conc_ppm", "mass_t"),
            ("angle_deg", "length_m"),
            ("conc_ppm", "no_such_family"),
        ],
    )
    def test_two_families_of_different_dimensions_do_not_convert(
        self, source: str, target: str
    ) -> None:
        assert _conversion_factors(source, target) == ()


# ---------------------------------------------------------------------------
# Finding 3 -- the restatements an NI 43-101 answer actually makes
# ---------------------------------------------------------------------------


class TestCommonRestatements:
    RESOURCE = (
        "The Indicated resource is 48,200,000 tonnes at 0.85% U3O8, containing "
        "71,000,000 pounds. The gold zone holds 2,500,000 ounces at 1.85 g/t Au."
    )

    @pytest.mark.parametrize(
        "answer",
        [
            "The resource is 48.2 Mt [NI43-1].",
            "The resource is 48.2 million tonnes [NI43-1].",
            "The resource is 48,200 kt [NI43-1].",
            "It contains 71 million pounds [NI43-1].",
            "It contains 71 Mlbs [NI43-1].",
            "It contains 32,205 tonnes of U3O8 metal [NI43-1].",
            "The gold zone holds 2.5 million ounces [NI43-1].",
            "The gold zone holds 2.5 Moz [NI43-1].",
            "The gold zone holds 2,500 koz [NI43-1].",
            "Gold grades 0.054 oz/ton [NI43-1].",
            "Gold grades 0.0595 oz/t [NI43-1].",
            "The resource is about 53.1 million short tons [NI43-1].",
        ],
    )
    def test_a_correct_restatement_is_not_flagged(self, answer: str) -> None:
        assert verify_numbers(answer, [_docs(self.RESOURCE)]) == []

    @pytest.mark.parametrize(
        "answer",
        [
            "The resource is 4.82 Mt [NI43-1].",
            "The resource is 482 Mt [NI43-1].",
            "It contains 7.1 million pounds [NI43-1].",
            "The gold zone holds 25 million ounces [NI43-1].",
            "Gold grades 0.54 oz/ton [NI43-1].",
        ],
    )
    def test_a_restatement_out_by_a_power_of_ten_is_flagged(self, answer: str) -> None:
        assert verify_numbers(answer, [_docs(self.RESOURCE)])

    @pytest.mark.parametrize(
        "answer",
        [
            "The Indicated resource is 48,200,000 tonnes [NI43-1].",
            "It contains 71,000,000 pounds [NI43-1].",
            "The gold zone holds 2,500,000 ounces [NI43-1].",
            "Gold grades 1.85 g/t [NI43-1].",
        ],
    )
    def test_the_long_form_of_a_short_form_in_the_evidence(self, answer: str) -> None:
        docs = _docs(
            "Resource: 48.2 Mt at 0.85% U3O8 for 71 Mlbs; gold zone 2.5 Moz at 0.054 oz/ton."
        )
        assert verify_numbers(answer, [docs]) == []

    @pytest.mark.parametrize(
        ("answer", "flagged"),
        [
            ("The grade is 8,500 parts per million U3O8 [NI43-1].", False),
            ("The grade is 850 parts per million U3O8 [NI43-1].", True),
            ("Gold grades 0.054 ounces per ton [NI43-1].", False),
            ("Gold grades 1,850 parts per billion [NI43-1].", False),
            ("Gold grades 18,500 parts per billion [NI43-1].", True),
            ("The resource grades 0.85 percent U3O8 [NI43-1].", False),
        ],
    )
    def test_prose_units_are_read_like_their_symbols(self, answer: str, flagged: bool) -> None:
        docs = _docs("The Indicated resource averages 0.85% U3O8; gold grades 1.85 g/t.")
        assert bool(verify_numbers(answer, [docs])) is flagged

    def test_a_million_in_the_evidence_grounds_the_written_out_figure(self) -> None:
        docs = _docs("The after-tax NPV is US$425 million.")
        assert verify_numbers("The after-tax NPV is US$425,000,000 [NI43-1].", [docs]) == []
        assert verify_numbers("The after-tax NPV is US$425 million [NI43-1].", [docs]) == []
        assert verify_numbers("The after-tax NPV is US$4,250,000,000 [NI43-1].", [docs])

    def test_a_million_is_only_a_factor_where_it_is_written(self) -> None:
        """48,200,000 tonnes does not ground a bare "48.2"."""
        assert verify_numbers("The resource is 48.2 [NI43-1].", [_docs(self.RESOURCE)])


class TestQuantityScan:
    def test_magnitude_words_and_unit_families(self) -> None:
        scanned = {
            (q.value, q.magnitude, q.family)
            for q in _scan_quantities("48.2 million tonnes and 71 M lb, 22.7 Mlbs, 2.5 Moz")
        }
        assert scanned == {
            (48.2, 1e6, "mass_t"),
            (71.0, 1e6, "mass_lb"),
            (22.7, 1.0, "mass_mlb"),
            (2.5, 1.0, "oz_m"),
        }

    def test_a_capital_m_is_a_magnitude_only_before_a_unit_word(self) -> None:
        assert [(q.magnitude, q.family) for q in _scan_quantities("48.2 M tonnes")] == [
            (1e6, "mass_t")
        ]
        assert [(q.magnitude, q.family) for q in _scan_quantities("48.2 Mt")] == [
            (1.0, "mass_mt")
        ]
        assert [(q.magnitude, q.family) for q in _scan_quantities("48 m of core")] == [
            (1.0, "length_m")
        ]

    def test_a_unit_that_runs_into_more_letters_is_not_a_unit(self) -> None:
        assert [q.family for q in _scan_quantities("5 mm wide, 12 metals, 3 tests")] == [
            None, None, None
        ]
        assert [q.family for q in _scan_quantities("25 °C and 12 km2")] == [None, None]

    def test_both_ends_of_a_range_take_the_unit(self) -> None:
        assert [q.family for q in _scan_quantities("145.2 to 148.0 m")] == ["length_m"] * 2
        assert [q.family for q in _scan_quantities("120-126 m")] == ["length_m"] * 2
        assert [q.family for q in _scan_quantities("0.5 to 1.2% Cu")] == ["conc_pct"] * 2

    def test_a_year_before_a_comma_does_not_take_the_next_number_s_unit(self) -> None:
        assert [q.family for q in _scan_quantities("In 2011, 150 m were drilled")] == [
            None, "length_m"
        ]

    def test_signs_and_degrees(self) -> None:
        scanned = [(q.value, q.family) for q in _scan_quantities("dip of -60 degrees, azimuth 45°")]
        assert scanned == [(-60.0, "angle_deg"), (45.0, "angle_deg")]
