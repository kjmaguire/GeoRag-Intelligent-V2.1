"""Tests for resource-table extraction helpers added in Sprint 3.

Covers:
  - _score_header_row: pure-unit token counting.
  - _classify_header: best-header-row selection over first 3 rows.
  - _table_confidence: bounds and monotonicity.
  - _extract_resource_tables: integration via mocked pdfplumber.open.
  - parse_pdf_report: resource_table_extraction_failed warning on pdfplumber error.

No real PDF needed — all tests use mocked pdfplumber or are pure-unit.

Run with:  pytest tests/test_pdf_resource_tables.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from georag_dagster.parsers.pdf_report import (
    _classify_header,
    _extract_resource_tables,
    _score_header_row,
    _table_confidence,
    parse_pdf_report,
)

# ---------------------------------------------------------------------------
# Minimal PDF bytes (same pattern as test_pdf_warnings.py)
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
    p = tmp_path / "test.pdf"
    p.write_bytes(_MINIMAL_PDF_CONTENT)
    return p


# ---------------------------------------------------------------------------
# Pure-unit: _score_header_row
# ---------------------------------------------------------------------------

class TestScoreHeaderRow:
    def test_all_matching_tokens(self):
        assert _score_header_row(["tonnes", "grade", "category"]) == 3

    def test_no_matching_tokens(self):
        assert _score_header_row(["random", "stuff", "here"]) == 0

    def test_partial_match(self):
        # "au" and "oz" are tokens; "depth" is not
        score = _score_header_row(["au", "depth", "oz"])
        assert score == 2

    def test_case_insensitive(self):
        # token matching is lowercase comparison
        assert _score_header_row(["TONNES", "Grade (g/t)", "Category"]) == 3

    def test_none_cells_are_skipped(self):
        # None cells must not raise
        score = _score_header_row([None, "tonnes", None])
        assert score == 1

    def test_empty_row(self):
        assert _score_header_row([]) == 0

    @pytest.mark.parametrize("cell,expected", [
        ("g/t Au", 1),
        ("Cu %", 1),
        ("contained oz", 1),
        ("description", 0),
    ])
    def test_parametrized_single_cells(self, cell, expected):
        assert _score_header_row([cell]) == expected


# ---------------------------------------------------------------------------
# Pure-unit: _classify_header
# ---------------------------------------------------------------------------

class TestClassifyHeader:
    def test_header_at_row_0(self):
        table = [
            ["Category", "Tonnes", "Grade (g/t)"],
            ["Measured", "1000", "2.5"],
            ["Indicated", "2000", "1.8"],
        ]
        idx, header = _classify_header(table)
        assert idx == 0
        assert "Category" in header or "Tonnes" in header

    def test_header_at_row_1_when_row_0_is_title(self):
        # Row 0 is a merged title-like row; row 1 has column tokens.
        table = [
            ["Mineral Resource Summary", "", ""],
            ["Category", "Tonnes", "Au (g/t)"],
            ["Measured", "500", "1.2"],
        ]
        idx, header = _classify_header(table)
        assert idx == 1

    def test_returns_cleaned_header_list(self):
        table = [
            ["tonnes", "grade", "category"],
            ["100", "1.5", "Measured"],
        ]
        idx, header = _classify_header(table)
        assert isinstance(header, list)
        assert all(isinstance(h, str) for h in header)

    def test_empty_cells_replaced_with_col_fallback(self):
        table = [
            ["tonnes", "", "grade"],
            ["100", "500", "1.5"],
        ]
        _, header = _classify_header(table)
        # Middle cell was empty — should become "col_1"
        assert header[1] == "col_1"

    def test_single_row_table(self):
        table = [["Category", "Tonnes", "Grade"]]
        idx, header = _classify_header(table)
        assert idx == 0
        assert len(header) == 3

    def test_empty_table(self):
        # Should not raise; returns (0, [])
        idx, header = _classify_header([])
        assert header == []


# ---------------------------------------------------------------------------
# Pure-unit: _table_confidence
# ---------------------------------------------------------------------------

class TestTableConfidence:
    def test_confidence_in_bounds(self):
        header = ["Category", "Tonnes", "Grade (g/t)"]
        data = [["Measured", "1000", "2.5"], ["Indicated", "2000", "1.8"]]
        conf = _table_confidence(header, data)
        assert 0.0 <= conf <= 1.0

    def test_no_header_returns_zero(self):
        assert _table_confidence([], [["a", "b"]]) == 0.0

    def test_no_data_rows_penalised(self):
        header = ["tonnes", "grade", "category"]
        conf_no_data = _table_confidence(header, [])
        header_full = ["tonnes", "grade", "category"]
        data_full = [["M", "1", "2"]] * 10
        conf_full = _table_confidence(header_full, data_full)
        assert conf_full > conf_no_data

    def test_more_rows_monotonically_increases(self):
        header = ["tonnes", "grade", "category"]
        confs = []
        for n in (1, 5, 10):
            data = [["M", "1", "2"]] * n
            confs.append(_table_confidence(header, data))
        assert confs[0] <= confs[1] <= confs[2]

    def test_better_header_tokens_increases_confidence(self):
        data = [["M", "1", "2"]] * 5
        weak_header = ["col_0", "col_1", "col_2"]
        strong_header = ["tonnes", "grade (g/t)", "category"]
        conf_weak = _table_confidence(weak_header, data)
        conf_strong = _table_confidence(strong_header, data)
        assert conf_strong > conf_weak

    def test_capped_at_one(self):
        header = ["tonnes", "grade", "category", "au", "ag"]
        data = [["M", "1", "2", "3", "4"]] * 20
        assert _table_confidence(header, data) <= 1.0


# ---------------------------------------------------------------------------
# Integration: _extract_resource_tables via mocked pdfplumber.open
# ---------------------------------------------------------------------------

def _make_mock_pdf(pages: list[dict]) -> MagicMock:
    """Build a mock pdfplumber PDF context manager.

    Each entry in *pages* is a dict with:
        text: str  (returned by page.extract_text())
        tables_lines: list[list[list]]  (returned by extract_tables with lines strategy)
        tables_text: list[list[list]]   (returned by extract_tables with text strategy)
    """
    mock_pages = []
    for pg in pages:
        page = MagicMock()
        page.extract_text.return_value = pg.get("text", "")

        lines_tables = pg.get("tables_lines", [])
        text_tables = pg.get("tables_text", [])

        def make_extract_tables(lt, tt):
            def extract_tables(table_settings=None):
                if table_settings and table_settings.get("vertical_strategy") == "text":
                    return tt
                return lt
            return extract_tables

        page.extract_tables.side_effect = make_extract_tables(lines_tables, text_tables)
        mock_pages.append(page)

    mock_pdf = MagicMock()
    mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
    mock_pdf.__exit__ = MagicMock(return_value=False)
    mock_pdf.pages = mock_pages
    return mock_pdf


class TestExtractResourceTables:
    def test_trigger_phrase_marks_page_as_candidate(self):
        """Page text containing 'mineral resource' → ≥1 result with correct trigger."""
        resource_table = [
            ["Category", "Tonnes", "Grade (g/t)"],
            ["Measured", "1000", "2.5"],
            ["Indicated", "2000", "1.8"],
        ]
        mock_pdf = _make_mock_pdf([{
            "text": "Mineral Resource Estimate summary for the project.",
            "tables_lines": [resource_table],
        }])

        with patch("pdfplumber.open", return_value=mock_pdf):
            results = _extract_resource_tables("fake.pdf")

        assert len(results) >= 1
        assert results[0]["trigger_phrase"] == "mineral resource"

    def test_no_trigger_phrase_returns_empty_list(self):
        """Page text with no trigger phrases → empty result."""
        mock_pdf = _make_mock_pdf([{
            "text": "General introduction to the project location and history.",
            "tables_lines": [[["col_a", "col_b"], ["val1", "val2"]]],
        }])

        with patch("pdfplumber.open", return_value=mock_pdf):
            results = _extract_resource_tables("fake.pdf")

        assert results == []

    def test_resource_table_header_recognised_with_confidence(self):
        """Standard resource table → header recognised, confidence > 0.3."""
        resource_table = [
            ["Category", "Tonnes", "Grade (g/t)"],
            ["Measured", "1000", "2.5"],
            ["Indicated", "2000", "1.8"],
        ]
        mock_pdf = _make_mock_pdf([{
            "text": "mineral resource estimate table below.",
            "tables_lines": [resource_table],
        }])

        with patch("pdfplumber.open", return_value=mock_pdf):
            results = _extract_resource_tables("fake.pdf")

        assert len(results) >= 1
        assert results[0]["confidence"] > 0.3

    def test_lines_strategy_failure_falls_back_to_text_strategy(self):
        """When lines strategy returns empty, text strategy is used."""
        resource_table = [
            ["Category", "Tonnes", "Grade (g/t)"],
            ["Measured", "1000", "2.5"],
        ]
        mock_pdf = _make_mock_pdf([{
            "text": "mineral resource estimate.",
            "tables_lines": [],        # lines strategy finds nothing
            "tables_text": [resource_table],  # text strategy finds the table
        }])

        with patch("pdfplumber.open", return_value=mock_pdf):
            results = _extract_resource_tables("fake.pdf")

        assert len(results) >= 1
        assert results[0]["extraction_method"] == "text"

    def test_result_dict_contains_required_keys(self):
        """Each result entry must contain the expected keys."""
        resource_table = [
            ["Category", "Tonnes"],
            ["Measured", "1000"],
        ]
        mock_pdf = _make_mock_pdf([{
            "text": "mineral resource estimate.",
            "tables_lines": [resource_table],
        }])

        with patch("pdfplumber.open", return_value=mock_pdf):
            results = _extract_resource_tables("fake.pdf")

        required_keys = {
            "page", "table_index_on_page", "trigger_phrase",
            "header", "rows", "extraction_method", "confidence",
        }
        for r in results:
            assert required_keys.issubset(r.keys())


class TestParseReportResourceTableFailureWarning:
    def test_pdfplumber_exception_produces_resource_table_extraction_failed_warning(
        self, minimal_pdf: Path
    ):
        """When _extract_resource_tables raises, parse_pdf_report must:
        - NOT re-raise.
        - Append {'code': 'resource_table_extraction_failed'} to warnings.
        """
        mock_text = "1. Summary\nThis is a test NI 43-101 report.\n"

        with (
            patch(
                "georag_dagster.parsers.pdf_report._parse_with_unstructured",
                return_value=(mock_text, "Test Report", 0),
            ),
            patch(
                "georag_dagster.parsers.pdf_report._extract_resource_tables",
                side_effect=RuntimeError("pdfplumber internal error"),
            ),
        ):
            result = parse_pdf_report(str(minimal_pdf))

        codes = [w.get("code") for w in result.warnings if isinstance(w, dict)]
        assert "resource_table_extraction_failed" in codes

    def test_parse_returns_empty_resource_tables_on_extraction_failure(
        self, minimal_pdf: Path
    ):
        """resource_tables is empty when extraction raises."""
        mock_text = "1. Summary\nTest NI 43-101 report.\n"

        with (
            patch(
                "georag_dagster.parsers.pdf_report._parse_with_unstructured",
                return_value=(mock_text, "Test Report", 0),
            ),
            patch(
                "georag_dagster.parsers.pdf_report._extract_resource_tables",
                side_effect=RuntimeError("boom"),
            ),
        ):
            result = parse_pdf_report(str(minimal_pdf))

        assert result.resource_tables == []
