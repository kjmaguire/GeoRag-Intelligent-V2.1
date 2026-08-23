"""``_crs_quality`` and ``_crs_epsg`` -- how confident the map is allowed to look.

WHY THIS FILE EXISTS
    ``test_ingest_spatial_archive.py`` covers how a spatial delivery is
    unpacked. Nothing covered what happens to its COORDINATE REFERENCE
    SYSTEM, which is the half that decides whether the features land in
    the right place.

    ``georef_method`` is CHECK-constrained to declared / detected /
    assumed / manual / survey, and the distinction drives the map's
    positional-uncertainty ring. "assumed" is the only honest way to say
    the location may be wrong. A CRS assumed to be WGS84 that is really
    UTM puts a drill collar a continent away -- and if the classifier
    reported that as "declared", the map draws a confident dot with no
    ring, on top of the wrong country.

    So there are two failure modes here, and they are not symmetric.
    Producing a value outside the constraint fails the insert for the
    whole file, which is loud. Producing a value that is INSIDE the
    constraint but too confident is silent, and looks like data.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.hatchet_workflows.ingest_spatial import _crs_epsg, _crs_quality

#: The CHECK constraint on silver.spatial_features.georef_method.
LEGAL_METHODS = {"declared", "detected", "assumed", "manual", "survey"}


class TestCrsEpsg:
    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("EPSG:26913", 26913),
            ("epsg:4326", 4326),
            ("  EPSG:32613  ", 32613),
            (None, None),
            ("", None),
            ("WGS 84", None),
            ("+proj=utm +zone=13 +datum=NAD83", None),
            ("EPSG:", None),
            ("EPSG:not-a-number", None),
        ],
    )
    def test_parses_a_code_or_returns_none(self, source, expected) -> None:
        """None is the correct answer for anything that is not EPSG:<int>.

        Guessing a code from a proj4 string or a datum name would be the
        one mistake with no visible symptom -- the features land somewhere
        plausible and wrong.
        """
        assert _crs_epsg(source) == expected


class TestCrsQuality:
    def test_a_qfield_capture_is_a_survey_fix(self) -> None:
        """QField data comes off a GNSS receiver in someone's hand.

        That is a genuine survey fix, not a guess, and it gets the fixed
        0.9 the pipeline has always assigned it.
        """
        assert _crs_quality(SimpleNamespace(is_qfield=True)) == (0.9, "survey")

    def test_qfield_is_checked_before_the_confidence_score(self) -> None:
        """Order is load-bearing.

        A QField GeoPackage can carry a low parser CRS confidence -- the
        file format says little about the CRS -- while the fix itself is
        good. Reading the score first would demote a real survey to
        "assumed" and draw an uncertainty ring around a GNSS point.
        """
        assert _crs_quality(
            SimpleNamespace(is_qfield=True, crs_confidence=0.1),
        ) == (0.9, "survey")

    def test_missing_confidence_is_assumed_not_declared(self) -> None:
        """The honest default, and the one that raises the ring."""
        assert _crs_quality(SimpleNamespace(crs_confidence=None)) == (
            None, "assumed")

    def test_an_object_with_no_crs_attribute_at_all_is_also_assumed(self) -> None:
        """getattr defaults must not fall through to a confident value."""
        assert _crs_quality(SimpleNamespace()) == (None, "assumed")

    @pytest.mark.parametrize(
        ("confidence", "method"),
        [
            (1.0, "declared"),
            (0.85, "declared"),   # boundary, inclusive
            (0.84, "detected"),
            (0.5, "detected"),    # boundary, inclusive
            (0.49, "assumed"),
            (0.0, "assumed"),
        ],
    )
    def test_the_thresholds_are_inclusive_at_both_boundaries(
        self, confidence: float, method: str,
    ) -> None:
        assert _crs_quality(
            SimpleNamespace(crs_confidence=confidence))[1] == method

    def test_the_confidence_is_passed_through_unchanged(self) -> None:
        """The number reaches crs_confidence on the row; rounding or
        clamping it here would make the ring disagree with the parser."""
        assert _crs_quality(SimpleNamespace(crs_confidence=0.732))[0] == 0.732

    def test_a_string_confidence_is_coerced_not_crashed(self) -> None:
        """Parser results have come back with stringified numbers before,
        and a TypeError here fails the whole file rather than one CRS."""
        assert _crs_quality(SimpleNamespace(crs_confidence="0.9")) == (
            0.9, "declared")

    @pytest.mark.parametrize(
        "result",
        [
            SimpleNamespace(is_qfield=True),
            SimpleNamespace(crs_confidence=None),
            SimpleNamespace(crs_confidence=1.0),
            SimpleNamespace(crs_confidence=0.85),
            SimpleNamespace(crs_confidence=0.6),
            SimpleNamespace(crs_confidence=0.1),
            SimpleNamespace(),
        ],
    )
    def test_every_reachable_method_satisfies_the_check_constraint(
        self, result,
    ) -> None:
        """An illegal value fails the insert for every feature in the file,
        not just the one row -- the same shape as the feature_type bug of
        2026-08-20."""
        assert _crs_quality(result)[1] in LEGAL_METHODS

    def test_manual_is_reachable_only_from_outside_this_function(self) -> None:
        """Documented, not asserted-by-accident.

        "manual" is in the constraint because a geologist can correct a
        CRS in the UI. This classifier never produces it, and should not:
        it only sees what the parser found. If a future change makes it
        return "manual", that is a human claim being invented by a
        heuristic.
        """
        produced = {
            _crs_quality(SimpleNamespace(is_qfield=True))[1],
            _crs_quality(SimpleNamespace(crs_confidence=None))[1],
            _crs_quality(SimpleNamespace(crs_confidence=0.95))[1],
            _crs_quality(SimpleNamespace(crs_confidence=0.6))[1],
            _crs_quality(SimpleNamespace(crs_confidence=0.2))[1],
        }
        assert "manual" not in produced
