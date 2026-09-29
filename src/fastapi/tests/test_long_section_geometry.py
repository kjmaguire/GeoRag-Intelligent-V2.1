"""GIS-7 (audit 2026-09-29): the long section's end-of-hole geometry.

``end_n`` used ``sin(az)`` like ``end_e``, so on a N-S section a hole drilled
due north at -45 was drawn vertical and one drilled due east moved north.
"""
from __future__ import annotations

import math

import pytest

from app.services.visualizations.additional_charts import long_section_figure


def _trace(**collar):
    base = {"hole_id": "H", "easting": 0.0, "northing": 0.0, "elevation": 0.0,
            "total_depth": 100.0}
    base.update(collar)
    fig = long_section_figure(collars=[base], reference_azimuth_deg=0.0)
    return fig["data"][0]


def test_north_hole_on_a_north_section_moves_along_the_section() -> None:
    t = _trace(azimuth=0.0, inclination=-45.0)
    assert t["x"][1] - t["x"][0] == pytest.approx(100 * math.cos(math.radians(45)))
    assert t["y"][1] == pytest.approx(-100 * math.sin(math.radians(45)))


def test_east_hole_on_a_north_section_does_not_move_along_it() -> None:
    t = _trace(azimuth=90.0, inclination=-45.0)
    assert t["x"][1] - t["x"][0] == pytest.approx(0.0, abs=1e-9)


def test_unoriented_hole_is_labelled_not_silently_vertical() -> None:
    t = _trace(azimuth=None, inclination=None)
    assert "orientation not recorded" in t["name"]
    assert t["line"]["dash"] == "dash"
    assert t["x"][1] == pytest.approx(t["x"][0])


def test_oriented_hole_name_is_plain() -> None:
    t = _trace(azimuth=10.0, inclination=-60.0)
    assert t["name"] == "H"
    assert "dash" not in t["line"]
