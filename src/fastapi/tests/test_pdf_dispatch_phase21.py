"""_parse_with_fitz apply_ocr_fallback param tests.

Originally part of the Phase 2.1 (2026-05-22) fitz-first dispatch +
docling-OCR merge test suite. The docling merge tests (fitz-vs-docling
per-page merge, figure_manifest propagation, table propagation, and the
PDF_PARSER_DOCLING_ENABLED/DOCLING_OCR_ENABLED env-var dispatch behavior)
were removed 2026-07-29 along with docling itself — the merge mechanism
they tested no longer exists in app.services.ingest.pdf_report
(parse_pdf_report's dispatch is now: fitz (with its internal tesseract
loop gated by PDF_PARSER_TESSERACT_FALLBACK_ENABLED) → pdfplumber
fallback on total failure). Docling never ran in production
(PDF_PARSER_DOCLING_ENABLED was false in every live deployment; 1,173
real silver.reports rows show parser_used as fitz or pdfplumber, never
docling).

The tests below are unaffected by that removal — they exercise
_parse_with_fitz's `apply_ocr_fallback` parameter directly, which is
still live (it now gates the tesseract loop unconditionally rather than
skipping it in favor of a docling merge).

Run with:
    pytest src/fastapi/tests/test_pdf_dispatch_phase21.py -v
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def parser_module():
    """Re-import the parser module fresh for each test so env-driven
    module-level constants aren't pinned across cases."""
    import importlib

    from app.services.ingest import pdf_report
    importlib.reload(pdf_report)
    return pdf_report


# Minimal valid PDF bytes (same pattern as other PDF tests).
_MINIMAL_PDF = b"""%PDF-1.4
1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj
2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj
3 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]
  /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>
endobj
4 0 obj << /Length 44 >>
stream
BT /F1 12 Tf 100 700 Td (Hello World) Tj ET
endstream
endobj
5 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj
xref
0 6
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000231 00000 n
0000000324 00000 n
trailer << /Size 6 /Root 1 0 R >>
startxref
391
%%EOF
"""


@pytest.fixture
def minimal_pdf(tmp_path):
    p = tmp_path / "minimal.pdf"
    p.write_bytes(_MINIMAL_PDF)
    return str(p)


def _stub_fitz(parser_module, per_page, image_pages, warnings=None,
               per_page_method=None, per_page_confidence=None):
    """Patch _parse_with_fitz to return the Phase-3-extended 9-tuple."""
    full_text = "\n".join(t for _n, t in per_page)
    page_langs = ["en" if t else "unknown" for _n, t in per_page]
    image_set = set(image_pages)
    default_method = {
        n: ("tesseract" if n in image_set else "fitz_native")
        for n, _t in per_page
    }
    default_conf = {
        n: (None if n not in image_set else 0.85) for n, _t in per_page
    }
    method = per_page_method if per_page_method is not None else default_method
    conf = per_page_confidence if per_page_confidence is not None else default_conf

    def _fake(path, apply_ocr_fallback=True):
        return (
            full_text, "Test Doc", 0, list(warnings or []), page_langs,
            list(per_page), list(image_pages),
            dict(method), dict(conf),
        )

    return patch.object(parser_module, "_parse_with_fitz", side_effect=_fake)


def _stub_pdfplumber(parser_module, full_text="", per_page=None):
    per_page = per_page or []

    def _fake(path):
        return (
            full_text, "Pdfplumber Title", 0, [],
            ["en"] * len(per_page),
            list(per_page),
        )
    return patch.object(parser_module, "_parse_with_pdfplumber", side_effect=_fake)


# ---------------------------------------------------------------------------
# parse_pdf_report dispatch: fitz total failure → pdfplumber fallback fires
# ---------------------------------------------------------------------------

def test_fitz_total_failure_falls_back_to_pdfplumber(
    parser_module, minimal_pdf, monkeypatch
):
    pdfplumber_pages = [(1, "Pdfplumber recovered page one " * 10)]

    def _fitz_explodes(path, apply_ocr_fallback=True):
        raise RuntimeError("simulated fitz crash")

    with patch.object(parser_module, "_parse_with_fitz",
                      side_effect=_fitz_explodes), \
            _stub_pdfplumber(
                parser_module,
                full_text="Pdfplumber recovered page one " * 10,
                per_page=pdfplumber_pages,
            ):
        result = parser_module.parse_pdf_report(minimal_pdf)

    assert result.parser_used == "pdfplumber"


# ---------------------------------------------------------------------------
# parse_pdf_report dispatch: PDF_PARSER_TESSERACT_FALLBACK_ENABLED controls
# whether _parse_with_fitz's internal tesseract loop runs
# ---------------------------------------------------------------------------

def test_tesseract_fallback_enabled_recovers_image_pages(
    parser_module, minimal_pdf, monkeypatch
):
    monkeypatch.setenv("PDF_PARSER_TESSERACT_FALLBACK_ENABLED", "true")

    captured_apply_ocr_fallback = []

    def _fake_parse_with_fitz(path, apply_ocr_fallback=True):
        captured_apply_ocr_fallback.append(apply_ocr_fallback)
        return (
            "Native text " * 30, "Test Doc", 0, [], ["en"],
            [(1, "Native text " * 30)], [],
            {1: "fitz_native"}, {1: None},
        )

    with patch.object(parser_module, "_parse_with_fitz",
                      side_effect=_fake_parse_with_fitz):
        result = parser_module.parse_pdf_report(minimal_pdf)

    assert captured_apply_ocr_fallback == [True]
    assert result.parser_used == "fitz"


def test_tesseract_fallback_disabled_skips_internal_ocr_loop(
    parser_module, minimal_pdf, monkeypatch
):
    monkeypatch.setenv("PDF_PARSER_TESSERACT_FALLBACK_ENABLED", "false")

    captured_apply_ocr_fallback = []

    def _fake_parse_with_fitz(path, apply_ocr_fallback=True):
        captured_apply_ocr_fallback.append(apply_ocr_fallback)
        return (
            "Native text " * 30, "Test Doc", 0, [], ["en"],
            [(1, "Native text " * 30)], [2],
            {1: "fitz_native"}, {1: None},
        )

    with patch.object(parser_module, "_parse_with_fitz",
                      side_effect=_fake_parse_with_fitz):
        result = parser_module.parse_pdf_report(minimal_pdf)

    assert captured_apply_ocr_fallback == [False]
    assert result.parser_used == "fitz"


def _install_fake_pypdfium2(monkeypatch, page_texts, *, title=None):
    """Install a fake ``pypdfium2`` whose ``PdfDocument`` yields ``page_texts``
    (one string per page via ``get_textpage().get_text_bounded()``).

    Used by the tests that drive the REAL ``_parse_with_fitz`` body — which,
    after PyMuPDF was removed for its AGPL license, extracts native text via
    pypdfium2. The lazy ``import pypdfium2`` inside the parser resolves this
    from ``sys.modules`` at call time.
    """
    class _FakeTextPage:
        def __init__(self, text):
            self._text = text

        def get_text_bounded(self):
            return self._text

    class _FakePage:
        def __init__(self, text):
            self._text = text

        def get_textpage(self):
            return _FakeTextPage(self._text)

    class _FakeDoc:
        def __init__(self):
            self._pages = [_FakePage(t) for t in page_texts]

        def get_metadata_dict(self):
            return {"Title": title} if title is not None else {}

        def __len__(self):
            return len(self._pages)

        def __getitem__(self, i):
            return self._pages[i]

        def close(self):
            pass

    fake = types.ModuleType("pypdfium2")
    fake.PdfDocument = MagicMock(return_value=_FakeDoc())
    monkeypatch.setitem(sys.modules, "pypdfium2", fake)


# ---------------------------------------------------------------------------
# apply_ocr_fallback param controls fitz's internal tesseract loop
# ---------------------------------------------------------------------------

def test_parse_with_fitz_apply_ocr_fallback_param_skips_loop(
    parser_module, monkeypatch
):
    """Direct unit test on _parse_with_fitz: when apply_ocr_fallback=False,
    the tesseract loop must not fire, regardless of short pages."""

    captured_ocr_calls = []

    def _track_ocr(path, page_num):
        captured_ocr_calls.append(page_num)
        return "x" * 200

    # One short page (empty text) — routed to the OCR-candidate list.
    _install_fake_pypdfium2(monkeypatch, [""], title="T")

    monkeypatch.setattr(parser_module, "_ocr_single_page", _track_ocr)

    out = parser_module._parse_with_fitz("/tmp/fake.pdf", apply_ocr_fallback=False)
    # Output unpacked — Phase 3 added per_page_method + per_page_confidence
    full_text, _, _, _, _, _, image_page_nums, method, conf = out
    assert image_page_nums == [1]
    assert captured_ocr_calls == []  # tesseract never invoked
    assert full_text == ""
    # Page 1 was short → not in the method/conf maps (it didn't fall into
    # either the text-layer branch nor the OCR-recovery branch)
    assert 1 not in method


# ---------------------------------------------------------------------------
# apply_ocr_fallback=True (default) still runs tesseract loop
# ---------------------------------------------------------------------------

def test_parse_with_fitz_apply_ocr_fallback_default_runs_loop(
    parser_module, monkeypatch
):
    _install_fake_pypdfium2(monkeypatch, [""], title="T")

    # Phase 3: return_confidence=True path; stub returns (text, conf)
    monkeypatch.setattr(
        parser_module, "_ocr_single_page",
        MagicMock(return_value=("tesseract recovered " * 10, 0.82)),
    )

    out = parser_module._parse_with_fitz("/tmp/fake.pdf", apply_ocr_fallback=True)
    full_text, _, _, warnings, _, per_page, image_page_nums, method, conf = out
    # tesseract fired and recovered page 1 → it's no longer in image_page_nums
    assert image_page_nums == []
    assert "tesseract recovered" in full_text
    assert any(w.get("code") == "page_ocr_recovered_fitz" for w in warnings)
    # Phase 3 — recovered page got method='tesseract' + a confidence
    assert method.get(1) == "tesseract"
    assert conf.get(1) == 0.82


# ---------------------------------------------------------------------------
# _parse_with_fitz 9-element return shape is stable
# ---------------------------------------------------------------------------

def test_parse_with_fitz_returns_nine_tuple(parser_module, monkeypatch):
    """Phase 3 extended the return shape from 7- to 9-tuple by adding
    `per_page_method` + `per_page_confidence` for OCR provenance."""
    _install_fake_pypdfium2(monkeypatch, [])

    out = parser_module._parse_with_fitz("/tmp/fake.pdf")
    assert isinstance(out, tuple) and len(out) == 9
    # image_page_nums (slot 6)
    assert out[6] == []
    # per_page_method + per_page_confidence (slots 7 + 8) — empty dicts
    # since the fake doc has no pages
    assert out[7] == {}
    assert out[8] == {}
