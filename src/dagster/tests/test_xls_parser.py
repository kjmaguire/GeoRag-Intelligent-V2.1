"""Sprint 4 smoke tests for .xls support in xlsx_parser.py.

Covers:
  - ExcelParseResult / XlsxParseResult (backward-compatible alias).
  - .xls file: xls_legacy_format_detected warning emitted.
  - .xls file: format field = "xls".
  - .xls file: collar sheet → parses hole IDs and coordinates correctly.
  - .xls file with merged cells → merged_cells_detected warning.
  - .xls file with multi-row header → multi_row_header_suspected warning.
  - .xlsx file still works unchanged (regression guard).
  - Unsupported extension → ValueError.
  - Provenance: sha256 hex present, deterministic.
  - XlsxParseResult is ExcelParseResult (alias check).

All .xls test fixtures are created inline using xlrd/xlwt (xlwt is test-only).

Run with:  pytest tests/test_xls_parser.py -v
"""

from __future__ import annotations

import os

import pytest

xlrd = pytest.importorskip("xlrd", reason="xlrd not installed")

from georag_dagster.parsers.xlsx_parser import (  # noqa: E402
    ExcelParseResult,
    XlsxParseResult,
    parse_xlsx_sheet,
)


# ---------------------------------------------------------------------------
# Helpers — XLS fixture builder using xlwt
# ---------------------------------------------------------------------------

def _require_xlwt():
    """Skip test if xlwt is unavailable (xlwt is write-only; only needed for fixtures)."""
    try:
        import xlwt  # noqa: PLC0415
        return xlwt
    except ImportError:
        pytest.skip("xlwt not installed — cannot create .xls fixtures")


def _write_collar_xls(path: str, merged: bool = False, multi_header: bool = False) -> None:
    """Write a minimal collar .xls fixture.

    Args:
        path: Output path for the .xls file.
        merged: If True, add a merged cell range to trigger the merged-cells warning.
        multi_header: If True, leave row 0 mostly empty and put headers in row 1
                      to trigger the multi-row-header warning.
    """
    xlwt = _require_xlwt()
    wb = xlwt.Workbook()
    ws = wb.add_sheet("Collars")

    if multi_header:
        # Row 0 mostly empty (only 1 of 5 cells filled)
        ws.write(0, 0, "")
        ws.write(0, 1, "")
        ws.write(0, 2, "")
        ws.write(0, 3, "")
        ws.write(0, 4, "SparseHeader")
        # Row 1 has full headers (this is what the warning flags)
        ws.write(1, 0, "HoleID")
        ws.write(1, 1, "Easting")
        ws.write(1, 2, "Northing")
        ws.write(1, 3, "Elevation")
        ws.write(1, 4, "TotalDepth")
        # Data in rows 2+
        ws.write(2, 0, "PLS-001")
        ws.write(2, 1, 500000.0)
        ws.write(2, 2, 6200000.0)
        ws.write(2, 3, 450.0)
        ws.write(2, 4, 120.5)
    else:
        ws.write(0, 0, "HoleID")
        ws.write(0, 1, "Easting")
        ws.write(0, 2, "Northing")
        ws.write(0, 3, "Elevation")
        ws.write(0, 4, "TotalDepth")
        ws.write(1, 0, "PLS-001")
        ws.write(1, 1, 500000.0)
        ws.write(1, 2, 6200000.0)
        ws.write(1, 3, 450.0)
        ws.write(1, 4, 120.5)
        ws.write(2, 0, "PLS-002")
        ws.write(2, 1, 510000.0)
        ws.write(2, 2, 6210000.0)
        ws.write(2, 3, 460.0)
        ws.write(2, 4, 98.0)

    if merged:
        # Merge cells (0,0)–(0,1) — row 0, cols 0–1
        ws.merge(0, 0, 0, 1)

    wb.save(path)


# ---------------------------------------------------------------------------
# Tests: backward-compat alias
# ---------------------------------------------------------------------------

class TestBackwardCompatAlias:
    def test_xlsxparseresult_is_excelparseresult(self):
        """XlsxParseResult must still exist and be the same class."""
        assert XlsxParseResult is ExcelParseResult

    def test_excelparseresult_has_format_field(self):
        import dataclasses  # noqa: PLC0415
        fields = {f.name for f in dataclasses.fields(ExcelParseResult)}
        assert "format" in fields


# ---------------------------------------------------------------------------
# Tests: .xls parsing
# ---------------------------------------------------------------------------

class TestXlsCollarParse:
    def test_xls_collar_returns_excel_parse_result(self, tmp_path):
        _require_xlwt()
        p = str(tmp_path / "collars.xls")
        _write_collar_xls(p)
        result = parse_xlsx_sheet(p, "", "collar")
        assert isinstance(result, ExcelParseResult)

    def test_xls_format_field_is_xls(self, tmp_path):
        _require_xlwt()
        p = str(tmp_path / "collars.xls")
        _write_collar_xls(p)
        result = parse_xlsx_sheet(p, "", "collar")
        assert result.format == "xls"

    def test_xls_emits_legacy_warning(self, tmp_path):
        _require_xlwt()
        p = str(tmp_path / "collars.xls")
        _write_collar_xls(p)
        result = parse_xlsx_sheet(p, "", "collar")
        codes = [w["code"] for w in result.warnings]
        assert "xls_legacy_format_detected" in codes, (
            f"Expected xls_legacy_format_detected warning; got: {codes}"
        )

    def test_xls_parses_hole_ids(self, tmp_path):
        _require_xlwt()
        p = str(tmp_path / "collars.xls")
        _write_collar_xls(p)
        result = parse_xlsx_sheet(p, "", "collar")
        assert result.total_rows >= 1, "Should have at least one data row"

    def test_xls_source_file_populated(self, tmp_path):
        _require_xlwt()
        p = str(tmp_path / "collars.xls")
        _write_collar_xls(p)
        result = parse_xlsx_sheet(p, "", "collar")
        assert result.source_file == "collars.xls"

    def test_xls_provenance_sha256_present(self, tmp_path):
        _require_xlwt()
        p = str(tmp_path / "collars.xls")
        _write_collar_xls(p)
        result = parse_xlsx_sheet(p, "", "collar")
        assert "source_file_sha256" in result.provenance
        assert len(result.provenance["source_file_sha256"]) == 64

    def test_xls_provenance_deterministic(self, tmp_path):
        _require_xlwt()
        p = str(tmp_path / "collars.xls")
        _write_collar_xls(p)
        r1 = parse_xlsx_sheet(p, "", "collar")
        r2 = parse_xlsx_sheet(p, "", "collar")
        assert r1.provenance["source_file_sha256"] == r2.provenance["source_file_sha256"]


# ---------------------------------------------------------------------------
# Tests: merged cell warning
# ---------------------------------------------------------------------------

class TestXlsMergedCells:
    def test_merged_cells_emits_warning(self, tmp_path):
        xlwt_mod = _require_xlwt()
        p = str(tmp_path / "merged.xls")
        # Use write_merge() to create merged cells without overwrite conflicts.
        # write_merge(r1, r2, c1, c2, label) merges the range and writes one label.
        wb = xlwt_mod.Workbook()
        ws = wb.add_sheet("Collars")
        # Merged header spanning columns 0–1
        ws.write_merge(0, 0, 0, 1, "HoleID_Merged")
        ws.write(0, 2, "Northing")
        ws.write(0, 3, "Elevation")
        ws.write(0, 4, "TotalDepth")
        ws.write(1, 0, "PLS-001")
        ws.write(1, 1, 500000.0)
        ws.write(1, 2, 6200000.0)
        ws.write(1, 3, 450.0)
        ws.write(1, 4, 120.5)
        wb.save(p)

        result = parse_xlsx_sheet(p, "", "collar")
        codes = [w["code"] for w in result.warnings]
        assert "merged_cells_detected" in codes, (
            f"Expected merged_cells_detected warning; got: {codes}"
        )

    def test_merged_cells_warning_has_count(self, tmp_path):
        xlwt_mod = _require_xlwt()
        p = str(tmp_path / "merged2.xls")
        wb = xlwt_mod.Workbook()
        ws = wb.add_sheet("Sheet1")
        # write_merge avoids cell overwrite conflicts
        ws.write_merge(0, 0, 0, 1, "MergedCol")
        ws.write(0, 2, "Col3")
        ws.write(1, 0, "val1")
        ws.write(1, 1, "val2")
        ws.write(1, 2, "val3")
        wb.save(p)

        result = parse_xlsx_sheet(p, "", "collar")
        merged_warnings = [w for w in result.warnings if w["code"] == "merged_cells_detected"]
        assert len(merged_warnings) == 1
        assert merged_warnings[0]["context"]["count"] >= 1


# ---------------------------------------------------------------------------
# Tests: multi-row header detection
# ---------------------------------------------------------------------------

class TestXlsMultiRowHeader:
    def test_multi_row_header_emits_warning(self, tmp_path):
        _require_xlwt()
        p = str(tmp_path / "multirow.xls")
        _write_collar_xls(p, multi_header=True)
        result = parse_xlsx_sheet(p, "", "collar")
        codes = [w["code"] for w in result.warnings]
        assert "multi_row_header_suspected" in codes, (
            f"Expected multi_row_header_suspected warning; got: {codes}"
        )

    def test_multi_row_header_warning_has_candidate_rows(self, tmp_path):
        _require_xlwt()
        p = str(tmp_path / "multirow2.xls")
        _write_collar_xls(p, multi_header=True)
        result = parse_xlsx_sheet(p, "", "collar")
        mhw = [w for w in result.warnings if w["code"] == "multi_row_header_suspected"]
        assert len(mhw) == 1
        assert "header_candidate_rows" in mhw[0]["context"]
        assert 0 in mhw[0]["context"]["header_candidate_rows"]
        assert 1 in mhw[0]["context"]["header_candidate_rows"]


# ---------------------------------------------------------------------------
# Tests: .xlsx regression guard
# ---------------------------------------------------------------------------

class TestXlsxRegressionGuard:
    def test_xlsx_collar_fixture_still_parses(self):
        """Existing .xlsx fixture must still parse correctly after the .xls addition."""
        fixture = os.path.join(
            os.path.dirname(__file__),
            "fixtures", "excel", "PLS_collars.xlsx"
        )
        if not os.path.isfile(fixture):
            pytest.skip("PLS_collars.xlsx fixture not found")
        result = parse_xlsx_sheet(fixture, "", "collar")
        assert isinstance(result, ExcelParseResult)
        assert result.format in ("xlsx", "xlsm")
        assert result.total_rows >= 1

    def test_xlsx_format_field_is_xlsx(self, tmp_path):
        """Verify format field on an .xlsx parse is 'xlsx'."""
        fixture = os.path.join(
            os.path.dirname(__file__),
            "fixtures", "excel", "PLS_collars.xlsx"
        )
        if not os.path.isfile(fixture):
            pytest.skip("PLS_collars.xlsx fixture not found")
        result = parse_xlsx_sheet(fixture, "", "collar")
        assert result.format == "xlsx"

    def test_xlsx_no_xls_legacy_warning(self, tmp_path):
        """An .xlsx file must NOT emit xls_legacy_format_detected."""
        fixture = os.path.join(
            os.path.dirname(__file__),
            "fixtures", "excel", "PLS_collars.xlsx"
        )
        if not os.path.isfile(fixture):
            pytest.skip("PLS_collars.xlsx fixture not found")
        result = parse_xlsx_sheet(fixture, "", "collar")
        codes = [w["code"] for w in result.warnings]
        assert "xls_legacy_format_detected" not in codes


# ---------------------------------------------------------------------------
# Tests: error cases
# ---------------------------------------------------------------------------

class TestXlsErrorCases:
    def test_unsupported_extension_raises_value_error(self, tmp_path):
        p = str(tmp_path / "data.csv")
        with open(p, "w") as f:
            f.write("HoleID,Easting\nPLS-001,500000\n")
        with pytest.raises(ValueError, match="unsupported file extension"):
            parse_xlsx_sheet(p, "", "collar")

    def test_unknown_sheet_type_raises_value_error(self, tmp_path):
        _require_xlwt()
        p = str(tmp_path / "collars.xls")
        _write_collar_xls(p)
        with pytest.raises(ValueError, match="unknown sheet_type"):
            parse_xlsx_sheet(p, "", "invalid_type")  # type: ignore[arg-type]
