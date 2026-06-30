"""Regression tests for _survey_interp.py — Sprint 2 minimum-curvature math.

Covers minimum_curvature() and interpolate_sample_xyz().

Run with:  pytest tests/test_survey_interpolation.py -v
"""

from __future__ import annotations

import math

import pytest

from georag_dagster.parsers._survey_interp import (
    SurveyStation,
    interpolate_sample_xyz,
    minimum_curvature,
)


# ---------------------------------------------------------------------------
# minimum_curvature()
# ---------------------------------------------------------------------------

class TestMinimumCurvatureVerticalHole:
    def test_vertical_hole_single_segment(self):
        """Vertical hole: 0 → 100 m at dip=-90, az=0.
        Expected: east ≈ 0, north ≈ 0, elev ≈ -100.
        """
        stations = [
            SurveyStation(depth_m=0,   azimuth_deg=0, dip_deg=-90),
            SurveyStation(depth_m=100, azimuth_deg=0, dip_deg=-90),
        ]
        result = minimum_curvature(0.0, 0.0, 0.0, stations)
        assert len(result) == 2
        _, xyz1 = result[1]
        assert abs(xyz1.east_m)  < 1e-9, f"east_m should be ~0 for vertical hole, got {xyz1.east_m}"
        assert abs(xyz1.north_m) < 1e-9, f"north_m should be ~0 for vertical hole, got {xyz1.north_m}"
        assert abs(xyz1.elev_m - (-100.0)) < 1e-9, f"elev_m should be -100, got {xyz1.elev_m}"

    def test_vertical_from_nonzero_collar(self):
        """Vertical hole from a real collar position.
        XYZ.elev_m is relative to collar, so should still be -100.
        """
        stations = [
            SurveyStation(depth_m=0,   azimuth_deg=0, dip_deg=-90),
            SurveyStation(depth_m=100, azimuth_deg=0, dip_deg=-90),
        ]
        result = minimum_curvature(500000.0, 6000000.0, 450.0, stations)
        _, xyz1 = result[1]
        assert abs(xyz1.elev_m - (-100.0)) < 1e-9


class TestMinimumCurvatureHorizontalHole:
    def test_horizontal_due_east(self):
        """Horizontal hole due east: dip=0, az=90.
        Expected: east ≈ 100, north ≈ 0, elev ≈ 0.
        dip=0 is NOT up-going (up-going is dip > 0), so must NOT raise.
        """
        stations = [
            SurveyStation(depth_m=0,   azimuth_deg=90, dip_deg=0),
            SurveyStation(depth_m=100, azimuth_deg=90, dip_deg=0),
        ]
        result = minimum_curvature(0.0, 0.0, 0.0, stations)
        assert len(result) == 2
        _, xyz1 = result[1]
        assert abs(xyz1.east_m  - 100.0) < 1e-9, f"east_m should be 100, got {xyz1.east_m}"
        assert abs(xyz1.north_m)          < 1e-9, f"north_m should be ~0, got {xyz1.north_m}"
        assert abs(xyz1.elev_m)           < 1e-9, f"elev_m should be ~0, got {xyz1.elev_m}"


class TestMinimumCurvature45Degree:
    def test_45_degree_dip_north(self):
        """45° dip, azimuth north: dip=-45, az=0.
        Expected: east ≈ 0, north ≈ 100*cos(45°) ≈ 70.71, elev ≈ 100*sin(-45°) ≈ -70.71.
        """
        stations = [
            SurveyStation(depth_m=0,   azimuth_deg=0, dip_deg=-45),
            SurveyStation(depth_m=100, azimuth_deg=0, dip_deg=-45),
        ]
        result = minimum_curvature(0.0, 0.0, 0.0, stations)
        _, xyz1 = result[1]
        expected_horiz = 100.0 * math.cos(math.radians(45))  # ~70.71
        expected_elev  = -100.0 * math.sin(math.radians(45)) # ~-70.71
        assert abs(xyz1.east_m)                    < 1e-9,  f"east_m should be ~0, got {xyz1.east_m}"
        assert abs(xyz1.north_m - expected_horiz)  < 1e-9,  f"north_m should be ~{expected_horiz:.2f}, got {xyz1.north_m}"
        assert abs(xyz1.elev_m  - expected_elev)   < 1e-9,  f"elev_m should be ~{expected_elev:.2f}, got {xyz1.elev_m}"


class TestMinimumCurvatureDogleg:
    def test_dogleg_curve_from_vertical_to_45(self):
        """Curved segment: station 0 vertical, station 1 at 45° dip.
        Minimum-curvature correction: east ≈ 0 (no azimuth change),
        north > 0, -100 < elev < 0.
        """
        stations = [
            SurveyStation(depth_m=0,   azimuth_deg=0, dip_deg=-90),
            SurveyStation(depth_m=100, azimuth_deg=0, dip_deg=-45),
        ]
        result = minimum_curvature(0.0, 0.0, 0.0, stations)
        assert len(result) == 2
        _, xyz1 = result[1]
        assert abs(xyz1.east_m) < 1e-9, f"east_m should be ~0 with no az change, got {xyz1.east_m}"
        assert xyz1.north_m > 0,        f"north_m should be positive for dogleg north, got {xyz1.north_m}"
        assert xyz1.elev_m > -100.0,    f"elev_m should be > -100 (curve reduces vertical), got {xyz1.elev_m}"
        assert xyz1.elev_m < 0.0,       f"elev_m should be < 0 (still going down), got {xyz1.elev_m}"


class TestMinimumCurvatureEdgeCases:
    def test_empty_stations_returns_empty(self):
        result = minimum_curvature(0.0, 0.0, 0.0, [])
        assert result == []

    def test_single_station_returns_one_result(self):
        stations = [SurveyStation(depth_m=50, azimuth_deg=0, dip_deg=-90)]
        result = minimum_curvature(0.0, 0.0, 0.0, stations)
        assert len(result) == 1
        depth, xyz = result[0]
        assert depth == 50.0
        # elev_m is relative to collar; for a vertical 50m hole it should be -50
        assert abs(xyz.elev_m - (-50.0)) < 1e-9

    def test_raises_on_up_going_dip(self):
        with pytest.raises(ValueError, match="up-going"):
            minimum_curvature(0.0, 0.0, 0.0, [SurveyStation(depth_m=0, azimuth_deg=0, dip_deg=10)])

    def test_raises_on_impossible_dip_below_minus_90(self):
        with pytest.raises(ValueError):
            minimum_curvature(0.0, 0.0, 0.0, [SurveyStation(depth_m=0, azimuth_deg=0, dip_deg=-95)])

    def test_raises_on_non_monotonic_depth(self):
        stations = [
            SurveyStation(depth_m=0,  azimuth_deg=0, dip_deg=-90),
            SurveyStation(depth_m=50, azimuth_deg=0, dip_deg=-90),
            SurveyStation(depth_m=40, azimuth_deg=0, dip_deg=-90),  # backwards
        ]
        with pytest.raises(ValueError, match="monotonically"):
            minimum_curvature(0.0, 0.0, 0.0, stations)

    def test_dip_zero_does_not_raise(self):
        """dip=0 (flat) is NOT up-going — must be accepted."""
        stations = [SurveyStation(depth_m=0, azimuth_deg=0, dip_deg=0)]
        result = minimum_curvature(0.0, 0.0, 0.0, stations)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# interpolate_sample_xyz()
# ---------------------------------------------------------------------------

class TestInterpolateSampleXyz:
    @pytest.fixture()
    def vertical_station_xyz(self):
        """Two-station vertical hole: (0, XYZ at 0) and (100, XYZ at -100)."""
        stations = [
            SurveyStation(depth_m=0,   azimuth_deg=0, dip_deg=-90),
            SurveyStation(depth_m=100, azimuth_deg=0, dip_deg=-90),
        ]
        return minimum_curvature(0.0, 0.0, 0.0, stations)

    def test_depth_at_exact_station(self, vertical_station_xyz):
        result = interpolate_sample_xyz(100.0, vertical_station_xyz)
        assert result is not None
        assert abs(result.elev_m - (-100.0)) < 1e-9

    def test_depth_between_stations(self, vertical_station_xyz):
        """Midpoint at 50m should interpolate to elev ≈ -50."""
        result = interpolate_sample_xyz(50.0, vertical_station_xyz)
        assert result is not None
        assert abs(result.elev_m - (-50.0)) < 1e-6, f"expected elev -50, got {result.elev_m}"

    def test_depth_above_shallowest_station_returns_none(self, vertical_station_xyz):
        """Requesting a depth shallower than the first station — out of range → None."""
        # station_xyz starts at depth 0; requesting -1 is out of range
        # But depth=0 is valid boundary. Let's request a negative depth.
        # Actually depth 0 is the minimum here; -1 is out of range.
        result = interpolate_sample_xyz(-1.0, vertical_station_xyz)
        assert result is None

    def test_depth_below_deepest_station_returns_none(self, vertical_station_xyz):
        result = interpolate_sample_xyz(200.0, vertical_station_xyz)
        assert result is None

    def test_empty_station_list_returns_none(self):
        result = interpolate_sample_xyz(50.0, [])
        assert result is None

    def test_depth_at_first_station(self, vertical_station_xyz):
        first_depth, first_xyz = vertical_station_xyz[0]
        result = interpolate_sample_xyz(first_depth, vertical_station_xyz)
        assert result is not None
        assert abs(result.elev_m - first_xyz.elev_m) < 1e-9
