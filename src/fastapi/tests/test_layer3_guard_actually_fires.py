"""Layer 3 was inert. These lock in that it is not.

Two independent defects, both of the same shape: a guard that exists, is
tested in isolation, and cannot fire on the case it was built for.

**Derivation tolerance.** `verify_numbers` accepted any number inside
`[min, max]` of the grounded set, and the grounded set is built by regexing
digit runs out of the JSON-serialised tool results -- ISO timestamps, UTM
eastings, UUID fragments. One realistic collar row gives a range of roughly
[-20, 512345.7], so every plausible geological value fell inside it and was
waved through as "likely average/median". The guard only ever fired on numbers
larger than the biggest coordinate in the payload.

The first repair narrowed that to "within 0.5x-2x of ANY numeric field", which
still ignored what the field was: an azimuth of 45 grounded "40 m", a depth of
320 m grounded "87 drill holes". A derived statistic is now accepted only
against a structured series of the same dimension (`total_depth` for a length,
the assay values for a grade), only inside that series' own [min, max] -- the
one thing a mean, median or percentile is guaranteed to satisfy (2026-10-10
audit, finding 1) -- and, since the review of the same day, only in a sentence
that says it is a statistic or states a bound, about the series it names
(see test_layer3_review_2026_10_10.py).

**Unit families.** `_detect_unit_mismatches` flags a (value, unit) pair only
when every same-valued grounded tuple lives in a different unit family, and
the table put g/t, oz/t, ppm, ppb, wt% and % in one family called
"mass_conc". The entire class of grade-unit errors was therefore invisible by
construction -- including the g/t-versus-percent confusion the config comment
cites as the reason the guard was promoted from shadow to warn.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.agent.hallucination.orchestrator_validators import (
    _detect_unit_mismatches,
    verify_numbers,
)


def _collars(*depths: float | None, **extra: Any) -> tuple[str, dict[str, Any]]:
    """A ``query_spatial_collars`` payload with one collar per depth, carrying
    the usual noise: coordinates, an orientation, a score and a page."""
    return ("query_spatial_collars", {
        "count": len(depths),
        "collars": [
            {
                "hole_id": f"H-{i}", "total_depth": depth, "azimuth": 45.0,
                "dip": -60.0, "easting": 512345.7, "northing": 6543210.1,
                "relevance_score": 0.82, "page": 12, **extra,
            }
            for i, depth in enumerate(depths)
        ],
    })


#: Four real collar depths.
DEPTHS = (310.0, 360.0, 410.0, 455.0)


class TestDerivationTolerance:
    def test_a_genuine_mean_is_accepted(self) -> None:
        """The case the tolerance exists for: mean(DEPTHS) is 383.75."""
        assert verify_numbers("The mean depth is 383.75 m [DATA-1].", [_collars(*DEPTHS)]) == []

    @pytest.mark.parametrize("claim", ["4,500 m", "12 m", "0.4 m", "250 m", "458 m"])
    def test_a_value_outside_the_series_is_not_derived(self, claim: str) -> None:
        """No mean of four depths between 310 and 455 m is 4,500 m -- or, at
        the other end, 250 m or 458 m. The old 0.5x-2x window blessed the
        last two (they sit within 2x of a depth) and the 12 m via the dip.
        (460 m is not here on purpose: written with a trailing zero it is a
        legitimate rounding of 455, which `_written_tolerance` accepts.)"""
        warnings = verify_numbers(f"The average depth is {claim} [DATA-1].", [_collars(*DEPTHS)])
        assert len(warnings) == 1, warnings

    def test_the_edges_of_the_series_hold_to_the_written_precision(self) -> None:
        """A percentile may sit right at the deepest or the shallowest hole,
        rounded either way by half a unit of the last place written."""
        payload = [_collars(*DEPTHS)]
        assert verify_numbers("The upper-quartile depth is 454.9 m [DATA-1].", payload) == []
        assert verify_numbers("The lower-quartile depth is 310.04 m [DATA-1].", payload) == []
        assert verify_numbers("The upper-quartile depth is 455.3 m [DATA-1].", payload)
        assert verify_numbers("The lower-quartile depth is 309.7 m [DATA-1].", payload)

    def test_coordinates_and_timestamps_no_longer_launder_a_fabrication(self) -> None:
        """The headline defect: 450 m invented against a payload of noise --
        a timestamp, a UTM coordinate, a score and a page number, and no
        depth at all."""
        noise = ("query_spatial_collars", {
            "count": 1,
            "collars": [{
                "hole_id": "H-1", "total_depth": None, "elevation": 11.0,
                "easting": 512345.7, "northing": 6543210.1,
                "drill_date": "2026-03-11T08:14:12Z", "relevance_score": 0.82,
                "page": 12, "section_number": "14.2",
            }],
        })
        assert len(verify_numbers("The hole reached a depth of 450 m [DATA-1].", [noise])) == 1

    def test_magnitude_not_signed_range(self) -> None:
        """A dip is judged by magnitude: the evidence says -60 and -55, the
        answer writes an unsigned 57.5 degrees."""
        payload = ("query_spatial_collars", {
            "count": 2,
            "collars": [{"hole_id": "H-1", "dip": -60.0}, {"hole_id": "H-2", "dip": -55.0}],
        })
        assert verify_numbers("The average dip is 57.5 degrees [DATA-1].", [payload]) == []
        assert verify_numbers("The average dip is -57.5° [DATA-1].", [payload]) == []
        assert verify_numbers("The average dip is 70 degrees [DATA-1].", [payload])

    def test_a_derived_statistic_needs_a_unit(self) -> None:
        """"87 drill holes" is a count, and a count is literal or it is wrong:
        the collar depths (320-540 m) put 87 within no window of their own."""
        warnings = verify_numbers("There are 87 drill holes [DATA-1].", [_collars(*DEPTHS)])
        assert len(warnings) == 1

    def test_a_derived_statistic_needs_a_series_of_its_own_dimension(self) -> None:
        """Depths are lengths; a grade written in g/t is not one of them."""
        warnings = verify_numbers("The mean grade is 380 g/t Au [DATA-1].", [_collars(*DEPTHS)])
        assert len(warnings) == 1

    def test_a_sentinel_depth_does_not_stretch_the_series(self) -> None:
        """-999 marks a missing depth in a lot of drill databases; one such
        row used to make every depth from 0 up "derivable"."""
        payload = _collars(*DEPTHS)
        payload[1]["collars"].append({"hole_id": "H-9", "total_depth": -999.0})
        assert verify_numbers("The average depth is 150 m [DATA-1].", [payload])

    def test_an_interval_width_is_a_derived_figure_of_two_grounded_depths(self) -> None:
        """"over 2.8 m" for a sample from 145.2 to 148.0 m: nobody queried
        the width, and it is not a coincidence of some other row."""
        samples = ("query_assay_data", {
            "count": 1, "element": "Au_ppm",
            "samples": [{"hole_id": "H-1", "from_depth": 145.2, "to_depth": 148.0, "value": 2.31}],
        })
        assert verify_numbers(
            "H-1 returned 2.31 g/t Au over 2.8 m [DATA-1].", [samples]
        ) == []
        assert verify_numbers(
            "H-1 returned 2.31 g/t Au over 12.6 m [DATA-1].", [samples]
        )

    def test_no_grounded_values_derives_nothing(self) -> None:
        assert verify_numbers("The mean depth is 383.75 m [DATA-1].", [("query_spatial_collars", {"count": 0})])


class TestUnitFamilies:
    @pytest.mark.parametrize(
        ("reported_unit", "why"),
        [
            ("%", "1.85% for 1.85 g/t is a 10,000x error"),
            ("ppb", "1.85 ppb for 1.85 g/t is a 1,000x error"),
            ("oz/t", "1.85 oz/t for 1.85 g/t is a ~34x error"),
        ],
    )
    def test_grade_unit_swaps_are_flagged(self, reported_unit: str, why: str) -> None:
        warnings = _detect_unit_mismatches(
            [(1.85, reported_unit)],
            [(1.85, "g/t")],
        )

        assert warnings, why

    def test_g_per_tonne_and_ppm_are_the_same_unit(self) -> None:
        """1 g/t IS 1 ppm. Flagging it would be a false positive."""
        assert not _detect_unit_mismatches([(1.85, "ppm")], [(1.85, "g/t")])

    def test_metres_reported_as_feet_is_flagged(self) -> None:
        assert _detect_unit_mismatches([(500.0, "ft")], [(500.0, "m")])

    def test_evidence_carrying_both_units_is_not_a_mismatch(self) -> None:
        assert not _detect_unit_mismatches(
            [(500.0, "m")],
            [(500.0, "m"), (500.0, "ft")],
        )

    def test_a_value_absent_from_the_evidence_is_left_to_the_grounding_check(self) -> None:
        """Not this guard's job to re-flag an ungrounded number."""
        assert not _detect_unit_mismatches([(12.5, "m")], [(1.85, "g/t")])

    def test_an_unrecognised_grounded_unit_is_not_treated_as_a_mismatch(self) -> None:
        assert not _detect_unit_mismatches([(1.85, "g/t")], [(1.85, "widgets")])
