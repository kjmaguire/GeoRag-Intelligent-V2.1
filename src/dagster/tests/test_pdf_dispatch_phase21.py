"""Phase 2.1 (2026-05-22) — fitz-first dispatch + docling-OCR merge tests.

These tests exercise the top-level parse_pdf_report dispatch and the
per-page merge between fitz's native-text output and docling's
rapidocr-OCR output. The merge rule (chosen by Kyle 2026-05-22):
"fitz wins where it has any text ≥ PER_PAGE_MIN_CHARS; docling only
overrides on the pages fitz returned as image-pages."

No real docling / rapidocr / pdfplumber installs needed — every parser
is patched at the module level. Tests run on the host without container
dependencies.

Run with:
    pytest src/dagster/tests/test_pdf_dispatch_phase21.py -v
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock, patch

import pytest


# Inject fake boto3 + botocore.config so the parser module imports cleanly
sys.modules.setdefault("boto3", MagicMock())
sys.modules.setdefault("botocore", MagicMock())
sys.modules.setdefault("botocore.config", MagicMock())


@pytest.fixture
def parser_module():
    """Re-import the parser module fresh for each test so env-driven
    module-level constants aren't pinned across cases."""
    import importlib
    from georag_dagster.parsers import pdf_report
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


def _stub_docling(parser_module, per_page, tables=None, figures=None,
                  warnings=None, langs=None):
    full_text = "\n".join(t for _n, t in per_page)

    def _fake(path, pdf_sha256=None):
        return (
            full_text, "Docling Title", 0, list(warnings or []),
            list(langs or ["unknown"] * len(per_page)),
            list(per_page),
            list(tables or []),
            list(figures or []),
        )

    return patch.object(parser_module, "_parse_with_docling", side_effect=_fake)


def _stub_tesseract(parser_module, recovered_map, confidence_map=None):
    """Patch _ocr_single_page so page N returns recovered_map[N].

    Phase 3 (2026-05-22): when called with `return_confidence=True`,
    returns ``(text, mean_conf)`` where mean_conf comes from
    confidence_map (default 0.85). When called without, returns just
    the text (legacy callers).
    """
    confidence_map = confidence_map or {}

    def _fake(path, page_num, return_confidence=False):
        text = recovered_map.get(page_num, "")
        conf = confidence_map.get(page_num, 0.85)
        return (text, conf) if return_confidence else text

    return patch.object(parser_module, "_ocr_single_page", side_effect=_fake)


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
# 1. Text-only PDF: fitz returns text for every page → docling NEVER fires
# ---------------------------------------------------------------------------

def test_text_only_pdf_skips_docling(parser_module, minimal_pdf, monkeypatch):
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "true")
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "true")

    fitz_pages = [(1, "Page one body text " * 20), (2, "Page two body text " * 20)]
    docling_called = MagicMock()

    with _stub_fitz(parser_module, fitz_pages, image_pages=[]), \
            patch.object(parser_module, "_parse_with_docling", docling_called):
        result = parser_module.parse_pdf_report(minimal_pdf)

    assert result.parser_used == "fitz"
    docling_called.assert_not_called()
    assert "Page one body text" in (result.sections[0].text
                                    if result.sections else "")


# ---------------------------------------------------------------------------
# 2. PDF with one image page: fitz + docling merge
# ---------------------------------------------------------------------------

def test_fitz_with_image_pages_triggers_docling_merge(
    parser_module, minimal_pdf, monkeypatch
):
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "true")
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "true")

    fitz_pages = [
        (1, "Page one native text " * 20),
        (2, ""),  # image page — fitz returned nothing
        (3, "Page three native text " * 20),
    ]
    docling_pages = [
        (1, "Docling rendered text one"),
        (2, "Docling OCR'd image page two text"),
        (3, "Docling rendered text three"),
    ]

    with _stub_fitz(parser_module, fitz_pages, image_pages=[2]), \
            _stub_docling(parser_module, docling_pages,
                          tables=[], figures=[{"idx": 0, "page": 2}]):
        result = parser_module.parse_pdf_report(minimal_pdf)

    assert result.parser_used == "fitz+docling_ocr"
    # Phase 1 figure manifest propagates
    assert len(result.figure_manifest) == 1
    # Merge: fitz wins on pages 1 + 3; docling fills page 2
    combined = "\n".join(s.text for s in result.sections)
    assert "Page one native text" in combined
    assert "Docling OCR'd image page two text" in combined
    assert "Page three native text" in combined
    # Docling text for pages 1/3 should NOT be present (fitz wins)
    assert "Docling rendered text one" not in combined


# ---------------------------------------------------------------------------
# 3. All-image PDF: fitz returns nothing → all pages are image pages → docling fills
# ---------------------------------------------------------------------------

def test_all_image_pdf_full_docling(parser_module, minimal_pdf, monkeypatch):
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "true")
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "true")

    fitz_pages = [(1, ""), (2, ""), (3, "")]
    docling_pages = [
        (1, "Scanned page one OCR text here is long enough to count " * 3),
        (2, "Scanned page two OCR text here is long enough to count " * 3),
        (3, "Scanned page three OCR text here is long enough to count " * 3),
    ]

    with _stub_fitz(parser_module, fitz_pages, image_pages=[1, 2, 3]), \
            _stub_docling(parser_module, docling_pages):
        result = parser_module.parse_pdf_report(minimal_pdf)

    assert result.parser_used == "fitz+docling_ocr"
    combined = "\n".join(s.text for s in result.sections)
    assert "Scanned page one OCR text" in combined
    assert "Scanned page two OCR text" in combined
    assert "Scanned page three OCR text" in combined


# ---------------------------------------------------------------------------
# 4. Docling unavailable + tesseract fallback enabled → tesseract fires
# ---------------------------------------------------------------------------

def test_docling_disabled_falls_back_to_tesseract(
    parser_module, minimal_pdf, monkeypatch
):
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "false")
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "false")
    monkeypatch.setenv("PDF_PARSER_TESSERACT_FALLBACK_ENABLED", "true")

    fitz_pages = [(1, "Native text " * 30), (2, "")]
    docling_called = MagicMock()

    with _stub_fitz(parser_module, fitz_pages, image_pages=[2]), \
            _stub_tesseract(parser_module,
                            {2: "Tesseract recovered image page text " * 4}), \
            patch.object(parser_module, "_parse_with_docling", docling_called):
        result = parser_module.parse_pdf_report(minimal_pdf)

    assert result.parser_used == "fitz+tesseract_fallback"
    docling_called.assert_not_called()
    combined = "\n".join(s.text for s in result.sections)
    assert "Tesseract recovered image page text" in combined


# ---------------------------------------------------------------------------
# 5. Docling raises → tesseract fallback recovers (no data loss)
# ---------------------------------------------------------------------------

def test_docling_failure_falls_back_to_tesseract(
    parser_module, minimal_pdf, monkeypatch
):
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "true")
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "true")
    monkeypatch.setenv("PDF_PARSER_TESSERACT_FALLBACK_ENABLED", "true")

    fitz_pages = [(1, "Native " * 30), (2, "")]

    def _docling_explodes(path, pdf_sha256=None):
        raise RuntimeError("simulated docling crash")

    with _stub_fitz(parser_module, fitz_pages, image_pages=[2]), \
            patch.object(parser_module, "_parse_with_docling",
                         side_effect=_docling_explodes), \
            _stub_tesseract(parser_module,
                            {2: "Tesseract saved the day " * 5}):
        result = parser_module.parse_pdf_report(minimal_pdf)

    assert result.parser_used == "fitz+tesseract_fallback"
    combined = "\n".join(s.text for s in result.sections)
    assert "Tesseract saved the day" in combined


# ---------------------------------------------------------------------------
# 6. Tesseract fallback also disabled → image page is recorded but empty
#    (matches no-data-loss policy: empty page > missing page)
# ---------------------------------------------------------------------------

def test_no_ocr_path_keeps_native_pages(parser_module, minimal_pdf, monkeypatch):
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "false")
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "false")
    monkeypatch.setenv("PDF_PARSER_TESSERACT_FALLBACK_ENABLED", "false")

    fitz_pages = [(1, "Native page text " * 30), (2, "")]
    docling_called = MagicMock()
    tesseract_called = MagicMock()

    with _stub_fitz(parser_module, fitz_pages, image_pages=[2]), \
            patch.object(parser_module, "_parse_with_docling", docling_called), \
            patch.object(parser_module, "_ocr_single_page", tesseract_called):
        result = parser_module.parse_pdf_report(minimal_pdf)

    docling_called.assert_not_called()
    tesseract_called.assert_not_called()
    # Page 1 text still present
    combined = "\n".join(s.text for s in result.sections)
    assert "Native page text" in combined


# ---------------------------------------------------------------------------
# 7. Merge rule: fitz returns short non-empty text on a page → fitz still wins
# ---------------------------------------------------------------------------

def test_merge_fitz_wins_when_above_threshold(
    parser_module, minimal_pdf, monkeypatch
):
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "true")
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "true")

    # Build a page with text >= MIN_EXTRACTABLE_TEXT_CHARS (200 chars).
    # image_pages=[] means fitz reported this as a text page → never
    # eligible for the docling override.
    fitz_pages = [(1, "A" * 300)]
    docling_pages = [(1, "Different docling text " * 10)]

    with _stub_fitz(parser_module, fitz_pages, image_pages=[]), \
            _stub_docling(parser_module, docling_pages):
        result = parser_module.parse_pdf_report(minimal_pdf)

    combined = "\n".join(s.text for s in result.sections)
    # Fitz output present
    assert "A" * 100 in combined
    # Docling output NOT present (docling shouldn't even have been called
    # since image_page_nums was empty)
    assert "Different docling text" not in combined
    # parser_used reflects fitz-only
    assert result.parser_used == "fitz"


# ---------------------------------------------------------------------------
# 8. Merge rule: image page reported by fitz, docling provides text → docling wins
# ---------------------------------------------------------------------------

def test_merge_docling_overrides_image_page(
    parser_module, minimal_pdf, monkeypatch
):
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "true")
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "true")

    fitz_pages = [(1, "fitz native " * 20), (2, "")]
    docling_pages = [(1, "docling rendered " * 5),
                     (2, "DOCLING OCR'D IMAGE PAGE TWO" * 3)]

    with _stub_fitz(parser_module, fitz_pages, image_pages=[2]), \
            _stub_docling(parser_module, docling_pages):
        result = parser_module.parse_pdf_report(minimal_pdf)

    combined = "\n".join(s.text for s in result.sections)
    assert "fitz native" in combined
    assert "DOCLING OCR'D IMAGE PAGE TWO" in combined
    # Docling output for page 1 should NOT win
    assert "docling rendered" not in combined


# ---------------------------------------------------------------------------
# 9. Figure manifest from docling propagates into ReportParseResult
# ---------------------------------------------------------------------------

def test_figure_manifest_propagates_through_dispatch(
    parser_module, minimal_pdf, monkeypatch
):
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "true")
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "true")

    fitz_pages = [(1, "text " * 30), (2, "")]
    figures = [
        {"idx": 0, "page": 2, "caption": "Cross section A",
         "pending_key": "figures/_pending/abc/figure_0000_page_2.png",
         "bucket": "bronze", "sha256": "h0"},
    ]
    docling_pages = [(2, "OCR text for image page two here goes  " * 3)]

    with _stub_fitz(parser_module, fitz_pages, image_pages=[2]), \
            _stub_docling(parser_module, docling_pages, figures=figures):
        result = parser_module.parse_pdf_report(minimal_pdf)

    assert result.figure_manifest == figures
    assert result.parser_used == "fitz+docling_ocr"


# ---------------------------------------------------------------------------
# 10. Docling tables propagate when present
# ---------------------------------------------------------------------------

def test_docling_tables_propagate(parser_module, minimal_pdf, monkeypatch):
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "true")
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "true")

    fitz_pages = [(1, "narrative " * 30), (2, "")]
    docling_pages = [(2, "table page text " * 10)]
    table_section = parser_module.ReportSection(
        section_number=None,
        section_title="Table (docling, page 2)",
        text="| col1 | col2 |\n|---|---|\n| a | b |",
        page_first=2,
        page_last=2,
    )

    with _stub_fitz(parser_module, fitz_pages, image_pages=[2]), \
            _stub_docling(parser_module, docling_pages,
                          tables=[table_section]):
        result = parser_module.parse_pdf_report(minimal_pdf)

    # The table section is appended to the result's sections
    table_titles = [s.section_title for s in result.sections
                    if s.section_title and "Table" in s.section_title]
    assert any("docling" in t for t in table_titles)


# ---------------------------------------------------------------------------
# 11. Fitz throws entirely → pdfplumber fallback fires (no docling, no tesseract)
# ---------------------------------------------------------------------------

def test_fitz_total_failure_falls_back_to_pdfplumber(
    parser_module, minimal_pdf, monkeypatch
):
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "true")
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "true")

    pdfplumber_pages = [(1, "Pdfplumber recovered page one " * 10)]
    docling_called = MagicMock()

    def _fitz_explodes(path, apply_ocr_fallback=True):
        raise RuntimeError("simulated fitz crash")

    with patch.object(parser_module, "_parse_with_fitz",
                      side_effect=_fitz_explodes), \
            _stub_pdfplumber(
                parser_module,
                full_text="Pdfplumber recovered page one " * 10,
                per_page=pdfplumber_pages,
            ), \
            patch.object(parser_module, "_parse_with_docling", docling_called):
        result = parser_module.parse_pdf_report(minimal_pdf)

    assert result.parser_used == "pdfplumber"
    docling_called.assert_not_called()


# ---------------------------------------------------------------------------
# 12. apply_ocr_fallback param controls fitz's internal tesseract loop
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

    # We mock the inner pymupdf import to return one short page
    fake_page = MagicMock()
    fake_page.get_text = MagicMock(return_value="")

    class _FakeDoc:
        metadata = {"title": "T"}

        def __iter__(self):
            return iter([fake_page])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_pymupdf = types.ModuleType("pymupdf")
    fake_pymupdf.open = MagicMock(return_value=_FakeDoc())
    monkeypatch.setitem(sys.modules, "pymupdf", fake_pymupdf)

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
# 13. apply_ocr_fallback=True (default) still runs tesseract loop
# ---------------------------------------------------------------------------

def test_parse_with_fitz_apply_ocr_fallback_default_runs_loop(
    parser_module, monkeypatch
):
    fake_page = MagicMock()
    fake_page.get_text = MagicMock(return_value="")

    class _FakeDoc:
        metadata = {"title": "T"}

        def __iter__(self):
            return iter([fake_page])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    fake_pymupdf = types.ModuleType("pymupdf")
    fake_pymupdf.open = MagicMock(return_value=_FakeDoc())
    monkeypatch.setitem(sys.modules, "pymupdf", fake_pymupdf)

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
# 14. _parse_with_fitz 7-element return shape is stable
# ---------------------------------------------------------------------------

def test_parse_with_fitz_returns_nine_tuple(parser_module, monkeypatch):
    """Phase 3 extended the return shape from 7- to 9-tuple by adding
    `per_page_method` + `per_page_confidence` for OCR provenance."""
    class _FakeDoc:
        metadata = {}
        def __iter__(self): return iter([])
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake_pymupdf = types.ModuleType("pymupdf")
    fake_pymupdf.open = MagicMock(return_value=_FakeDoc())
    monkeypatch.setitem(sys.modules, "pymupdf", fake_pymupdf)

    out = parser_module._parse_with_fitz("/tmp/fake.pdf")
    assert isinstance(out, tuple) and len(out) == 9
    # image_page_nums (slot 6)
    assert out[6] == []
    # per_page_method + per_page_confidence (slots 7 + 8) — empty dicts
    # since the fake doc has no pages
    assert out[7] == {}
    assert out[8] == {}


# ---------------------------------------------------------------------------
# 15. Default env values match Phase 2.1 cutover (docling + OCR on)
# ---------------------------------------------------------------------------

def test_default_envs_route_through_docling_ocr_path(
    parser_module, minimal_pdf, monkeypatch
):
    """When no envs are set, the new Phase 2.1 dispatch should still
    treat PDF_PARSER_DOCLING_ENABLED=true and DOCLING_OCR_ENABLED=true
    as defaults (the module reads `(default, 'true')` in the helpers)."""
    for k in (
        "PDF_PARSER_DOCLING_ENABLED",
        "DOCLING_OCR_ENABLED",
        "PDF_PARSER_TESSERACT_FALLBACK_ENABLED",
    ):
        monkeypatch.delenv(k, raising=False)

    fitz_pages = [(1, "text " * 20), (2, "")]
    docling_pages = [(2, "image page two ocr text " * 5)]

    with _stub_fitz(parser_module, fitz_pages, image_pages=[2]), \
            _stub_docling(parser_module, docling_pages):
        result = parser_module.parse_pdf_report(minimal_pdf)

    assert result.parser_used == "fitz+docling_ocr"


# ---------------------------------------------------------------------------
# 16. PDF_PARSER_DOCLING_ENABLED=false explicitly → docling does NOT run even
#     when image pages exist + tesseract fires instead
# ---------------------------------------------------------------------------

def test_explicit_docling_off_skips_docling_even_with_image_pages(
    parser_module, minimal_pdf, monkeypatch
):
    monkeypatch.setenv("PDF_PARSER_DOCLING_ENABLED", "false")
    monkeypatch.setenv("DOCLING_OCR_ENABLED", "true")  # ignored when DOCLING off
    monkeypatch.setenv("PDF_PARSER_TESSERACT_FALLBACK_ENABLED", "true")

    fitz_pages = [(1, "text " * 40), (2, "")]
    docling_called = MagicMock()

    with _stub_fitz(parser_module, fitz_pages, image_pages=[2]), \
            _stub_tesseract(parser_module,
                            {2: "tess recovered image page two " * 5}), \
            patch.object(parser_module, "_parse_with_docling", docling_called):
        result = parser_module.parse_pdf_report(minimal_pdf)

    docling_called.assert_not_called()
    assert result.parser_used == "fitz+tesseract_fallback"
