"""GIS audit 2026-09-29 — parser-side fixes.

* GIS-3 / ING-5: a length unit in a header (``From_ft``, ``EOH (ft)``) is
  honoured — feet become metres — instead of being stripped and stored as
  metres. Coordinates in feet are reported, never converted.
* GIS-14 / ING-12: a small file of positive dips is flipped (and flagged
  low-confidence) instead of being rejected or stored positive; an
  explicit ``Inclination_From_Vertical`` is converted; a bare positive
  ``Inclination`` is warned about.
* GIS-1: decimal-degree coordinates are recognised from their headers or
  from degree-like values, and a local mine grid is not mistaken for them.
"""

from __future__ import annotations

import textwrap
from io import StringIO

import pytest

from georag_geoparsers._depth_units import FEET_TO_METRES
from georag_geoparsers._dip_convention import (
    detect_dip_convention,
    normalize_dip,
    resolve_dip_convention,
)
from georag_geoparsers._drill_schema import coordinate_mode_reason, detect_coordinate_mode
from georag_geoparsers._header_match import build_column_map, header_unit, normalize_header
from georag_geoparsers.csv_collar import parse_csv_collars
from georag_geoparsers.csv_lithology import parse_csv_lithology
from georag_geoparsers.csv_sample import parse_csv_samples
from georag_geoparsers.csv_survey import parse_csv_surveys


def _csv(text: str) -> StringIO:
    return StringIO(textwrap.dedent(text).lstrip())


def _codes(result) -> list[str]:
    return [w.get("code") for w in result.warnings]


# ---------------------------------------------------------------------------
# header_unit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("header", "unit"),
    [
        ("From_ft", "ft"),
        ("Depth (ft)", "ft"),
        ("EOH_Feet", "ft"),
        ("DepthFt", "ft"),
        ("TD_FT", "ft"),
        ("Depth_m", "m"),
        ("Depth (metres)", "m"),
        ("Depth", None),
        ("ft", None),          # a column named only "ft" keeps its name
        ("Dip_deg", None),     # angles have no conversion to make
        ("Au_ppm", None),
    ],
)
def test_header_unit(header: str, unit: str | None) -> None:
    assert header_unit(header) == unit


def test_matching_is_unchanged_by_unit_reporting() -> None:
    # The unit is still stripped for matching; only reporting is new.
    assert normalize_header("From_ft") == "from"
    mapped, _ = build_column_map(
        ["HoleID", "Easting", "Northing", "EOH_ft"],
        {"total_depth": ["EOH"], "hole_id": ["HoleID"]},
    )
    assert mapped["total_depth"] == "EOH_ft"


# ---------------------------------------------------------------------------
# GIS-3 — feet headers
# ---------------------------------------------------------------------------


def test_collar_feet_depth_and_elevation_converted() -> None:
    result = parse_csv_collars(_csv("""
        HoleID,Easting,Northing,Elev_ft,EOH_ft,Azimuth,Dip
        WY-1,791126,617244,7000,1257.5,0,-90
    """))
    assert result.valid_rows == 1
    rec = result.records[0]
    assert rec["total_depth"] == pytest.approx(1257.5 * FEET_TO_METRES)
    assert rec["elevation"] == pytest.approx(7000 * FEET_TO_METRES)
    # Coordinates are NEVER converted here.
    assert rec["easting"] == pytest.approx(791126)
    assert "depth_unit_converted" in _codes(result)


def test_collar_feet_coordinates_warned_not_converted() -> None:
    result = parse_csv_collars(_csv("""
        HoleID,Easting_ft,Northing_ft,Elevation,TotalDepth
        WY-1,791126,617244,2100,380
    """))
    assert result.records[0]["easting"] == pytest.approx(791126)
    assert "coordinate_unit_feet" in _codes(result)


def test_metre_headers_untouched() -> None:
    result = parse_csv_collars(_csv("""
        HoleID,Easting,Northing,Elevation_m,TotalDepth_m
        A-1,512000,6300000,450,300
    """))
    assert result.records[0]["total_depth"] == pytest.approx(300)
    assert "depth_unit_converted" not in _codes(result)


def test_lithology_feet_intervals_converted() -> None:
    result = parse_csv_lithology(_csv("""
        HoleID,From_ft,To_ft,Lithology
        A-1,0,10,SST
        A-1,10,25,SHL
    """))
    assert result.valid_rows == 2
    first, second = result.records
    assert first["to_depth"] == pytest.approx(10 * FEET_TO_METRES)
    assert second["from_depth"] == pytest.approx(10 * FEET_TO_METRES)
    assert second["to_depth"] == pytest.approx(25 * FEET_TO_METRES)
    assert "depth_unit_converted" in _codes(result)


def test_sample_feet_intervals_converted() -> None:
    result = parse_csv_samples(_csv("""
        HoleID,SampleID,SampleType,From_ft,To_ft,Au_ppm
        A-1,S1,Core,100,105,1.2
    """))
    assert result.valid_rows == 1
    rec = result.records[0]
    assert rec["from_depth"] == pytest.approx(100 * FEET_TO_METRES)
    assert rec["to_depth"] == pytest.approx(105 * FEET_TO_METRES)


def test_survey_feet_depth_converted() -> None:
    result = parse_csv_surveys(_csv("""
        HoleID,Depth_ft,Azimuth,Dip
        A-1,0,90,-60
        A-1,100,90,-60
    """))
    assert [r["depth"] for r in result.records] == pytest.approx(
        [0.0, 100 * FEET_TO_METRES],
    )


def test_unparseable_feet_cell_still_rejected_with_original_text() -> None:
    result = parse_csv_lithology(_csv("""
        HoleID,From_ft,To_ft,Lithology
        A-1,0,abc,SST
    """))
    assert result.valid_rows == 0
    assert "abc" in str(result.skipped_details[0])


# ---------------------------------------------------------------------------
# GIS-14 — dip conventions
# ---------------------------------------------------------------------------


def test_small_positive_file_is_down_positive() -> None:
    assert detect_dip_convention([55.0, 60.0, 70.0]) == "down_positive"


def test_small_mixed_file_is_ambiguous_not_db_default() -> None:
    assert detect_dip_convention([-60.0, 45.0]) == "ambiguous"


def test_small_negative_file_unchanged() -> None:
    assert detect_dip_convention([-60.0, -45.0]) == "down_negative"


def test_three_hole_collar_file_with_positive_dips_is_flipped_and_flagged() -> None:
    result = parse_csv_collars(_csv("""
        HoleID,Easting,Northing,Elevation,TotalDepth,Azimuth,Dip
        A-1,512000,6300000,450,300,90,55
        A-2,512050,6300000,450,300,90,60
        A-3,512100,6300000,450,300,90,70
    """))
    assert result.valid_rows == 3
    assert [r["dip"] for r in result.records] == [-55.0, -60.0, -70.0]
    convention = next(w for w in result.warnings if w["code"] == "dip_convention_normalized")
    assert convention["context"]["low_confidence"] is True


def test_four_station_positive_survey_lands() -> None:
    result = parse_csv_surveys(_csv("""
        HoleID,Depth,Azimuth,Dip
        A-1,0,90,60
        A-1,50,90,60
        A-1,100,90,61
        A-1,150,90,62
    """))
    assert result.valid_rows == 4
    assert all(r["dip"] < 0 for r in result.records)


def test_inclination_from_vertical_is_converted() -> None:
    result = parse_csv_surveys(_csv("""
        HoleID,Depth,Azimuth,Inclination_From_Vertical
        A-1,0,90,0
        A-1,50,90,30
    """))
    assert [r["dip"] for r in result.records] == [-90.0, -60.0]
    assert "dip_inclination_from_vertical" in _codes(result)


def test_positive_bare_inclination_is_warned_not_converted() -> None:
    result = parse_csv_surveys(_csv("""
        HoleID,Depth,Azimuth,Inclination
        A-1,0,90,60
        A-1,50,90,62
    """))
    # Read as dip below horizontal (flipped), exactly as before...
    assert [r["dip"] for r in result.records] == [-60.0, -62.0]
    # ...but now the ambiguity is said out loud.
    assert "dip_inclination_ambiguous" in _codes(result)


def test_negative_inclination_is_plain_dip() -> None:
    resolution = resolve_dip_convention([-60.0, -55.0], header="Inclination", parser="t")
    assert resolution.convention == "down_negative"
    assert resolution.warnings == []


def test_from_vertical_out_of_range_is_ambiguous() -> None:
    resolution = resolve_dip_convention(
        [-10.0, 30.0], header="Inc_From_Vertical", parser="t",
    )
    assert resolution.convention == "ambiguous"


def test_normalize_from_vertical() -> None:
    assert normalize_dip(0.0, "from_vertical") == -90.0
    assert normalize_dip(90.0, "from_vertical") == 0.0


# ---------------------------------------------------------------------------
# GIS-1 — geographic detection
# ---------------------------------------------------------------------------


def test_lat_lon_headers_are_geographic() -> None:
    assert coordinate_mode_reason(
        [-105.51, -105.52], [57.31, 57.32],
        easting_column="Longitude", northing_column="Latitude",
    ) == ("geographic", "header")


def test_x_y_degree_values_are_geographic() -> None:
    # Real Athabasca-area collars (Key Lake area), decimal degrees.
    assert coordinate_mode_reason(
        [-105.6231, -105.6187, -105.6102], [57.2104, 57.2139, 57.2188],
        easting_column="X", northing_column="Y",
    ) == ("geographic", "values")


def test_local_mine_grid_is_not_geographic() -> None:
    # A grid numbered from zero: inside +/-180/+/-90 but a 100-unit spread.
    assert detect_coordinate_mode(
        [10.0, 55.5, 110.0], [5.0, 40.25, 85.0],
        easting_column="Local_X", northing_column="Local_Y",
    ) == "projected"


def test_whole_number_small_grid_is_not_geographic() -> None:
    assert detect_coordinate_mode([100.0, 101.0], [50.0, 51.0]) == "projected"


def test_utm_values_are_projected() -> None:
    assert detect_coordinate_mode([512000.0], [6300000.0]) == "projected"


def test_collar_parse_reports_coordinate_mode() -> None:
    result = parse_csv_collars(_csv("""
        HoleID,Longitude,Latitude,Elevation,TotalDepth
        A-1,-105.6231,57.2104,480,300
        A-2,-105.6187,57.2139,482,250
    """))
    assert result.valid_rows == 2
    assert result.coordinate_mode == "geographic"
