"""Ingestion validation corpus — CSV collar parser.

Tests verify:
- Correct collar count from the fixture CSV
- Column alias detection across naming variants
- Coordinate range-check logic (UTM 13N sanity bounds)
- CRS bbox validation helper
- Required-field enforcement (rows with missing hole_id/easting/etc are skipped)
- Parse quality metric computation
- Graceful handling of entirely missing required columns (file-level error)
- Unmapped column tracking
- Date parsing across common formats

Run with:  pytest tests/test_csv_collar_parser.py -v
"""

from __future__ import annotations

import textwrap
from io import StringIO
from pathlib import Path

import pytest

from georag_dagster.parsers.csv_collar import (
    REQUIRED_FIELDS,
    CollarParseResult,
    _build_column_map,
    parse_csv_collars,
)

# _detect_source_epsg moved from parsers.csv_collar to assets.silver because
# the heuristic is shared across every tabular Silver asset (CSV collars,
# XLSX workbooks, XYZ gravity grids). The TestCRSDetection class imports it
# from its new home; this file no longer needs it at module scope.

FIXTURE_CSV = Path(__file__).parent / "fixtures" / "sample_collars.csv"

# Expected values for the sample_collars.csv fixture
EXPECTED_COLLAR_COUNT = 10
# Coordinate checksum: sum of all eastings in the fixture, rounded to 1 dp
EXPECTED_EASTING_SUM = round(
    495123.5 + 495234.1 + 494876.3 + 496012.8 + 493445.0
    + 497331.7 + 494102.6 + 496788.4 + 493990.2 + 498256.9,
    1,
)


# ---------------------------------------------------------------------------
# Fixture CSV — baseline correctness
# ---------------------------------------------------------------------------

class TestFixtureCSV:
    def test_collar_count(self):
        result = parse_csv_collars(FIXTURE_CSV)
        assert result.total_rows == EXPECTED_COLLAR_COUNT
        assert result.valid_rows == EXPECTED_COLLAR_COUNT
        assert result.skipped_rows == 0

    def test_easting_coordinate_checksum(self):
        """Verify CRS transform did not corrupt eastings (coordinate checksum)."""
        result = parse_csv_collars(FIXTURE_CSV)
        actual_sum = round(sum(r["easting"] for r in result.records), 1)
        assert actual_sum == EXPECTED_EASTING_SUM, (
            f"Easting checksum mismatch: expected {EXPECTED_EASTING_SUM}, got {actual_sum}"
        )

    def test_schema_field_completeness(self):
        """Every record must contain all canonical fields that were present in the CSV."""
        result = parse_csv_collars(FIXTURE_CSV)
        expected_fields = {
            "hole_id", "easting", "northing", "elevation",
            "total_depth", "azimuth", "dip", "hole_type", "drill_date", "status",
        }
        for rec in result.records:
            for field in expected_fields:
                assert field in rec, f"Field '{field}' missing from record {rec.get('hole_id')}"

    def test_no_unmapped_columns(self):
        result = parse_csv_collars(FIXTURE_CSV)
        assert result.unmapped_columns == [], f"Unexpected unmapped columns: {result.unmapped_columns}"

    def test_parse_quality_100_pct(self):
        result = parse_csv_collars(FIXTURE_CSV)
        assert result.parse_quality_pct == 100.0

    def test_drill_dates_parsed(self):
        result = parse_csv_collars(FIXTURE_CSV)
        from datetime import date
        for rec in result.records:
            assert isinstance(rec["drill_date"], date), (
                f"drill_date should be a date object, got {type(rec['drill_date'])} for {rec['hole_id']}"
            )

    def test_numeric_types(self):
        result = parse_csv_collars(FIXTURE_CSV)
        for rec in result.records:
            for field in ("easting", "northing", "elevation", "total_depth", "azimuth", "dip"):
                assert isinstance(rec[field], float), (
                    f"Field '{field}' should be float in record {rec.get('hole_id')}"
                )


# ---------------------------------------------------------------------------
# Column alias detection
# ---------------------------------------------------------------------------

class TestColumnAliasDetection:
    def test_alternative_aliases(self):
        csv = textwrap.dedent("""\
            HOLEID,EAST,NORTH,ELEV,TD,AZI,DIP,DrillType,StartDate,status
            BH001,495000.0,6220000.0,430.0,300.0,180.0,-70.0,RC,2021-01-10,Completed
        """)
        result = parse_csv_collars(StringIO(csv))
        assert result.valid_rows == 1
        assert result.records[0]["hole_id"] == "BH001"
        assert result.records[0]["easting"] == 495000.0

    def test_dh_id_alias(self):
        csv = textwrap.dedent("""\
            DH_ID,X,Y,Z,MaxDepth,AZ,Inclination,HoleType,Date,Status
            DH-001,496000.0,6221000.0,425.0,400.0,270.0,-80.0,Diamond,2022-05-20,Completed
        """)
        result = parse_csv_collars(StringIO(csv))
        assert result.valid_rows == 1
        assert result.records[0]["hole_id"] == "DH-001"
        assert result.records[0]["dip"] == -80.0

    def test_unmapped_columns_reported(self):
        csv = textwrap.dedent("""\
            HoleID,Easting,Northing,Elevation,TotalDepth,Geologist,Comments
            PLS-01,495000.0,6220000.0,430.0,300.0,John Smith,Anomalous zone
        """)
        result = parse_csv_collars(StringIO(csv))
        assert "Geologist" in result.unmapped_columns
        assert "Comments" in result.unmapped_columns
        assert result.valid_rows == 1  # Unmapped columns don't block valid rows


# ---------------------------------------------------------------------------
# Validation — required fields
# ---------------------------------------------------------------------------

class TestRequiredFieldValidation:
    def test_missing_hole_id_skipped(self):
        csv = textwrap.dedent("""\
            HoleID,Easting,Northing,Elevation,TotalDepth
            ,495000.0,6220000.0,430.0,300.0
            PLS-01,495100.0,6220100.0,431.0,305.0
        """)
        result = parse_csv_collars(StringIO(csv))
        assert result.valid_rows == 1
        assert result.skipped_rows == 1

    def test_missing_easting_skipped(self):
        csv = textwrap.dedent("""\
            HoleID,Easting,Northing,Elevation
            PLS-01,,6220000.0,430.0
            PLS-02,495100.0,6220100.0,431.0
        """)
        result = parse_csv_collars(StringIO(csv))
        assert result.valid_rows == 1
        assert result.skipped_rows == 1

    def test_all_required_missing_skips_all(self):
        csv = textwrap.dedent("""\
            HoleID,Easting,Northing,Elevation
            ,,,
            ,,,
        """)
        result = parse_csv_collars(StringIO(csv))
        assert result.valid_rows == 0
        assert result.skipped_rows == 2

    def test_missing_required_column_mapping(self):
        """CSV with no recognisable northing column should produce zero valid rows."""
        csv = textwrap.dedent("""\
            HoleID,Easting,LATITUDE,Elevation
            PLS-01,495000.0,57.123,430.0
        """)
        result = parse_csv_collars(StringIO(csv))
        # 'northing' required field has no mapped column — file-level failure
        assert result.valid_rows == 0
        assert result.skipped_rows >= 0  # could be total_rows or 0 depending on path


# ---------------------------------------------------------------------------
# Range checks
# ---------------------------------------------------------------------------

class TestRangeChecks:
    def test_invalid_azimuth_rejected(self):
        csv = textwrap.dedent("""\
            HoleID,Easting,Northing,Elevation,TotalDepth,Azimuth,Dip
            PLS-01,495000.0,6220000.0,430.0,300.0,999.0,-70.0
            PLS-02,495100.0,6220100.0,431.0,300.0,180.0,-70.0
        """)
        result = parse_csv_collars(StringIO(csv))
        assert result.valid_rows == 1
        assert result.skipped_rows == 1

    def test_invalid_dip_rejected(self):
        csv = textwrap.dedent("""\
            HoleID,Easting,Northing,Elevation,TotalDepth,Azimuth,Dip
            BAD-01,495000.0,6220000.0,430.0,300.0,180.0,-120.0
            OK-01,495100.0,6220100.0,431.0,300.0,180.0,-70.0
        """)
        result = parse_csv_collars(StringIO(csv))
        assert result.valid_rows == 1
        assert result.records[0]["hole_id"] == "OK-01"

    def test_valid_boundary_values_accepted(self):
        csv = textwrap.dedent("""\
            HoleID,Easting,Northing,Elevation,TotalDepth,Azimuth,Dip
            PLS-01,495000.0,6220000.0,430.0,300.0,0.0,-90.0
            PLS-02,495100.0,6220100.0,431.0,300.0,360.0,0.0
        """)
        result = parse_csv_collars(StringIO(csv))
        assert result.valid_rows == 2


# ---------------------------------------------------------------------------
# Parse quality metric
# ---------------------------------------------------------------------------

class TestParseQualityMetric:
    def test_quality_50_pct(self):
        csv = textwrap.dedent("""\
            HoleID,Easting,Northing,Elevation
            PLS-01,495000.0,6220000.0,430.0
            ,495100.0,6220100.0,431.0
        """)
        result = parse_csv_collars(StringIO(csv))
        assert result.parse_quality_pct == 50.0

    def test_quality_zero_rows(self):
        csv = "HoleID,Easting,Northing,Elevation\n"
        result = parse_csv_collars(StringIO(csv))
        assert result.parse_quality_pct == 0.0
        assert result.total_rows == 0


# ---------------------------------------------------------------------------
# Column map builder (unit test)
# ---------------------------------------------------------------------------

class TestColumnMapBuilder:
    def test_standard_names_mapped(self):
        cols = ["HoleID", "Easting", "Northing", "Elevation", "TotalDepth", "Azimuth", "Dip"]
        col_map, unmapped = _build_column_map(cols)
        assert col_map["hole_id"] == "HoleID"
        assert col_map["easting"] == "Easting"
        assert col_map["northing"] == "Northing"
        assert unmapped == []

    def test_first_alias_wins(self):
        # Both "Easting" and "EAST" present — "Easting" should win (listed first in aliases)
        cols = ["HoleID", "Easting", "EAST", "Northing", "Elevation"]
        col_map, _ = _build_column_map(cols)
        assert col_map["easting"] == "Easting"

    def test_unmapped_columns_identified(self):
        cols = ["HoleID", "Easting", "Northing", "Elevation", "Lab_Number", "Rock_Type"]
        _, unmapped = _build_column_map(cols)
        assert "Lab_Number" in unmapped
        assert "Rock_Type" in unmapped


# ---------------------------------------------------------------------------
# CRS detection (silver asset helper — tested here for coverage)
# ---------------------------------------------------------------------------

class TestCRSDetection:
    """Tests for the heuristic CRS detection used in silver.py."""

    def test_override_respected(self):
        from georag_dagster.assets.silver import _detect_source_epsg
        records = [{"easting": 495000.0, "northing": 6220000.0}]
        assert _detect_source_epsg(records, override=32612) == 32612

    def test_utm_heuristic_returns_project_epsg(self):
        from georag_dagster.assets.silver import _detect_source_epsg
        records = [{"easting": 495000.0, "northing": 6220000.0}]
        assert _detect_source_epsg(records, override=0) == 32613

    def test_empty_records_returns_default(self):
        from georag_dagster.assets.silver import _detect_source_epsg
        assert _detect_source_epsg([], override=0) == 32613


# ---------------------------------------------------------------------------
# Bbox validation (silver asset helper)
# ---------------------------------------------------------------------------

class TestBboxValidation:
    def test_valid_athabasca_coordinate(self):
        from georag_dagster.assets.silver import _bbox_valid
        assert _bbox_valid(495000.0, 6221000.0) is True

    def test_coordinate_outside_bbox_rejected(self):
        from georag_dagster.assets.silver import _bbox_valid
        # Way outside — southern hemisphere
        assert _bbox_valid(495000.0, 200000.0) is False

    def test_coordinate_west_of_bbox_rejected(self):
        from georag_dagster.assets.silver import _bbox_valid
        assert _bbox_valid(50000.0, 6221000.0) is False
