"""Phase 3 (2026-05-22) — OCR confidence + method capture in the parser.

These tests verify that:
  - ReportSection has the two new optional fields with default None.
  - _ocr_single_page(return_confidence=True) returns (text, mean_conf).
  - _parse_with_fitz populates per_page_method + per_page_confidence
    correctly for text-layer pages (fitz_native, None confidence) and
    tesseract-recovered pages (tesseract, captured confidence).
  - _assign_ocr_metadata applies first-page-method-wins and min-
    confidence-across-spanned-pages rules.

No real docling / pdfplumber / pymupdf installs needed. All parsers
are stubbed.

Run with:
    pytest src/dagster/tests/test_pdf_ocr_confidence_capture.py -v
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest


sys.modules.setdefault("boto3", MagicMock())
sys.modules.setdefault("botocore", MagicMock())
sys.modules.setdefault("botocore.config", MagicMock())


@pytest.fixture
def parser_module():
    import importlib
    from georag_dagster.parsers import pdf_report
    importlib.reload(pdf_report)
    return pdf_report


# ---------------------------------------------------------------------------
# 1. ReportSection has the two new fields with default None
# ---------------------------------------------------------------------------

def test_report_section_has_ocr_fields_default_none(parser_module):
    s = parser_module.ReportSection(
        section_number="1", section_title="Summary", text="hello",
    )
    assert hasattr(s, "ocr_confidence")
    assert hasattr(s, "ocr_method")
    assert s.ocr_confidence is None
    assert s.ocr_method is None


def test_report_section_accepts_ocr_fields(parser_module):
    s = parser_module.ReportSection(
        section_number="1", section_title="t", text="x",
        ocr_confidence=0.85, ocr_method="tesseract",
    )
    assert s.ocr_confidence == 0.85
    assert s.ocr_method == "tesseract"


# ---------------------------------------------------------------------------
# 2. _ocr_single_page returns (text, confidence) when return_confidence=True
# ---------------------------------------------------------------------------

def test_ocr_single_page_returns_confidence_tuple(parser_module, monkeypatch):
    # Stub pdf2image + pytesseract
    fake_image = MagicMock()
    fake_pdf2image = types.ModuleType("pdf2image")
    fake_pdf2image.convert_from_path = MagicMock(return_value=[fake_image])

    fake_tesseract = types.ModuleType("pytesseract")
    fake_tesseract.Output = types.SimpleNamespace(DICT="dict")
    fake_tesseract.image_to_data = MagicMock(return_value={
        "text": ["Hello", "World", "more", "words"],
        "conf": [80, 90, 70, 85],
    })
    fake_tesseract.image_to_string = MagicMock(return_value="Hello World")

    monkeypatch.setitem(sys.modules, "pdf2image", fake_pdf2image)
    monkeypatch.setitem(sys.modules, "pytesseract", fake_tesseract)
    monkeypatch.setattr(parser_module, "_preprocess_image_for_ocr",
                        lambda img: img)
    monkeypatch.setattr(parser_module, "_postprocess_ocr_text",
                        lambda t: t)

    text, conf = parser_module._ocr_single_page(
        "/tmp/fake.pdf", 1, return_confidence=True,
    )
    assert text == "Hello World more words"
    # mean of [80, 90, 70, 85] / 100 = 0.8125
    assert abs(conf - 0.8125) < 1e-6


def test_ocr_single_page_legacy_signature_returns_string(parser_module, monkeypatch):
    fake_image = MagicMock()
    fake_pdf2image = types.ModuleType("pdf2image")
    fake_pdf2image.convert_from_path = MagicMock(return_value=[fake_image])

    fake_tesseract = types.ModuleType("pytesseract")
    fake_tesseract.image_to_string = MagicMock(return_value="legacy text")

    monkeypatch.setitem(sys.modules, "pdf2image", fake_pdf2image)
    monkeypatch.setitem(sys.modules, "pytesseract", fake_tesseract)
    monkeypatch.setattr(parser_module, "_preprocess_image_for_ocr",
                        lambda img: img)
    monkeypatch.setattr(parser_module, "_postprocess_ocr_text",
                        lambda t: t)

    out = parser_module._ocr_single_page("/tmp/fake.pdf", 1)
    assert out == "legacy text"
    assert isinstance(out, str)


# ---------------------------------------------------------------------------
# 3. _ocr_single_page returns (empty, 0.0) on missing imports + on errors
# ---------------------------------------------------------------------------

def test_ocr_single_page_returns_empty_tuple_on_import_error(parser_module, monkeypatch):
    # Force ImportError by removing pdf2image
    monkeypatch.setitem(sys.modules, "pdf2image",
                        types.ModuleType("missing"))
    # ImportError happens at "import pytesseract" inside the function
    monkeypatch.delitem(sys.modules, "pytesseract", raising=False)

    # Suppress real import by injecting a broken pytesseract
    class _ImportTrap:
        def __getattr__(self, name):
            raise ImportError("not really installed")
    monkeypatch.setitem(sys.modules, "pdf2image", _ImportTrap())

    text, conf = parser_module._ocr_single_page(
        "/tmp/fake.pdf", 1, return_confidence=True,
    )
    assert text == ""
    assert conf == 0.0


# ---------------------------------------------------------------------------
# 4. _parse_with_fitz tags text-layer pages as fitz_native with NULL conf
# ---------------------------------------------------------------------------

def test_parse_with_fitz_tags_text_layer_pages_as_fitz_native(parser_module, monkeypatch):
    fake_pages = []
    for _i in range(3):
        page = MagicMock()
        page.get_text = MagicMock(return_value="A" * 200)
        fake_pages.append(page)

    class _FakeDoc:
        metadata = {"title": "T"}
        def __iter__(self): return iter(fake_pages)
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake_pymupdf = types.ModuleType("pymupdf")
    fake_pymupdf.open = MagicMock(return_value=_FakeDoc())
    monkeypatch.setitem(sys.modules, "pymupdf", fake_pymupdf)

    out = parser_module._parse_with_fitz("/tmp/fake.pdf")
    *_, image_pages, method, conf = out

    assert image_pages == []
    for p in [1, 2, 3]:
        assert method[p] == "fitz_native"
        assert conf[p] is None


# ---------------------------------------------------------------------------
# 5. _parse_with_fitz tags tesseract-recovered pages with captured confidence
# ---------------------------------------------------------------------------

def test_parse_with_fitz_tags_recovered_pages_with_tesseract(parser_module, monkeypatch):
    # Page 1 returns substantial text; page 2 returns empty (image page)
    page1 = MagicMock()
    page1.get_text = MagicMock(return_value="P" * 200)
    page2 = MagicMock()
    page2.get_text = MagicMock(return_value="")

    class _FakeDoc:
        metadata = {}
        def __iter__(self): return iter([page1, page2])
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake_pymupdf = types.ModuleType("pymupdf")
    fake_pymupdf.open = MagicMock(return_value=_FakeDoc())
    monkeypatch.setitem(sys.modules, "pymupdf", fake_pymupdf)

    # Stub _ocr_single_page to return text + conf=0.72
    def _fake_ocr(path, page_num, return_confidence=False):
        assert return_confidence is True
        return ("R" * 200, 0.72)
    monkeypatch.setattr(parser_module, "_ocr_single_page", _fake_ocr)

    out = parser_module._parse_with_fitz("/tmp/fake.pdf", apply_ocr_fallback=True)
    *_, image_pages, method, conf = out

    # Page 1 = text layer
    assert method[1] == "fitz_native"
    assert conf[1] is None
    # Page 2 = recovered by tesseract
    assert method[2] == "tesseract"
    assert conf[2] == 0.72


# ---------------------------------------------------------------------------
# 6. _parse_with_fitz apply_ocr_fallback=False keeps page in image_pages
# ---------------------------------------------------------------------------

def test_parse_with_fitz_no_fallback_leaves_image_pages(parser_module, monkeypatch):
    page1 = MagicMock()
    page1.get_text = MagicMock(return_value="")

    class _FakeDoc:
        metadata = {}
        def __iter__(self): return iter([page1])
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake_pymupdf = types.ModuleType("pymupdf")
    fake_pymupdf.open = MagicMock(return_value=_FakeDoc())
    monkeypatch.setitem(sys.modules, "pymupdf", fake_pymupdf)

    out = parser_module._parse_with_fitz(
        "/tmp/fake.pdf", apply_ocr_fallback=False,
    )
    *_, image_pages, method, conf = out

    assert image_pages == [1]
    # Page not in either map (didn't fall into text-layer NOR recovery)
    assert 1 not in method
    assert 1 not in conf


# ---------------------------------------------------------------------------
# 7. _assign_ocr_metadata picks first-page-method-wins
# ---------------------------------------------------------------------------

def test_assign_ocr_metadata_first_page_method_wins(parser_module):
    sections = [
        parser_module.ReportSection(
            section_number="1", section_title="Mix",
            text="x", page_first=1, page_last=3,
        ),
    ]
    per_page_method = {1: "fitz_native", 2: "tesseract", 3: "docling_rapidocr"}
    per_page_confidence = {1: None, 2: 0.70, 3: 0.90}

    parser_module._assign_ocr_metadata(sections, per_page_method, per_page_confidence)

    assert sections[0].ocr_method == "fitz_native"
    # Min over [0.70, 0.90] = 0.70 (None pages are skipped)
    assert sections[0].ocr_confidence == 0.70


# ---------------------------------------------------------------------------
# 8. _assign_ocr_metadata: all-None confidences leave ocr_confidence as None
# ---------------------------------------------------------------------------

def test_assign_ocr_metadata_all_none_confidence(parser_module):
    sections = [
        parser_module.ReportSection(
            section_number=None, section_title="t",
            text="y", page_first=1, page_last=2,
        ),
    ]
    parser_module._assign_ocr_metadata(
        sections,
        {1: "fitz_native", 2: "fitz_native"},
        {1: None, 2: None},
    )
    assert sections[0].ocr_method == "fitz_native"
    assert sections[0].ocr_confidence is None


# ---------------------------------------------------------------------------
# 9. _assign_ocr_metadata: min confidence across pages
# ---------------------------------------------------------------------------

def test_assign_ocr_metadata_min_confidence_wins(parser_module):
    sections = [
        parser_module.ReportSection(
            section_number=None, section_title="t",
            text="y", page_first=10, page_last=12,
        ),
    ]
    parser_module._assign_ocr_metadata(
        sections,
        {10: "tesseract", 11: "tesseract", 12: "tesseract"},
        {10: 0.95, 11: 0.40, 12: 0.85},
    )
    # min([0.95, 0.40, 0.85]) = 0.40
    assert sections[0].ocr_confidence == 0.40


# ---------------------------------------------------------------------------
# 10. _assign_ocr_metadata: preamble (page_first=None) is left untouched
# ---------------------------------------------------------------------------

def test_assign_ocr_metadata_skips_preamble(parser_module):
    sections = [
        parser_module.ReportSection(
            section_number=None, section_title="Preamble",
            text="preamble text", page_first=None, page_last=None,
        ),
    ]
    parser_module._assign_ocr_metadata(
        sections,
        {1: "fitz_native"},
        {1: None},
    )
    assert sections[0].ocr_method is None
    assert sections[0].ocr_confidence is None


# ---------------------------------------------------------------------------
# 11. _assign_ocr_metadata: empty maps → no-op
# ---------------------------------------------------------------------------

def test_assign_ocr_metadata_empty_maps_noop(parser_module):
    sections = [
        parser_module.ReportSection(
            section_number=None, section_title="t",
            text="y", page_first=1, page_last=1,
        ),
    ]
    parser_module._assign_ocr_metadata(sections, {}, {})
    assert sections[0].ocr_method is None
    assert sections[0].ocr_confidence is None


# ---------------------------------------------------------------------------
# 12. _assign_ocr_metadata: missing page in maps → first-page-wins skips ahead
# ---------------------------------------------------------------------------

def test_assign_ocr_metadata_missing_page_falls_through(parser_module):
    sections = [
        parser_module.ReportSection(
            section_number=None, section_title="t",
            text="y", page_first=5, page_last=7,
        ),
    ]
    # Page 5 is missing from maps; page 6 has tesseract
    parser_module._assign_ocr_metadata(
        sections,
        {6: "tesseract", 7: "tesseract"},
        {6: 0.85, 7: 0.65},
    )
    # First non-missing page wins for method
    assert sections[0].ocr_method == "tesseract"
    # Min over present confidences = 0.65
    assert sections[0].ocr_confidence == 0.65


# ---------------------------------------------------------------------------
# 13. tesseract mean_conf == 0 when no positive-confidence words
# ---------------------------------------------------------------------------

def test_ocr_single_page_returns_zero_conf_for_no_positive_words(
    parser_module, monkeypatch,
):
    fake_image = MagicMock()
    fake_pdf2image = types.ModuleType("pdf2image")
    fake_pdf2image.convert_from_path = MagicMock(return_value=[fake_image])

    fake_tesseract = types.ModuleType("pytesseract")
    fake_tesseract.Output = types.SimpleNamespace(DICT="dict")
    # All -1 confidences (no detection)
    fake_tesseract.image_to_data = MagicMock(return_value={
        "text": ["", "", ""],
        "conf": [-1, -1, -1],
    })
    fake_tesseract.image_to_string = MagicMock(return_value="")

    monkeypatch.setitem(sys.modules, "pdf2image", fake_pdf2image)
    monkeypatch.setitem(sys.modules, "pytesseract", fake_tesseract)
    monkeypatch.setattr(parser_module, "_preprocess_image_for_ocr",
                        lambda img: img)
    monkeypatch.setattr(parser_module, "_postprocess_ocr_text",
                        lambda t: t)

    text, conf = parser_module._ocr_single_page(
        "/tmp/fake.pdf", 1, return_confidence=True,
    )
    assert text == ""
    assert conf == 0.0


# ---------------------------------------------------------------------------
# 14. tesseract mean_conf clamped to [0, 1]
# ---------------------------------------------------------------------------

def test_ocr_single_page_clamps_confidence(parser_module, monkeypatch):
    fake_image = MagicMock()
    fake_pdf2image = types.ModuleType("pdf2image")
    fake_pdf2image.convert_from_path = MagicMock(return_value=[fake_image])

    fake_tesseract = types.ModuleType("pytesseract")
    fake_tesseract.Output = types.SimpleNamespace(DICT="dict")
    # Hypothetical out-of-range from tesseract (defensive test)
    fake_tesseract.image_to_data = MagicMock(return_value={
        "text": ["alpha"],
        "conf": [150],  # > 100, defensive
    })
    fake_tesseract.image_to_string = MagicMock(return_value="alpha")

    monkeypatch.setitem(sys.modules, "pdf2image", fake_pdf2image)
    monkeypatch.setitem(sys.modules, "pytesseract", fake_tesseract)
    monkeypatch.setattr(parser_module, "_preprocess_image_for_ocr",
                        lambda img: img)
    monkeypatch.setattr(parser_module, "_postprocess_ocr_text",
                        lambda t: t)

    text, conf = parser_module._ocr_single_page(
        "/tmp/fake.pdf", 1, return_confidence=True,
    )
    assert 0.0 <= conf <= 1.0
    assert conf == 1.0
