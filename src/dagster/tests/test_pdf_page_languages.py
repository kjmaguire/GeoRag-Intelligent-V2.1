"""Tests for per-page language detection added in Sprint 3.

Covers:
  - _detect_page_language: English, French, empty, too-short, determinism.
  - parse_pdf_report integration: mixed_language_document warning for 2-language docs.
  - Single-language doc: no mixed_language_document warning.
  - Determinism guarantee from DetectorFactory.seed = 0.

Run with:  pytest tests/test_pdf_page_languages.py -v
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from georag_dagster.parsers.pdf_report import (
    _detect_page_language,
    parse_pdf_report,
)

# ---------------------------------------------------------------------------
# Minimal PDF bytes
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

# Sufficiently long English and French texts for reliable langdetect output.
_EN_TEXT = (
    "The quick brown fox jumps over the lazy dog. "
    "Exploration drilling commenced in the northern portion of the property. "
    "The mineral resource estimate was prepared in accordance with NI 43-101. "
) * 3

_FR_TEXT = (
    "Le renard brun saute par-dessus le chien paresseux. "
    "Les travaux d'exploration ont commence dans la partie nord de la propriete. "
    "L'estimation des ressources minerales a ete preparee conformement au NI 43-101. "
) * 3


@pytest.fixture()
def minimal_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "test.pdf"
    p.write_bytes(_MINIMAL_PDF_CONTENT)
    return p


# ---------------------------------------------------------------------------
# Pure-unit: _detect_page_language
# ---------------------------------------------------------------------------

class TestDetectPageLanguage:
    def test_english_text_returns_en(self):
        result = _detect_page_language(_EN_TEXT)
        assert result == "en"

    def test_french_text_returns_fr(self):
        result = _detect_page_language(_FR_TEXT)
        assert result == "fr"

    def test_empty_string_returns_unknown(self):
        assert _detect_page_language("") == "unknown"

    def test_single_char_returns_unknown(self):
        assert _detect_page_language("a") == "unknown"

    def test_whitespace_only_returns_unknown(self):
        assert _detect_page_language("   \n\t  ") == "unknown"

    def test_short_text_under_threshold_returns_unknown(self):
        # The function requires >= 20 stripped chars; 19 chars should return unknown.
        short = "abcdefghijklmnopqrs"  # 19 chars
        assert len(short.strip()) < 20
        assert _detect_page_language(short) == "unknown"

    def test_determinism_same_input_same_output(self):
        """Call twice on identical text → same result (DetectorFactory.seed = 0)."""
        first = _detect_page_language(_EN_TEXT)
        second = _detect_page_language(_EN_TEXT)
        assert first == second

    def test_determinism_french_same_output(self):
        first = _detect_page_language(_FR_TEXT)
        second = _detect_page_language(_FR_TEXT)
        assert first == second

    def test_result_is_string(self):
        assert isinstance(_detect_page_language(_EN_TEXT), str)

    def test_unknown_language_returns_other_or_known_tag(self):
        # For any non-empty text >= 20 chars, result is a non-empty string.
        result = _detect_page_language(_EN_TEXT)
        assert len(result) > 0


# ---------------------------------------------------------------------------
# Integration: mixed_language_document warning
# ---------------------------------------------------------------------------

class TestMixedLanguageWarning:
    def test_two_language_doc_emits_mixed_language_warning(self, minimal_pdf: Path):
        """When _parse_with_pdfplumber returns page_languages ['en', 'fr'],
        parse_pdf_report must emit a mixed_language_document warning."""
        mock_text = "1. Summary\nThis is an NI 43-101 technical report.\n"
        mock_page_languages = ["en", "fr"]

        with (
            patch(
                "georag_dagster.parsers.pdf_report._parse_with_unstructured",
                side_effect=ImportError("not available"),
            ),
            patch(
                "georag_dagster.parsers.pdf_report._parse_with_pdfplumber",
                return_value=(mock_text, "Test Report", 0, [], mock_page_languages),
            ),
            patch(
                "georag_dagster.parsers.pdf_report._extract_resource_tables",
                return_value=[],
            ),
        ):
            result = parse_pdf_report(str(minimal_pdf))

        codes = [w.get("code") for w in result.warnings if isinstance(w, dict)]
        assert "mixed_language_document" in codes, (
            f"Expected 'mixed_language_document' warning; got warnings: {result.warnings}"
        )

    def test_mixed_language_warning_context_contains_languages(self, minimal_pdf: Path):
        """mixed_language_document warning context must list the detected languages."""
        mock_text = "1. Summary\nTest NI 43-101 report.\n"
        mock_page_languages = ["en", "fr"]

        with (
            patch(
                "georag_dagster.parsers.pdf_report._parse_with_unstructured",
                side_effect=ImportError("not available"),
            ),
            patch(
                "georag_dagster.parsers.pdf_report._parse_with_pdfplumber",
                return_value=(mock_text, "Test Report", 0, [], mock_page_languages),
            ),
            patch(
                "georag_dagster.parsers.pdf_report._extract_resource_tables",
                return_value=[],
            ),
        ):
            result = parse_pdf_report(str(minimal_pdf))

        mixed_warnings = [
            w for w in result.warnings
            if isinstance(w, dict) and w.get("code") == "mixed_language_document"
        ]
        assert mixed_warnings
        ctx = mixed_warnings[0].get("context", {})
        langs = ctx.get("languages", [])
        assert "en" in langs
        assert "fr" in langs

    def test_single_language_doc_no_mixed_warning(self, minimal_pdf: Path):
        """A doc with only English pages must NOT emit mixed_language_document."""
        mock_text = "1. Summary\nThis is an NI 43-101 technical report.\n"
        mock_page_languages = ["en", "en", "en"]

        with (
            patch(
                "georag_dagster.parsers.pdf_report._parse_with_unstructured",
                side_effect=ImportError("not available"),
            ),
            patch(
                "georag_dagster.parsers.pdf_report._parse_with_pdfplumber",
                return_value=(mock_text, "Test Report", 0, [], mock_page_languages),
            ),
            patch(
                "georag_dagster.parsers.pdf_report._extract_resource_tables",
                return_value=[],
            ),
        ):
            result = parse_pdf_report(str(minimal_pdf))

        codes = [w.get("code") for w in result.warnings if isinstance(w, dict)]
        assert "mixed_language_document" not in codes

    def test_all_unknown_languages_no_mixed_warning(self, minimal_pdf: Path):
        """All-unknown page languages must NOT emit mixed_language_document
        (unknown is excluded from the unique-language count)."""
        mock_text = "1. Summary\nTest NI 43-101 report.\n"
        mock_page_languages = ["unknown", "unknown"]

        with (
            patch(
                "georag_dagster.parsers.pdf_report._parse_with_unstructured",
                side_effect=ImportError("not available"),
            ),
            patch(
                "georag_dagster.parsers.pdf_report._parse_with_pdfplumber",
                return_value=(mock_text, "Test Report", 0, [], mock_page_languages),
            ),
            patch(
                "georag_dagster.parsers.pdf_report._extract_resource_tables",
                return_value=[],
            ),
        ):
            result = parse_pdf_report(str(minimal_pdf))

        codes = [w.get("code") for w in result.warnings if isinstance(w, dict)]
        assert "mixed_language_document" not in codes

    def test_page_languages_stored_on_result(self, minimal_pdf: Path):
        """result.page_languages must reflect the per-page language list."""
        mock_text = "1. Summary\nTest NI 43-101 report.\n"
        mock_page_languages = ["en", "fr"]

        with (
            patch(
                "georag_dagster.parsers.pdf_report._parse_with_unstructured",
                side_effect=ImportError("not available"),
            ),
            patch(
                "georag_dagster.parsers.pdf_report._parse_with_pdfplumber",
                return_value=(mock_text, "Test Report", 0, [], mock_page_languages),
            ),
            patch(
                "georag_dagster.parsers.pdf_report._extract_resource_tables",
                return_value=[],
            ),
        ):
            result = parse_pdf_report(str(minimal_pdf))

        assert result.page_languages == ["en", "fr"]
