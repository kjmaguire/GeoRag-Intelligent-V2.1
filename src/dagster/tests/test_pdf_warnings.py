"""Light regression tests for pdf_report.py — Sprint 1 warnings list threading.

Scope: Do NOT write heavy PDF parsing tests — that is Sprint 3 scope. These
tests only verify that the warnings list is correctly threaded through the parse
result and that partial extraction failures are surfaced as structured warnings
rather than raised exceptions.

Covers:
  - parse_pdf_report on a minimal valid PDF → result.warnings is a list (possibly empty).
  - Mocked pdfplumber page extraction raises → one {"code": "pdf_extraction_partial"}
    entry in warnings, parse returns a ReportParseResult (not an exception).
  - ReportParseResult.warnings attribute exists on the dataclass.

Run with:  pytest tests/test_pdf_warnings.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from georag_dagster.parsers.pdf_report import ReportParseResult, parse_pdf_report

# ---------------------------------------------------------------------------
# Fixture: trivial valid PDF bytes (hand-crafted, no library dependency)
# ---------------------------------------------------------------------------

# Minimal syntactically-valid PDF with one page and a small text stream.
# This is the smallest PDF that passes the %PDF- magic and opens in most
# readers. We write it to a temp file for tests that need a real file on disk.
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
    """Write a minimal valid PDF to a temp file and return the path."""
    pdf_path = tmp_path / "test_minimal.pdf"
    pdf_path.write_bytes(_MINIMAL_PDF_CONTENT)
    return pdf_path


# ---------------------------------------------------------------------------
# Tests: warnings field is always a list on ReportParseResult
# ---------------------------------------------------------------------------

class TestReportParseResultWarningsField:
    def test_warnings_field_is_list_on_dataclass_default(self):
        """ReportParseResult must have a warnings field that defaults to a list."""
        result = ReportParseResult(
            title="Test",
            authors=[],
            company=None,
            filing_date=None,
            commodity=None,
            project_name=None,
            region=None,
            sections=[],
            parse_quality_pct=0.0,
        )
        assert isinstance(result.warnings, list), (
            "ReportParseResult.warnings must default to a list, not None or missing"
        )

    def test_warnings_field_accepts_structured_dicts(self):
        """Confirm warnings list holds dict entries (not strings or arbitrary types)."""
        w = {"code": "pdf_extraction_partial", "page": 1, "message": "test error"}
        result = ReportParseResult(
            title="Test",
            authors=[],
            company=None,
            filing_date=None,
            commodity=None,
            project_name=None,
            region=None,
            sections=[],
            parse_quality_pct=0.0,
            warnings=[w],
        )
        assert len(result.warnings) == 1
        assert result.warnings[0]["code"] == "pdf_extraction_partial"


# ---------------------------------------------------------------------------
# Tests: parse_pdf_report on a real minimal PDF
# ---------------------------------------------------------------------------

class TestParsePdfReportWarningsThreaded:
    def test_result_warnings_is_list_on_minimal_pdf(self, minimal_pdf: Path):
        """parse_pdf_report must return a result with warnings as a list,
        even when no warnings are generated."""
        # Both unstructured and pdfplumber may be unavailable in the test env;
        # patch both to return minimal text so we exercise the happy path.
        mock_text = "1. Summary\nThis is a test NI 43-101 technical report.\n"

        with (
            patch(
                "georag_dagster.parsers.pdf_report._parse_with_unstructured",
                return_value=(mock_text, "Test PDF Report", 0),
            ),
        ):
            result = parse_pdf_report(str(minimal_pdf))

        assert isinstance(result.warnings, list), (
            "parse_pdf_report must return ReportParseResult with warnings as a list"
        )

    def test_file_not_found_raises(self, tmp_path: Path):
        with pytest.raises(FileNotFoundError):
            parse_pdf_report(str(tmp_path / "nonexistent.pdf"))

    def test_non_pdf_bytes_raises_value_error(self, tmp_path: Path):
        bad_path = tmp_path / "not_a_pdf.pdf"
        bad_path.write_bytes(b"This is not a PDF file at all.")
        with pytest.raises(ValueError, match="not a valid PDF"):
            parse_pdf_report(str(bad_path))


# ---------------------------------------------------------------------------
# Tests: pdfplumber extraction partial failure → pdf_extraction_partial warning
# ---------------------------------------------------------------------------

class TestPdfPlumberPartialExtractionWarning:
    def test_pdfplumber_page_exception_produces_extraction_partial_warning(
        self, minimal_pdf: Path
    ):
        """When pdfplumber raises on a page, the parser must:
        1. NOT re-raise the exception.
        2. Append a warning with code='pdf_extraction_partial'.
        3. Return a ReportParseResult (not raise).
        """
        # Make unstructured unavailable so we fall through to pdfplumber.
        def _raise_unstructured(*args, **kwargs):
            raise ImportError("unstructured not available (test mock)")

        # Mock the pdfplumber page to raise on extract_text
        mock_page = MagicMock()
        mock_page.extract_text.side_effect = RuntimeError("simulated page extraction error")

        mock_pdf_ctx = MagicMock()
        mock_pdf_ctx.__enter__ = MagicMock(return_value=mock_pdf_ctx)
        mock_pdf_ctx.__exit__ = MagicMock(return_value=False)
        mock_pdf_ctx.pages = [mock_page]

        with (
            patch(
                "georag_dagster.parsers.pdf_report._parse_with_unstructured",
                side_effect=_raise_unstructured,
            ),
            patch("pdfplumber.open", return_value=mock_pdf_ctx),
        ):
            result = parse_pdf_report(str(minimal_pdf))

        assert isinstance(result, ReportParseResult), (
            "parse_pdf_report must return a ReportParseResult even when a page fails"
        )
        extraction_partial_warnings = [
            w for w in result.warnings
            if isinstance(w, dict) and w.get("code") == "pdf_extraction_partial"
        ]
        assert len(extraction_partial_warnings) >= 1, (
            "Expected at least one 'pdf_extraction_partial' warning when a "
            "pdfplumber page extraction raises; got warnings: "
            + str(result.warnings)
        )

    def test_pdfplumber_partial_failure_result_is_not_exception(
        self, minimal_pdf: Path
    ):
        """Duplicate assertion framed differently: result is returned, not raised."""
        def _raise_unstructured(*args, **kwargs):
            raise ImportError("unstructured not available (test mock)")

        mock_page = MagicMock()
        mock_page.extract_text.side_effect = RuntimeError("page error")

        mock_pdf_ctx = MagicMock()
        mock_pdf_ctx.__enter__ = MagicMock(return_value=mock_pdf_ctx)
        mock_pdf_ctx.__exit__ = MagicMock(return_value=False)
        mock_pdf_ctx.pages = [mock_page]

        with (
            patch(
                "georag_dagster.parsers.pdf_report._parse_with_unstructured",
                side_effect=_raise_unstructured,
            ),
            patch("pdfplumber.open", return_value=mock_pdf_ctx),
        ):
            try:
                parse_pdf_report(str(minimal_pdf))
                returned_result = True
            except Exception:
                returned_result = False

        assert returned_result, (
            "parse_pdf_report should not raise when pdfplumber fails on a page"
        )


# ---------------------------------------------------------------------------
# Tests: existing fixture PDF (if available)
# ---------------------------------------------------------------------------

_FIXTURE_PDF = (
    Path(__file__).parent / "fixtures" / "reports" / "PLS-2024-Technical-Report.pdf"
)


@pytest.mark.skipif(
    not _FIXTURE_PDF.is_file(),
    reason="Fixture PDF not found — skipping fixture-based warnings test",
)
class TestFixturePdfWarnings:
    def test_fixture_pdf_returns_warnings_list(self):
        """The existing fixture PDF must return a result.warnings that is a list."""
        with (
            patch(
                "georag_dagster.parsers.pdf_report._parse_with_unstructured",
                return_value=("1. Summary\nTest document text.\n", "Test Report", 0),
            ),
        ):
            result = parse_pdf_report(str(_FIXTURE_PDF))

        assert isinstance(result.warnings, list), (
            "parse_pdf_report must always return warnings as a list"
        )
