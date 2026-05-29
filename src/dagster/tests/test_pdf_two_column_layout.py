"""Tests for two-column layout detection and column-aware text extraction (Sprint 3).

Covers:
  - _detect_page_columns: single-cluster, two-cluster, sparse-second-cluster cases.
  - _extract_text_column_aware: single-column passthrough, two-column crop-and-join.
  - Integration through _parse_with_pdfplumber: two_column_layout_detected warning.

All tests use mocked pdfplumber page objects — no real PDFs needed.

Run with:  pytest tests/test_pdf_two_column_layout.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest

from georag_dagster.parsers.pdf_report import (
    _detect_page_columns,
    _extract_text_column_aware,
    _parse_with_pdfplumber,
)

# ---------------------------------------------------------------------------
# Minimal PDF bytes (for integration tests that need a file on disk)
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
# Helpers for building mock pages
# ---------------------------------------------------------------------------

def _make_page(words: list[dict], width: float = 612.0, height: float = 792.0) -> MagicMock:
    """Build a mock pdfplumber page with the given word list and dimensions."""
    page = MagicMock()
    page.width = width
    page.height = height
    page.extract_words.return_value = words
    return page


def _words_at(x0_values: list[float]) -> list[dict]:
    """Make a minimal word-dict list with the given x0 positions."""
    return [{"x0": x} for x in x0_values]


# ---------------------------------------------------------------------------
# _detect_page_columns — unit tests
# ---------------------------------------------------------------------------

class TestDetectPageColumns:
    def test_single_cluster_returns_1(self):
        """All words bunched in one x0 region → single column."""
        # 30 words all between x0=10 and x0=50 on a 612-wide page
        words = _words_at([float(10 + i % 5) for i in range(30)])
        page = _make_page(words)
        assert _detect_page_columns(page) == 1

    def test_two_clusters_far_apart_returns_2(self):
        """Two equal clusters >30% of page width apart → two columns."""
        # Left cluster: x0 in [10, 50], Right cluster: x0 in [310, 350]
        # Page width 612 → gap is ~260px > 0.3 * 612 = 183.6
        left = _words_at([10.0, 20.0, 30.0, 40.0, 50.0] * 6)   # 30 words
        right = _words_at([310.0, 320.0, 330.0, 340.0, 350.0] * 6)  # 30 words
        page = _make_page(left + right)
        assert _detect_page_columns(page) == 2

    def test_tiny_right_cluster_below_threshold_returns_1(self):
        """A tiny second cluster (<20% of words) does not trigger two-column."""
        # 80 words in left cluster, only 5 in right cluster (5/85 ≈ 6% < 20%)
        left = _words_at([10.0, 20.0, 30.0] * 27)  # 81 words
        right = _words_at([310.0, 320.0, 330.0, 340.0, 350.0])  # 5 words
        page = _make_page(left + right)
        assert _detect_page_columns(page) == 1

    def test_no_words_returns_1(self):
        page = _make_page([])
        assert _detect_page_columns(page) == 1

    def test_extract_words_raises_returns_1(self):
        """If extract_words raises, the function must return 1, not propagate."""
        page = MagicMock()
        page.extract_words.side_effect = RuntimeError("pdf error")
        page.width = 612.0
        assert _detect_page_columns(page) == 1

    def test_zero_width_page_returns_1(self):
        """A page with width=0 must return 1 (division guard)."""
        words = _words_at([10.0, 20.0, 300.0, 310.0] * 10)
        page = _make_page(words, width=0.0)
        assert _detect_page_columns(page) == 1

    def test_clusters_too_close_together_returns_1(self):
        """Two clusters that are only 10% of page width apart → single column."""
        # Gap of ~60px on a 612-wide page is < 183.6 threshold
        left = _words_at([50.0, 60.0, 70.0] * 10)
        right = _words_at([110.0, 120.0, 130.0] * 10)
        page = _make_page(left + right)
        assert _detect_page_columns(page) == 1


# ---------------------------------------------------------------------------
# _extract_text_column_aware — unit tests
# ---------------------------------------------------------------------------

class TestExtractTextColumnAware:
    def test_single_column_returns_extract_text_unchanged(self):
        """Single-column page → extract_text() result returned directly."""
        page = _make_page(_words_at([10.0, 20.0, 30.0] * 10))
        page.extract_text.return_value = "Full page text here."

        result = _extract_text_column_aware(page)

        assert result == "Full page text here."

    def test_two_column_returns_left_newline_newline_right(self):
        """Two-column page → crop left and right halves, join with '\\n\\n'."""
        # Set up two equal clusters so _detect_page_columns returns 2
        left_words = _words_at([10.0, 20.0, 30.0] * 10)
        right_words = _words_at([310.0, 320.0, 330.0] * 10)
        page = _make_page(left_words + right_words, width=612.0, height=792.0)

        # Mock the crop operations
        left_crop = MagicMock()
        left_crop.extract_text.return_value = "LEFT"
        right_crop = MagicMock()
        right_crop.extract_text.return_value = "RIGHT"

        half = page.width / 2  # 306.0

        def mock_crop(bbox):
            x0, y0, x1, y1 = bbox
            if x1 <= half + 1:  # left half
                return left_crop
            return right_crop

        page.crop.side_effect = mock_crop

        result = _extract_text_column_aware(page)

        assert result == "LEFT\n\nRIGHT"

    def test_two_column_none_extract_text_handled(self):
        """When crop().extract_text() returns None, treat as empty string."""
        left_words = _words_at([10.0, 20.0, 30.0] * 10)
        right_words = _words_at([310.0, 320.0, 330.0] * 10)
        page = _make_page(left_words + right_words, width=612.0)

        left_crop = MagicMock()
        left_crop.extract_text.return_value = None
        right_crop = MagicMock()
        right_crop.extract_text.return_value = "RIGHT"

        page.crop.side_effect = lambda bbox: left_crop if bbox[2] <= 306.1 else right_crop

        result = _extract_text_column_aware(page)

        # Should not raise; empty left + "\n\n" + "RIGHT"
        assert "RIGHT" in result


# ---------------------------------------------------------------------------
# Integration: _parse_with_pdfplumber emits two_column_layout_detected warning
# ---------------------------------------------------------------------------

class TestPdfPlumberTwoColumnWarning:
    def test_two_column_page_emits_warning_in_parse_with_pdfplumber(
        self, minimal_pdf: Path
    ):
        """_parse_with_pdfplumber must append a two_column_layout_detected warning
        for each page detected as two-column."""
        # Build a mock page that returns two-cluster words (→ _detect_page_columns == 2)
        left_words = [{"x0": float(x)} for x in [10, 20, 30] * 10]
        right_words = [{"x0": float(x)} for x in [310, 320, 330] * 10]
        all_words = left_words + right_words

        mock_page = MagicMock()
        mock_page.width = 612.0
        mock_page.height = 792.0
        mock_page.extract_words.return_value = all_words

        left_crop = MagicMock()
        left_crop.extract_text.return_value = "Left column text here."
        right_crop = MagicMock()
        right_crop.extract_text.return_value = "Right column text here."
        mock_page.crop.side_effect = lambda bbox: left_crop if bbox[2] <= 307 else right_crop

        mock_pdf = MagicMock()
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_pdf.pages = [mock_page]

        with patch("pdfplumber.open", return_value=mock_pdf):
            full_text, title, skipped, page_warnings, page_languages = (
                _parse_with_pdfplumber(str(minimal_pdf))
            )

        two_col_warnings = [
            w for w in page_warnings
            if isinstance(w, dict) and w.get("code") == "two_column_layout_detected"
        ]
        assert len(two_col_warnings) >= 1
        assert two_col_warnings[0]["page"] == 1

    def test_single_column_page_no_two_column_warning(self, minimal_pdf: Path):
        """Single-column page must NOT emit two_column_layout_detected."""
        single_words = [{"x0": float(x)} for x in [10, 20, 30, 40, 50] * 6]

        mock_page = MagicMock()
        mock_page.width = 612.0
        mock_page.height = 792.0
        mock_page.extract_words.return_value = single_words
        mock_page.extract_text.return_value = "Normal single column text."

        mock_pdf = MagicMock()
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_pdf.pages = [mock_page]

        with patch("pdfplumber.open", return_value=mock_pdf):
            full_text, title, skipped, page_warnings, page_languages = (
                _parse_with_pdfplumber(str(minimal_pdf))
            )

        two_col_warnings = [
            w for w in page_warnings
            if isinstance(w, dict) and w.get("code") == "two_column_layout_detected"
        ]
        assert two_col_warnings == []

    def test_parse_with_pdfplumber_returns_5_tuple(self, minimal_pdf: Path):
        """_parse_with_pdfplumber must return a 5-tuple."""
        mock_page = MagicMock()
        mock_page.width = 612.0
        mock_page.height = 792.0
        mock_page.extract_words.return_value = [{"x0": 10.0}] * 10
        mock_page.extract_text.return_value = "Simple text."

        mock_pdf = MagicMock()
        mock_pdf.__enter__ = MagicMock(return_value=mock_pdf)
        mock_pdf.__exit__ = MagicMock(return_value=False)
        mock_pdf.pages = [mock_page]

        with patch("pdfplumber.open", return_value=mock_pdf):
            result = _parse_with_pdfplumber(str(minimal_pdf))

        assert len(result) == 5, (
            "_parse_with_pdfplumber must return a 5-tuple: "
            "(full_text, title, skipped, page_warnings, page_languages)"
        )
        full_text, title, skipped, page_warnings, page_languages = result
        assert isinstance(full_text, str)
        assert isinstance(page_warnings, list)
        assert isinstance(page_languages, list)
