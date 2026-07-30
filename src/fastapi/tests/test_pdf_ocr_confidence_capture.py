"""Phase 3 (2026-05-22) — OCR confidence + method capture in the parser.

These tests verify that:
  - ReportSection has the two new optional fields with default None.
  - _ocr_single_page(return_confidence=True) returns (text, mean_conf).
  - _parse_with_fitz populates per_page_method + per_page_confidence
    correctly for text-layer pages (fitz_native, None confidence) and
    tesseract-recovered pages (tesseract, captured confidence).
  - _assign_ocr_metadata applies first-page-method-wins and min-
    confidence-across-spanned-pages rules.

No real docling / pdfplumber / pypdfium2 installs needed. All parsers
are stubbed.

Run with:
    pytest src/dagster/tests/test_pdf_ocr_confidence_capture.py -v
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

# Stub boto3/botocore ONLY when genuinely absent (running pytest on a dev host
# rather than in the container, where they are installed for real). This used to
# be an unconditional sys.modules.setdefault, which left a MagicMock in
# sys.modules that unrelated modules tripped over: aioboto3 does
# `import boto3.session`, and that raises "'boto3' is not a package" against a
# mock. Once this file moved into the FastAPI suite it sorted ahead of
# test_pg_partman_maintenance / test_section11_* / test_workspace_export_restore
# and broke their collection.
try:  # pragma: no cover - environment probe
    import boto3  # noqa: F401
    import botocore.config  # noqa: F401
except ImportError:  # pragma: no cover - dev host without the AWS SDKs
    sys.modules.setdefault("boto3", MagicMock())
    sys.modules.setdefault("botocore", MagicMock())
    sys.modules.setdefault("botocore.config", MagicMock())


def _install_fake_pypdfium2(monkeypatch, page_texts, *, title=None):
    """Install a fake ``pypdfium2`` whose ``PdfDocument`` yields ``page_texts``
    (one string per page via ``get_textpage().get_text_bounded()``).

    Mirrors the engine swap in ``_parse_with_fitz`` (PyMuPDF → pypdfium2 after
    PyMuPDF was removed for its AGPL license). The lazy ``import pypdfium2``
    inside the parser picks this up from ``sys.modules`` at call time.
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


@pytest.fixture
def parser_module():
    import importlib

    from app.services.ingest import pdf_report

    importlib.reload(pdf_report)
    return pdf_report


# ---------------------------------------------------------------------------
# 1. ReportSection has the two new fields with default None
# ---------------------------------------------------------------------------


def test_report_section_has_ocr_fields_default_none(parser_module):
    s = parser_module.ReportSection(
        section_number="1",
        section_title="Summary",
        text="hello",
    )
    assert hasattr(s, "ocr_confidence")
    assert hasattr(s, "ocr_method")
    assert s.ocr_confidence is None
    assert s.ocr_method is None


def test_report_section_accepts_ocr_fields(parser_module):
    s = parser_module.ReportSection(
        section_number="1",
        section_title="t",
        text="x",
        ocr_confidence=0.85,
        ocr_method="tesseract",
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
    fake_tesseract.image_to_data = MagicMock(
        return_value={
            "text": ["Hello", "World", "more", "words"],
            "conf": [80, 90, 70, 85],
        }
    )
    fake_tesseract.image_to_string = MagicMock(return_value="Hello World")

    monkeypatch.setitem(sys.modules, "pdf2image", fake_pdf2image)
    monkeypatch.setitem(sys.modules, "pytesseract", fake_tesseract)
    monkeypatch.setattr(parser_module, "_preprocess_image_for_ocr", lambda img: img)
    monkeypatch.setattr(parser_module, "_postprocess_ocr_text", lambda t: t)

    text, conf = parser_module._ocr_single_page(
        "/tmp/fake.pdf",
        1,
        return_confidence=True,
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
    monkeypatch.setattr(parser_module, "_preprocess_image_for_ocr", lambda img: img)
    monkeypatch.setattr(parser_module, "_postprocess_ocr_text", lambda t: t)

    out = parser_module._ocr_single_page("/tmp/fake.pdf", 1)
    assert out == "legacy text"
    assert isinstance(out, str)


# ---------------------------------------------------------------------------
# 3. _ocr_single_page returns (empty, 0.0) on missing imports + on errors
# ---------------------------------------------------------------------------


def test_ocr_single_page_returns_empty_tuple_on_import_error(parser_module, monkeypatch):
    # Force ImportError by removing pdf2image
    monkeypatch.setitem(sys.modules, "pdf2image", types.ModuleType("missing"))
    # ImportError happens at "import pytesseract" inside the function
    monkeypatch.delitem(sys.modules, "pytesseract", raising=False)

    # Suppress real import by injecting a broken pytesseract
    class _ImportTrap:
        def __getattr__(self, name):
            raise ImportError("not really installed")

    monkeypatch.setitem(sys.modules, "pdf2image", _ImportTrap())

    text, conf = parser_module._ocr_single_page(
        "/tmp/fake.pdf",
        1,
        return_confidence=True,
    )
    assert text == ""
    assert conf == 0.0


# ---------------------------------------------------------------------------
# 4. _parse_with_fitz tags text-layer pages as fitz_native with NULL conf
# ---------------------------------------------------------------------------


def test_parse_with_fitz_tags_text_layer_pages_as_fitz_native(parser_module, monkeypatch):
    _install_fake_pypdfium2(monkeypatch, ["A" * 200] * 3, title="T")

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
    _install_fake_pypdfium2(monkeypatch, ["P" * 200, ""])

    # Stub _ocr_single_page to return text + conf=0.72
    def _fake_ocr(
        path,
        page_num,
        return_confidence=False,
        return_assessment=False,
    ):
        assert return_confidence is True
        assert return_assessment is True
        assessment = {
            "tier": "mandatory_review",
            "routing_decision": "review_required",
            "reasons": ["routing_thresholds_not_calibrated"],
            "thresholds_calibrated": False,
            "signals": {},
        }
        if return_assessment:
            return "R" * 200, 0.72, assessment
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
    _install_fake_pypdfium2(monkeypatch, [""])

    out = parser_module._parse_with_fitz(
        "/tmp/fake.pdf",
        apply_ocr_fallback=False,
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
            section_number="1",
            section_title="Mix",
            text="x",
            page_first=1,
            page_last=3,
        ),
    ]
    per_page_method = {1: "fitz_native", 2: "tesseract", 3: "document_intelligence"}
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
            section_number=None,
            section_title="t",
            text="y",
            page_first=1,
            page_last=2,
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
            section_number=None,
            section_title="t",
            text="y",
            page_first=10,
            page_last=12,
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
            section_number=None,
            section_title="Preamble",
            text="preamble text",
            page_first=None,
            page_last=None,
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
            section_number=None,
            section_title="t",
            text="y",
            page_first=1,
            page_last=1,
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
            section_number=None,
            section_title="t",
            text="y",
            page_first=5,
            page_last=7,
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
    parser_module,
    monkeypatch,
):
    fake_image = MagicMock()
    fake_pdf2image = types.ModuleType("pdf2image")
    fake_pdf2image.convert_from_path = MagicMock(return_value=[fake_image])

    fake_tesseract = types.ModuleType("pytesseract")
    fake_tesseract.Output = types.SimpleNamespace(DICT="dict")
    # All -1 confidences (no detection)
    fake_tesseract.image_to_data = MagicMock(
        return_value={
            "text": ["", "", ""],
            "conf": [-1, -1, -1],
        }
    )
    fake_tesseract.image_to_string = MagicMock(return_value="")

    monkeypatch.setitem(sys.modules, "pdf2image", fake_pdf2image)
    monkeypatch.setitem(sys.modules, "pytesseract", fake_tesseract)
    monkeypatch.setattr(parser_module, "_preprocess_image_for_ocr", lambda img: img)
    monkeypatch.setattr(parser_module, "_postprocess_ocr_text", lambda t: t)

    text, conf = parser_module._ocr_single_page(
        "/tmp/fake.pdf",
        1,
        return_confidence=True,
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
    fake_tesseract.image_to_data = MagicMock(
        return_value={
            "text": ["alpha"],
            "conf": [150],  # > 100, defensive
        }
    )
    fake_tesseract.image_to_string = MagicMock(return_value="alpha")

    monkeypatch.setitem(sys.modules, "pdf2image", fake_pdf2image)
    monkeypatch.setitem(sys.modules, "pytesseract", fake_tesseract)
    monkeypatch.setattr(parser_module, "_preprocess_image_for_ocr", lambda img: img)
    monkeypatch.setattr(parser_module, "_postprocess_ocr_text", lambda t: t)

    text, conf = parser_module._ocr_single_page(
        "/tmp/fake.pdf",
        1,
        return_confidence=True,
    )
    assert 0.0 <= conf <= 1.0
    assert conf == 1.0


def test_tiled_document_intelligence_reconstructs_oversized_page(
    parser_module,
    monkeypatch,
):
    from PIL import Image

    from app.services.ingest import document_intelligence_client as di

    fake_pdf2image = types.ModuleType("pdf2image")
    fake_pdf2image.convert_from_path = MagicMock(return_value=[Image.new("L", (100, 9_500))])
    monkeypatch.setitem(sys.modules, "pdf2image", fake_pdf2image)

    results = iter(
        [
            di.PageOcrResult(
                text="Top Seam",
                mean_confidence=0.90,
                words=(
                    di.OcrWord("Top", 0.90, (5, 5, 25, 5, 25, 20, 5, 20)),
                    di.OcrWord(
                        "Seam",
                        0.80,
                        (5, 8_950, 35, 8_950, 35, 8_970, 5, 8_970),
                    ),
                ),
                detected_region_count=2,
            ),
            di.PageOcrResult(
                text="Seam Bottom",
                mean_confidence=0.95,
                words=(
                    di.OcrWord("Seam", 0.95, (5, 130, 35, 130, 35, 150, 5, 150)),
                    di.OcrWord("Bottom", 0.95, (5, 500, 45, 500, 45, 520, 5, 520)),
                ),
                detected_region_count=2,
            ),
        ]
    )
    monkeypatch.setattr(di, "ocr_image_sync", lambda _body: next(results))

    result, assessment = parser_module._ocr_tiled_pdf_page("/tmp/fake.pdf", 1)

    assert result.text == "Top Seam Bottom"
    assert len(result.words) == 3
    assert next(word for word in result.words if word.text == "Seam").confidence == 0.95
    assert assessment["signals"]["seam_duplicate_ratio"] == 0.25


def test_tiled_document_intelligence_rejects_partial_tile_failure(
    parser_module,
    monkeypatch,
):
    from PIL import Image

    from app.services.ingest import document_intelligence_client as di

    fake_pdf2image = types.ModuleType("pdf2image")
    fake_pdf2image.convert_from_path = MagicMock(return_value=[Image.new("L", (100, 9_500))])
    monkeypatch.setitem(sys.modules, "pdf2image", fake_pdf2image)

    results = iter(
        [
            di.PageOcrResult(
                text="Top",
                mean_confidence=0.9,
                words=(di.OcrWord("Top", 0.9, (5, 5, 25, 5, 25, 20, 5, 20)),),
                detected_region_count=1,
            ),
            di.PageOcrResult(
                "",
                0.0,
                request_succeeded=False,
                error="service unavailable",
            ),
        ]
    )
    monkeypatch.setattr(di, "ocr_image_sync", lambda _body: next(results))

    with pytest.raises(RuntimeError, match="tile r0001-c0000 failed"):
        parser_module._ocr_tiled_pdf_page("/tmp/fake.pdf", 1)


def test_tiled_document_intelligence_rejects_unmappable_words(
    parser_module,
    monkeypatch,
):
    from PIL import Image

    from app.services.ingest import document_intelligence_client as di

    fake_pdf2image = types.ModuleType("pdf2image")
    fake_pdf2image.convert_from_path = MagicMock(return_value=[Image.new("L", (100, 100))])
    monkeypatch.setitem(sys.modules, "pdf2image", fake_pdf2image)
    monkeypatch.setattr(
        di,
        "ocr_image_sync",
        lambda _body: di.PageOcrResult(
            text="Unmapped",
            mean_confidence=0.9,
            words=(di.OcrWord("Unmapped", 0.9, ()),),
            detected_region_count=1,
        ),
    )

    with pytest.raises(RuntimeError, match="without complete word polygons"):
        parser_module._ocr_tiled_pdf_page("/tmp/fake.pdf", 1)


def test_full_document_ocr_reports_actual_mixed_engine_provenance(
    parser_module,
    monkeypatch,
):
    fake_pdf2image = types.ModuleType("pdf2image")
    fake_pdf2image.pdfinfo_from_path = MagicMock(return_value={"Pages": 2})
    monkeypatch.setitem(sys.modules, "pdf2image", fake_pdf2image)

    page_results = iter(
        [
            (
                "Azure page",
                0.9,
                {
                    "tier": "mandatory_review",
                    "routing_decision": "review_required",
                    "reasons": ["routing_thresholds_not_calibrated"],
                    "thresholds_calibrated": False,
                    "signals": {"mean_confidence": 0.9},
                    "ocr_method": "document_intelligence",
                },
            ),
            (
                "Tesseract fallback page",
                0.7,
                {
                    "tier": "mandatory_review",
                    "routing_decision": "review_required",
                    "reasons": ["routing_thresholds_not_calibrated"],
                    "thresholds_calibrated": False,
                    "signals": {"mean_confidence": 0.7},
                    "ocr_method": "tesseract",
                },
            ),
        ]
    )
    monkeypatch.setattr(
        parser_module,
        "_ocr_single_page",
        lambda *_args, **_kwargs: next(page_results),
    )
    monkeypatch.setattr(parser_module, "_postprocess_ocr_text", lambda text: text)

    result = parser_module._attempt_ocr_document_intelligence("/tmp/fake.pdf")

    assert result.parser_used == "ocr_mixed"
    assert result.text == "Azure page\n\nTesseract fallback page"
    assert [page.page_number for page in result.pages] == [1, 2]
