"""Regression tests — provenance dict and _source_row on parse results.

Sprint 2: Every parser must populate result.provenance with:
  source_file_sha256, parser_name, parser_version, source_col_map.
Every validated record must carry _source_row == CSV row number (header=1,
data rows start at 2).

Also verifies provenance on spatial_parser (file-based only, geojson) and
pdf_report (file-based, using the minimal PDF fixture from test_pdf_warnings).

Tests that hit bronze.provenance DB writes are OUT OF SCOPE (integration
territory).  All tests here are pure parser-level.

Run with:  pytest tests/test_provenance_and_source_row.py -v
"""

from __future__ import annotations

import hashlib
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pytest

from georag_dagster.parsers.csv_collar import parse_csv_collars
from georag_dagster.parsers.csv_lithology import parse_csv_lithology
from georag_dagster.parsers.csv_sample import parse_csv_samples
from georag_dagster.parsers.csv_survey import parse_csv_surveys
from georag_dagster.parsers.pdf_report import parse_pdf_report


# ---------------------------------------------------------------------------
# Minimal-PDF fixture (duplicated from test_pdf_warnings.py)
# ---------------------------------------------------------------------------

_MINIMAL_PDF_CONTENT = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]
  /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj << /Length 44 >>
stream
BT /F1 12 Tf 100 700 Td (Test PDF Report) Tj ET
endstream
endobj
5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj
xref
0 6
0000000000 65535 f\r
0000000009 00000 n\r
0000000058 00000 n\r
0000000115 00000 n\r
0000000266 00000 n\r
0000000360 00000 n\r
trailer << /Size 6 /Root 1 0 R >>
startxref
441
%%EOF
"""


@pytest.fixture()
def minimal_pdf(tmp_path: Path) -> Path:
    pdf_path = tmp_path / "test_minimal.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF_CONTENT)
    return pdf_path


# ---------------------------------------------------------------------------
# Helper: expected SHA-256 for an inline StringIO
# ---------------------------------------------------------------------------

def _sha256_of_string(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# CSV Collar — provenance + _source_row
# ---------------------------------------------------------------------------

class TestCollarProvenance:
    _CSV = (
        "HoleID,Easting,Northing,Elevation,TotalDepth,HoleType,Status\n"
        "LEB23001,500000,5500000,450,150,Diamond,Active\n"
        "LEB23002,500100,5500100,455,200,Diamond,Active\n"
        "LEB23003,500200,5500200,460,175,Diamond,Active\n"
    )

    def test_sha256_matches_inline_computation(self):
        content = self._CSV
        expected_sha = _sha256_of_string(content)
        result = parse_csv_collars(StringIO(content))
        assert result.provenance["source_file_sha256"] == expected_sha

    def test_parser_name(self):
        result = parse_csv_collars(StringIO(self._CSV))
        assert result.provenance["parser_name"] == "csv_collar"

    def test_parser_version(self):
        result = parse_csv_collars(StringIO(self._CSV))
        assert result.provenance["parser_version"] == "2.0.0"

    def test_source_col_map_is_dict_with_hole_id(self):
        result = parse_csv_collars(StringIO(self._CSV))
        col_map = result.provenance["source_col_map"]
        assert isinstance(col_map, dict)
        assert "hole_id" in col_map
        assert col_map["hole_id"] == "HoleID"

    def test_source_row_numbering(self):
        """Row 1 is the header; data rows start at 2."""
        result = parse_csv_collars(StringIO(self._CSV))
        assert result.valid_rows == 3
        assert result.records[0]["_source_row"] == 2
        assert result.records[1]["_source_row"] == 3
        assert result.records[2]["_source_row"] == 4

    def test_source_file_sentinel_for_stream(self):
        """When source is a stream (no .name), provenance['source_file'] must be non-empty."""
        result = parse_csv_collars(StringIO(self._CSV))
        assert result.provenance["source_file"]  # non-empty string
        assert isinstance(result.provenance["source_file"], str)


# ---------------------------------------------------------------------------
# CSV Samples — provenance sha256 sanity check
# ---------------------------------------------------------------------------

class TestSampleProvenance:
    _CSV = (
        "HoleID,From,To,SampleType,Au_ppm\n"
        "LEB23001,0,1,Core,0.5\n"
        "LEB23001,1,2,Core,0.8\n"
    )

    def test_sha256_is_64_char_hex(self):
        result = parse_csv_samples(StringIO(self._CSV))
        sha = result.provenance["source_file_sha256"]
        assert isinstance(sha, str)
        assert len(sha) == 64
        int(sha, 16)  # must be valid hex — raises ValueError otherwise

    def test_sha256_matches_inline_computation(self):
        content = self._CSV
        result = parse_csv_samples(StringIO(content))
        assert result.provenance["source_file_sha256"] == _sha256_of_string(content)

    def test_parser_name(self):
        result = parse_csv_samples(StringIO(self._CSV))
        assert result.provenance["parser_name"] == "csv_sample"

    def test_parser_version(self):
        result = parse_csv_samples(StringIO(self._CSV))
        assert result.provenance["parser_version"] == "2.0.0"


# ---------------------------------------------------------------------------
# CSV Surveys — provenance sha256 sanity check
# ---------------------------------------------------------------------------

class TestSurveyProvenance:
    _CSV = (
        "HoleID,Depth,Azimuth,Dip\n"
        "LEB23001,0,180,-60\n"
        "LEB23001,50,181,-62\n"
    )

    def test_sha256_is_64_char_hex(self):
        result = parse_csv_surveys(StringIO(self._CSV))
        sha = result.provenance["source_file_sha256"]
        assert isinstance(sha, str) and len(sha) == 64
        int(sha, 16)

    def test_parser_name(self):
        result = parse_csv_surveys(StringIO(self._CSV))
        assert result.provenance["parser_name"] == "csv_survey"

    def test_parser_version(self):
        result = parse_csv_surveys(StringIO(self._CSV))
        assert result.provenance["parser_version"] == "2.0.0"


# ---------------------------------------------------------------------------
# CSV Lithology — provenance sha256 sanity check
# ---------------------------------------------------------------------------

class TestLithologyProvenance:
    _CSV = (
        "HoleID,From,To,Lithology\n"
        "LEB23001,0,5,GR\n"
        "LEB23001,5,10,QTZ\n"
    )

    def test_sha256_is_64_char_hex(self):
        result = parse_csv_lithology(StringIO(self._CSV))
        sha = result.provenance["source_file_sha256"]
        assert isinstance(sha, str) and len(sha) == 64
        int(sha, 16)

    def test_parser_name(self):
        result = parse_csv_lithology(StringIO(self._CSV))
        assert result.provenance["parser_name"] == "csv_lithology"

    def test_parser_version(self):
        result = parse_csv_lithology(StringIO(self._CSV))
        assert result.provenance["parser_version"] == "2.0.0"


# ---------------------------------------------------------------------------
# Spatial parser — provenance sha256 sanity check (file-based GeoJSON)
# ---------------------------------------------------------------------------

class TestSpatialProvenance:
    _GEOJSON = """{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature",
      "geometry": {"type": "Point", "coordinates": [-105.5, 58.5]},
      "properties": {"name": "TestPoint"}
    }
  ]
}"""

    def test_sha256_is_64_char_hex(self, tmp_path: Path):
        gj_path = tmp_path / "test.geojson"
        gj_path.write_text(self._GEOJSON, encoding="utf-8")

        from georag_dagster.parsers.spatial_parser import parse_spatial_file
        result = parse_spatial_file(str(gj_path))
        sha = result.provenance["source_file_sha256"]
        assert isinstance(sha, str) and len(sha) == 64
        int(sha, 16)

    def test_parser_name(self, tmp_path: Path):
        gj_path = tmp_path / "test.geojson"
        gj_path.write_text(self._GEOJSON, encoding="utf-8")

        from georag_dagster.parsers.spatial_parser import parse_spatial_file
        result = parse_spatial_file(str(gj_path))
        assert result.provenance["parser_name"] == "spatial_parser"

    def test_parser_version(self, tmp_path: Path):
        gj_path = tmp_path / "test.geojson"
        gj_path.write_text(self._GEOJSON, encoding="utf-8")

        from georag_dagster.parsers.spatial_parser import parse_spatial_file
        result = parse_spatial_file(str(gj_path))
        assert result.provenance["parser_version"] == "2.3.0"


# ---------------------------------------------------------------------------
# PDF report — provenance sha256 sanity check
# ---------------------------------------------------------------------------

class TestPdfReportProvenance:
    def test_sha256_is_64_char_hex(self, minimal_pdf: Path):
        mock_text = "1. Summary\nThis is a test NI 43-101 technical report.\n"
        with patch(
            "georag_dagster.parsers.pdf_report._parse_with_unstructured",
            return_value=(mock_text, "Test PDF Report", 0),
        ):
            result = parse_pdf_report(str(minimal_pdf))
        sha = result.provenance["source_file_sha256"]
        assert isinstance(sha, str) and len(sha) == 64
        int(sha, 16)

    def test_sha256_matches_file_bytes(self, minimal_pdf: Path):
        expected = hashlib.sha256(_MINIMAL_PDF_CONTENT).hexdigest()
        mock_text = "1. Summary\nThis is a test NI 43-101 technical report.\n"
        with patch(
            "georag_dagster.parsers.pdf_report._parse_with_unstructured",
            return_value=(mock_text, "Test PDF Report", 0),
        ):
            result = parse_pdf_report(str(minimal_pdf))
        assert result.provenance["source_file_sha256"] == expected

    def test_parser_name(self, minimal_pdf: Path):
        mock_text = "1. Summary\nThis is a test NI 43-101 technical report.\n"
        with patch(
            "georag_dagster.parsers.pdf_report._parse_with_unstructured",
            return_value=(mock_text, "Test PDF Report", 0),
        ):
            result = parse_pdf_report(str(minimal_pdf))
        assert result.provenance["parser_name"] == "pdf_report"

    def test_parser_version(self, minimal_pdf: Path):
        mock_text = "1. Summary\nThis is a test NI 43-101 technical report.\n"
        with patch(
            "georag_dagster.parsers.pdf_report._parse_with_unstructured",
            return_value=(mock_text, "Test PDF Report", 0),
        ):
            result = parse_pdf_report(str(minimal_pdf))
        assert result.provenance["parser_version"] == "2.0.0"
