"""Unit tests for the ADR-0007 PR-4 straight-line fallback path.

Covers the new code path added to ``silver_drill_traces`` for collars
that have no rows in ``silver.surveys`` but DO carry a usable collar
orientation (azimuth + dip + total_depth). The helper functions are
exercised directly — no DB, no Dagster, no MinIO.

Run with::

    pytest src/dagster/tests/test_silver_drill_traces_straightline.py -v
"""

from __future__ import annotations

import math

import pytest
from pyproj import Transformer

from georag_dagster.assets.silver_drill_traces import (
    _compute_collar_orientation_hash,
    _build_straight_line_wkt,
)


# ---------------------------------------------------------------------------
# _compute_collar_orientation_hash — deterministic + sensitive to changes
# ---------------------------------------------------------------------------


class TestOrientationHash:
    def test_hash_is_deterministic(self) -> None:
        a = _compute_collar_orientation_hash(45.0, -60.0, 300.0)
        b = _compute_collar_orientation_hash(45.0, -60.0, 300.0)
        assert a == b
        assert len(a) == 64  # SHA-256 hex

    def test_hash_changes_when_azimuth_changes(self) -> None:
        a = _compute_collar_orientation_hash(45.0, -60.0, 300.0)
        b = _compute_collar_orientation_hash(46.0, -60.0, 300.0)
        assert a != b

    def test_hash_changes_when_dip_changes(self) -> None:
        a = _compute_collar_orientation_hash(45.0, -60.0, 300.0)
        b = _compute_collar_orientation_hash(45.0, -65.0, 300.0)
        assert a != b

    def test_hash_changes_when_total_depth_changes(self) -> None:
        a = _compute_collar_orientation_hash(45.0, -60.0, 300.0)
        b = _compute_collar_orientation_hash(45.0, -60.0, 350.0)
        assert a != b

    def test_hash_handles_none(self) -> None:
        # All-None still hashes — useful so the asset can store a "no
        # orientation" marker without crashing.
        h = _compute_collar_orientation_hash(None, None, None)
        assert isinstance(h, str)
        assert len(h) == 64


# ---------------------------------------------------------------------------
# _build_straight_line_wkt — geometry sanity for the fallback path
# ---------------------------------------------------------------------------


class TestBuildStraightLineWkt:
    """Verify the straight-line builder produces sensible LINESTRINGZ WKT.

    Project CRS for the tests is EPSG:32613 (UTM 13N) to match the
    asset's default; the transformer maps to EPSG:4326 (lon/lat/elev).
    """

    @pytest.fixture
    def transformer(self) -> Transformer:
        return Transformer.from_crs(32613, 4326, always_xy=True)

    def test_vertical_hole_returns_two_points(self, transformer) -> None:
        # az=0, dip=-90 = pure vertical. Toe must sit directly below the
        # collar (same lon/lat after reproject) at elev - TD.
        wkt = _build_straight_line_wkt(
            collar_easting=500000.0,
            collar_northing=4500000.0,
            collar_elev=2000.0,
            total_depth=300.0,
            azimuth_deg=0.0,
            dip_deg=-90.0,
            transformer=transformer,
        )
        assert wkt.startswith("LINESTRING Z (")
        assert wkt.endswith(")")
        # Two coordinate triples.
        inside = wkt[len("LINESTRING Z ("): -1]
        pts = inside.split(",")
        assert len(pts) == 2

        # Toe elevation must be collar_elev - total_depth (within float
        # precision).
        toe_z = float(pts[1].strip().split()[2])
        assert toe_z == pytest.approx(2000.0 - 300.0, abs=1e-6)

    def test_inclined_hole_toe_offset_matches_trig(self, transformer) -> None:
        # az=90 (east), dip=-45 — toe should sit east of the collar AND
        # below it by TD * sin(45).
        td = 200.0
        az = 90.0
        dip = -45.0
        easting = 500000.0
        northing = 4500000.0
        elev = 1500.0
        wkt = _build_straight_line_wkt(
            collar_easting=easting,
            collar_northing=northing,
            collar_elev=elev,
            total_depth=td,
            azimuth_deg=az,
            dip_deg=dip,
            transformer=transformer,
        )

        # Compute the expected project-CRS toe and then reproject to lon/lat
        # the same way the helper does so we compare apples to apples.
        expected_e = easting + td * math.cos(math.radians(dip)) * math.sin(math.radians(az))
        expected_n = northing + td * math.cos(math.radians(dip)) * math.cos(math.radians(az))
        expected_z = elev + td * math.sin(math.radians(dip))
        lon_exp, lat_exp, _ = transformer.transform(expected_e, expected_n, expected_z)

        inside = wkt[len("LINESTRING Z ("): -1]
        toe_bits = inside.split(",")[1].strip().split()
        toe_lon = float(toe_bits[0])
        toe_lat = float(toe_bits[1])
        toe_z = float(toe_bits[2])

        assert toe_lon == pytest.approx(lon_exp, abs=1e-9)
        assert toe_lat == pytest.approx(lat_exp, abs=1e-9)
        assert toe_z == pytest.approx(expected_z, abs=1e-6)

    def test_horizontal_hole_toe_above_collar_unchanged(self, transformer) -> None:
        """dip=0 → toe at same elevation as collar."""
        wkt = _build_straight_line_wkt(
            collar_easting=500000.0,
            collar_northing=4500000.0,
            collar_elev=1000.0,
            total_depth=100.0,
            azimuth_deg=180.0,
            dip_deg=0.0,
            transformer=transformer,
        )
        inside = wkt[len("LINESTRING Z ("): -1]
        toe_z = float(inside.split(",")[1].strip().split()[2])
        assert toe_z == pytest.approx(1000.0, abs=1e-6)
