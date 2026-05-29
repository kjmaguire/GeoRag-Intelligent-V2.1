"""Unit tests for the §B/S/G build-out Dagster assets:

  bronze_geophysics            — payload validation
  silver_geophysics            — survey-type enum + UPSERT contract
  gold_cross_section_panels    — projection math
  gold_structure_measurements_visual — stereonet projection math
  silver_structure_derive      — α/β → true_dip rotation math

Pure-function tests that don't spin up a Dagster run or PostgreSQL — they
exercise the math + validation + module-shape contracts that would silently
break if column names or enum values drifted.
"""
from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# bronze_geophysics — payload validation
# ---------------------------------------------------------------------------


def _write_temp_json(payload: dict) -> str:
    p = Path(tempfile.mkstemp(suffix=".json")[1])
    p.write_text(json.dumps(payload), encoding="utf-8")
    return str(p)


def test_bronze_geophysics_imports_clean() -> None:
    from georag_dagster.assets.bronze_geophysics import bronze_geophysics
    assert bronze_geophysics is not None


def test_bronze_geophysics_validates_required_fields() -> None:
    from georag_dagster.assets.bronze_geophysics import _validate_payload

    good = _write_temp_json({"survey_name": "S1", "survey_type": "magnetic"})
    assert _validate_payload(good)["survey_name"] == "S1"

    with pytest.raises(ValueError, match="survey_name"):
        _validate_payload(_write_temp_json({"survey_type": "magnetic"}))

    with pytest.raises(ValueError, match="survey_type"):
        _validate_payload(_write_temp_json({"survey_name": "S1"}))

    with pytest.raises(ValueError, match="non-dict"):
        _validate_payload(_write_temp_json(["not", "a", "dict"]))


# ---------------------------------------------------------------------------
# silver_geophysics — enum + upsert key
# ---------------------------------------------------------------------------


def test_silver_geophysics_valid_survey_types_locked() -> None:
    """Schema CHECK constraint matches the asset's enum. Changing one without
    the other is the kind of silent breakage we want a test to catch."""
    from georag_dagster.assets.silver_geophysics import VALID_SURVEY_TYPES
    assert VALID_SURVEY_TYPES == frozenset({
        "seismic", "magnetic", "gravity", "radiometric", "IP", "EM", "other",
    })


def test_silver_geophysics_upsert_uses_workspace_survey_name_key() -> None:
    """The UPSERT key must be (workspace_id, survey_name) per R1 (the unique
    constraint added 2026_05_22_020000). If this drifts, payload retries
    duplicate instead of updating."""
    from georag_dagster.assets.silver_geophysics import INSERT_SURVEY_SQL
    assert "ON CONFLICT (workspace_id, survey_name)" in INSERT_SURVEY_SQL


# ---------------------------------------------------------------------------
# gold_cross_section_panels — projection math
# ---------------------------------------------------------------------------


def test_cross_section_bearing_north_is_zero() -> None:
    from georag_dagster.assets.gold_cross_section_panels import _bearing_deg
    # A → B due north: azimuth should be ~0.
    bearing = _bearing_deg(-106.5, 42.0, -106.5, 42.1)
    assert abs(bearing) < 1.0 or abs(bearing - 360.0) < 1.0


def test_cross_section_bearing_east_is_ninety() -> None:
    from georag_dagster.assets.gold_cross_section_panels import _bearing_deg
    bearing = _bearing_deg(-106.5, 42.0, -106.4, 42.0)
    # Eastward bearing ~90° (small variance OK due to spherical geometry).
    assert 88.0 < bearing < 92.0


def test_cross_section_haversine_zero_distance() -> None:
    from georag_dagster.assets.gold_cross_section_panels import _haversine_m
    assert _haversine_m(-106.5, 42.0, -106.5, 42.0) == 0.0


def test_cross_section_haversine_one_degree() -> None:
    """One degree of latitude is ~111 km on a perfect sphere."""
    from georag_dagster.assets.gold_cross_section_panels import _haversine_m
    d = _haversine_m(-106.5, 42.0, -106.5, 43.0)
    assert 110_000 < d < 112_000


def test_cross_section_projects_collinear_point_to_axis_zero_perp() -> None:
    from georag_dagster.assets.gold_cross_section_panels import _project_lonlat_onto_axis
    # Point exactly on the A→B great-circle line should have ~0 perp offset.
    axis, perp = _project_lonlat_onto_axis(
        -106.45, 42.05,   # midpoint of A-B
        -106.5, 42.0,
        -106.4, 42.1,
    )
    assert perp < 50.0  # within 50 m of the line


# ---------------------------------------------------------------------------
# gold_structure_measurements_visual — stereonet projection
# ---------------------------------------------------------------------------


def test_stereonet_planar_horizontal_plane_at_center() -> None:
    """A horizontal plane (dip=0) has a vertical pole — plots at the
    centre of the stereonet."""
    from georag_dagster.assets.gold_structure_measurements_visual import _project_planar_pole
    x, y = _project_planar_pole(0.0, 90.0, "equal_area")
    assert abs(x) < 1e-9 and abs(y) < 1e-9


def test_stereonet_planar_vertical_plane_on_equator() -> None:
    """A vertical plane (dip=90°) has a horizontal pole → pole_plunge=0,
    and the equal-area projection puts it at ρ = √2·sin(45°) = 1.0 on the
    equator (not √2 — that's the edge of the lower hemisphere itself)."""
    from georag_dagster.assets.gold_structure_measurements_visual import _project_planar_pole
    x, y = _project_planar_pole(90.0, 90.0, "equal_area")
    radius = math.hypot(x, y)
    assert 0.99 < radius < 1.01, f"expected ~1.0 (equator), got {radius}"


def test_stereonet_classify_unknown_to_other() -> None:
    from georag_dagster.assets.gold_structure_measurements_visual import _classify_structure_type
    assert _classify_structure_type("fault") == "fault"
    assert _classify_structure_type("FAULT") == "fault"
    assert _classify_structure_type("not-a-real-type") == "other"
    assert _classify_structure_type(None) == "other"


# ---------------------------------------------------------------------------
# silver_structure_derive — α/β → true_dip
# ---------------------------------------------------------------------------


def test_derive_true_orientation_vertical_hole_alpha_zero_gives_horizontal_plane() -> None:
    """In a vertical hole (dip=90°), α=0 means the measured plane is
    perpendicular to the core axis → horizontal plane → true_dip = 0°."""
    from georag_dagster.assets.silver_structure_derive import derive_true_orientation
    td, _ = derive_true_orientation(
        alpha_deg=0.0, beta_deg=0.0,
        hole_az_deg=0.0, hole_dip_deg=90.0,
    )
    assert td < 0.5  # near zero


def test_derive_true_orientation_vertical_hole_alpha_ninety_gives_vertical_plane() -> None:
    """In a vertical hole, α=90 means the measured plane is parallel to
    the core axis → vertical plane → true_dip = 90°."""
    from georag_dagster.assets.silver_structure_derive import derive_true_orientation
    td, _ = derive_true_orientation(
        alpha_deg=90.0, beta_deg=0.0,
        hole_az_deg=0.0, hole_dip_deg=90.0,
    )
    assert td > 89.5


def test_derive_true_orientation_returns_bounded_values() -> None:
    """Random-ish input should always produce dip ∈ [0,90] + dip_dir ∈ [0,360)."""
    from georag_dagster.assets.silver_structure_derive import derive_true_orientation
    for alpha in (10.0, 30.0, 45.0, 60.0, 80.0):
        for beta in (0.0, 90.0, 180.0, 270.0):
            for az in (0.0, 45.0, 200.0):
                for hd in (45.0, 60.0, 90.0):
                    td, tdd = derive_true_orientation(alpha, beta, az, hd)
                    assert 0.0 <= td <= 90.0, f"true_dip={td} out of range"
                    assert 0.0 <= tdd < 360.0, f"true_dip_dir={tdd} out of range"
