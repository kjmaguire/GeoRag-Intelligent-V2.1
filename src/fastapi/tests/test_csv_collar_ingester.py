"""Tests for the CSV collar ingester + its wiring into ingest_zip_archive.py.

Background — this restores CSV collar ingestion after the Dagster
retirement (2026-07-28) left `.csv` uploads with no live path (Laravel's
UploadController hard-rejects category=collar/assay with a 422). The
ZIP-archive Hatchet workflow was the one live, wired-up ingestion route
remaining, so `app/services/ingest/csv_collar_ingester.py` gives its
per-file dispatcher (`_ingest_one`) a `.csv` branch.

This file covers the pure-Python parsing/validation helpers only (no DB
required) — always runs. Live-Postgres coverage exercising the real
dispatch path (`ingest_zip_archive._ingest_one` with ext="csv") end to
end, asserting rows land in `silver.collars` with correct values and
workspace scoping, lives in the sibling module
tests/test_csv_collar_ingester_integration.py (skip-safe module-level
guard on POSTGRES_USER, same pattern as
tests/test_ingest_progress_state_machine.py).
"""
from __future__ import annotations

import pathlib

import pytest
from georag_geoparsers._drill_schema import (
    coordinate_bounds,
    coordinate_family_conflict,
    detect_coordinate_mode,
)

from app.services.ingest.csv_collar_ingester import (
    COLUMN_ALIASES,
    REQUIRED_FIELDS,
    _build_column_map,
    _detect_delimiter,
    _detect_encoding,
    _transform_decimal_comma_rows,
    _validate_row,
    canonicalize,
    detect_dip_convention,
    normalize_dip,
)

_FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "csv_collar"
_SAMPLE_CSV = _FIXTURE_DIR / "collars_sample.csv"

#: Coordinate bounds for the UTM fixtures below. The parser picks these
#: per file from the values; the row-level tests pass them explicitly so
#: each case states the ruler it is being measured against.
_UTM_BOUNDS = coordinate_bounds("projected")


# ---------------------------------------------------------------------------
# Pure-Python unit tests — delimiter / encoding / decimal-comma / dip /
# hole_id canonicalization / row validation. No DB required.
# ---------------------------------------------------------------------------
def test_detect_delimiter_comma():
    assert _detect_delimiter("a,b,c\n1,2,3\n") == ","


def test_detect_delimiter_semicolon():
    # EU-export style — the whole reason delimiter auto-detection exists
    # (2026-05-23 CSV audit gap #1, ported here from the Dagster parser).
    assert _detect_delimiter("HoleID;Easting;Northing\nA-1;471000;4657000\n") == ";"


def test_detect_delimiter_tab():
    assert _detect_delimiter("a\tb\tc\n1\t2\t3\n") == "\t"


def test_detect_delimiter_defaults_to_comma_when_ambiguous():
    assert _detect_delimiter("") == ","
    assert _detect_delimiter("just one column\nsome text\n") == ","


def test_detect_encoding_falls_back_to_utf8_for_plain_ascii():
    assert _detect_encoding(b"HoleID,Easting\nA-1,471000\n") in ("utf-8", "ascii", "Ascii")


def test_transform_decimal_comma_rewrites_matching_column():
    rows = [
        {"Easting": "471250,5", "Northing": "4657100,0"},
        {"Easting": "471300,0", "Northing": "4657150,0"},
    ]
    transformed = _transform_decimal_comma_rows(rows, ["Easting", "Northing"])
    assert set(transformed) == {"Easting", "Northing"}
    assert rows[0]["Easting"] == "471250.5"
    assert rows[1]["Northing"] == "4657150.0"


def test_transform_decimal_comma_skips_column_with_period():
    # A period anywhere in the sample disqualifies the column (the rule
    # that distinguishes EU decimal-comma from a stray thousands comma).
    rows = [{"X": "1,234"}, {"X": "5.6"}]
    transformed = _transform_decimal_comma_rows(rows, ["X"])
    assert transformed == []
    assert rows[0]["X"] == "1,234"  # untouched


def test_transform_decimal_comma_skips_text_column():
    rows = [{"HoleID": "CSVT-001"}, {"HoleID": "CSVT-002"}]
    transformed = _transform_decimal_comma_rows(rows, ["HoleID"])
    assert transformed == []


def test_canonicalize_strips_separators_and_uppercases():
    assert canonicalize("LEB-23-001") == "LEB23001"
    assert canonicalize("leb_23_001") == "LEB23001"
    assert canonicalize("  LEB 23/001") == "LEB23001"
    assert canonicalize("") is None
    assert canonicalize(None) is None


def test_dip_convention_detects_down_negative_majority():
    dips = [-60.0, -55.0, -70.0, -45.0, -80.0]
    assert detect_dip_convention(dips) == "down_negative"


def test_dip_convention_detects_down_positive_majority():
    dips = [60.0, 55.0, 70.0, 45.0, 80.0]
    assert detect_dip_convention(dips) == "down_positive"


def test_dip_convention_ambiguous_mixed_signs():
    dips = [60.0, -55.0, 70.0, -45.0, 80.0]
    assert detect_dip_convention(dips) == "ambiguous"


def test_dip_convention_defaults_when_insufficient_samples():
    # Fewer than 5 samples -> defensively defaults to the DB convention.
    assert detect_dip_convention([60.0, 70.0]) == "down_negative"


def test_normalize_dip_flips_sign_for_down_positive():
    assert normalize_dip(60.0, "down_positive") == -60.0
    assert normalize_dip(-60.0, "down_negative") == -60.0
    assert normalize_dip(60.0, "ambiguous") == 60.0


def test_build_column_map_matches_known_aliases():
    csv_columns = ["HoleID", "Easting", "Northing", "Elevation", "ExtraJunkCol"]
    column_map, unmapped = _build_column_map(csv_columns)
    assert column_map["hole_id"] == "HoleID"
    assert column_map["easting"] == "Easting"
    assert column_map["northing"] == "Northing"
    assert column_map["elevation"] == "Elevation"
    assert unmapped == ["ExtraJunkCol"]


def test_build_column_map_reports_all_columns_unmapped_for_unrecognized_headers():
    column_map, unmapped = _build_column_map(["Foo", "Bar"])
    assert column_map == {}
    assert set(unmapped) == {"Foo", "Bar"}


def test_required_fields_and_aliases_match_documented_shape():
    # Pin the documented CSV shape in the module docstring against the
    # actual constants, so a drift here fails loudly.
    #
    # elevation is deliberately NOT required (changed 2026-08-24):
    # silver.collars.elevation is nullable, the writer reads it with
    # .get(), and collar tables that leave elevation to a DEM are ordinary.
    # Requiring it rejected every row of such a file.
    assert frozenset({"hole_id", "easting", "northing"}) == REQUIRED_FIELDS
    assert "HoleID" in COLUMN_ALIASES["hole_id"]
    assert "Easting" in COLUMN_ALIASES["easting"]


def test_elevation_is_optional_and_survives_when_present():
    """A collar with no elevation column is valid; one with it keeps it."""
    without = {"hole_id": "HoleID", "easting": "Easting", "northing": "Northing"}
    record, err = _validate_row(
        2,
        {"HoleID": "A-1", "Easting": "471000", "Northing": "4657000"},
        without,
        "down_negative",
        _UTM_BOUNDS,
    )
    assert err is None
    assert record["hole_id_canonical"] == "A1"
    assert record.get("elevation") is None

    with_elev = {**without, "elevation": "Elevation"}
    record, err = _validate_row(
        2,
        {"HoleID": "A-1", "Easting": "471000", "Northing": "4657000", "Elevation": "1800"},
        with_elev,
        "down_negative",
        _UTM_BOUNDS,
    )
    assert err is None
    assert record["elevation"] == pytest.approx(1800.0)


def test_headers_match_regardless_of_case_separators_and_units():
    """The spelling a geologist actually types reaches the right field.

    Matching was exact and case-sensitive until 2026-08-24, so a file
    headed `Hole ID` was rejected in full with advice to go and rename it.
    """
    column_map, unmapped = _build_column_map(
        ["Hole ID", "East (m)", "North (m)", "Collar RL", "EOH Depth"]
    )
    assert column_map == {
        "hole_id": "Hole ID",
        "easting": "East (m)",
        "northing": "North (m)",
        "elevation": "Collar RL",
        "total_depth": "EOH Depth",
    }
    assert unmapped == []
    assert REQUIRED_FIELDS - set(column_map) == frozenset()


def test_one_column_is_never_claimed_by_two_fields():
    """`{v: k for k, v in column_map}` silently drops a duplicated value."""
    column_map, _ = _build_column_map(["HoleID", "Easting", "Northing", "Type"])
    assert len(set(column_map.values())) == len(column_map)


def test_a_local_mine_grid_is_not_rejected_as_out_of_range():
    """Easting 5,000 is a real local grid, not a bad value.

    The bounds used to be UTM-in-metres (easting 100,000..900,000), which
    refused local grids, State Plane feet and southern-hemisphere systems
    alike — one projection family encoded as a definition of validity.
    """
    column_map = {"hole_id": "HoleID", "easting": "Easting", "northing": "Northing"}
    record, err = _validate_row(
        2,
        {"HoleID": "LG-1", "Easting": "5000.0", "Northing": "2500.0"},
        column_map,
        "down_negative",
        coordinate_bounds(detect_coordinate_mode([5000.0], [2500.0])),
    )
    assert err is None
    assert record["easting"] == pytest.approx(5000.0)


def test_decimal_degrees_are_accepted_under_geographic_bounds():
    column_map = {"hole_id": "HoleID", "easting": "Longitude", "northing": "Latitude"}
    bounds = coordinate_bounds(detect_coordinate_mode([-134.52], [55.91]))
    record, err = _validate_row(
        2,
        {"HoleID": "GEO-1", "Longitude": "-134.52", "Latitude": "55.91"},
        column_map,
        "down_negative",
        bounds,
    )
    assert err is None
    assert record["easting"] == pytest.approx(-134.52)


def test_a_projected_easting_paired_with_a_degree_northing_is_refused():
    """'Easting' beside 'LATITUDE' passes every per-field check.

    (495000.0, 57.123) sits inside projected bounds, so nothing downstream
    objects — and the hole lands 57 metres north of the equator. Neither
    header can be assumed correct, so the pairing is refused.
    """
    assert coordinate_family_conflict("Easting", "LATITUDE") == (
        "projected",
        "geographic",
    )
    # An axis name that says nothing about its units is not a conflict.
    assert coordinate_family_conflict("X", "Latitude") is None
    assert coordinate_family_conflict("Easting", "Northing") is None


def test_validate_row_rejects_missing_required_field():
    column_map = {"hole_id": "HoleID", "easting": "Easting", "northing": "Northing", "elevation": "Elevation"}
    raw = {"HoleID": "A-1", "Easting": "", "Northing": "4657000", "Elevation": "1800"}
    record, err = _validate_row(2, raw, column_map, "down_negative", _UTM_BOUNDS)
    assert record is None
    assert err["code"] == "missing_required"
    assert "easting" in err["reason"]


def test_validate_row_rejects_out_of_range_value():
    column_map = {
        "hole_id": "HoleID", "easting": "Easting", "northing": "Northing",
        "elevation": "Elevation", "dip": "Dip",
    }
    raw = {
        "HoleID": "A-1", "Easting": "471000", "Northing": "4657000",
        "Elevation": "1800", "Dip": "-999",
    }
    record, err = _validate_row(2, raw, column_map, "down_negative", _UTM_BOUNDS)
    assert record is None
    assert err["code"] == "range_check_failed"


def test_validate_row_accepts_valid_row_and_canonicalizes_hole_id():
    column_map = {
        "hole_id": "HoleID", "easting": "Easting", "northing": "Northing",
        "elevation": "Elevation", "total_depth": "TotalDepth",
    }
    raw = {
        "HoleID": "CSVT-001", "Easting": "471250.5", "Northing": "4657100.0",
        "Elevation": "1830.2", "TotalDepth": "152.4",
    }
    record, err = _validate_row(2, raw, column_map, "down_negative", _UTM_BOUNDS)
    assert err is None
    assert record["hole_id"] == "CSVT-001"
    assert record["hole_id_canonical"] == "CSVT001"
    assert record["easting"] == pytest.approx(471250.5)
    assert record["total_depth"] == pytest.approx(152.4)


def test_sample_fixture_exists_and_has_expected_shape():
    assert _SAMPLE_CSV.exists(), f"fixture missing: {_SAMPLE_CSV}"
    text = _SAMPLE_CSV.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert lines[0].startswith("HoleID,Easting,Northing,Elevation")
    assert len(lines) == 5  # header + 4 data rows


# ---------------------------------------------------------------------------
# Dispatcher wiring — pure source-inspection, no DB. Mirrors the existing
# tests/test_ingest_zip_archive_observability.py style.
# ---------------------------------------------------------------------------
def test_ingest_zip_archive_routes_every_tabular_extension_to_ingest_tabular():
    """The .csv branch no longer stops at collars.

    It used to call ingest_csv_collar_file unconditionally, and that
    ingester handles COLLARS ONLY -- it requires hole_id/easting/northing.
    Zip a hole's full dataset (collars.csv, survey.csv, lithology.csv,
    assays.csv) and only collars.csv landed; the other three returned
    skipped_reason="missing_required_columns", which increments
    counts["skipped"] rather than counts["errors"], so the archive was still
    marked completed and the summary reported four files succeeded. The user
    got collars with no surveys, no lithology and no assays, and nothing
    said so.

    ingest_tabular classifies the header and routes to silver.collars,
    silver.surveys, silver.lithology_logs or silver.samples, and for a
    workbook classifies every sheet rather than assuming the first is the
    data.
    """
    path = (
        pathlib.Path(__file__).parents[1] / "app" / "hatchet_workflows" / "ingest_zip_archive.py"
    )
    src = path.read_text(encoding="utf-8")

    assert 'elif ext in ("csv", "tsv", "xlsx", "xls", "xlsm"):' in src, (
        "ingest_zip_archive._ingest_one must route every tabular extension "
        "through one branch, not just .csv"
    )
    assert "ingest_tabular.aio_run_no_wait" in src

    # The counts dict is built from _COUNT_KEYS (2026-09-02) rather than a
    # literal, so the bucket is checked there — the source no longer
    # contains a `"csv": 0`.
    from app.hatchet_workflows.ingest_zip_archive import _COUNT_KEYS

    assert "csv" in _COUNT_KEYS, "counts dict must track the csv branch like las/log/xlsx/pdf"


def test_the_collar_only_ingester_is_no_longer_wired_in():
    """Guards the regression, not the removal.

    Re-wiring ingest_csv_collar_file into the archive dispatcher would
    restore the silent three-quarters data loss described above. If the
    direct-upload path (`category=collar`, currently 422 in Laravel's
    UploadController) is ever restored, wire it there instead and delete
    this test with a note.
    """
    path = (
        pathlib.Path(__file__).parents[1] / "app" / "hatchet_workflows" / "ingest_zip_archive.py"
    )
    src = path.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in src.splitlines() if not line.lstrip().startswith("#")
    )

    assert "ingest_csv_collar_file" not in code


# Live-Postgres integration coverage (rows actually landing in
# silver.collars with correct workspace/project scoping) lives in the
# sibling module tests/test_csv_collar_ingester_integration.py — split
# out because pytest.skip(..., allow_module_level=True) aborts import of
# the whole module it's called in, which would otherwise silently skip
# every unit test above it too.
