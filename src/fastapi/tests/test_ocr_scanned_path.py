"""§04p scanned path behaviour tests (master-plan §3 Step 4, doc-phase 52).

Asserts:
- render_page produces PNG bytes of reasonable size
- render_page raises on invalid page / scale / path
- parse_scanned produces >0 passages with confidences in [0, 1] on a
  synthetic scanned PDF (PLS-2024 rasterized via pypdfium2)
- parse_scanned respects the `pages` slice
- parse_scanned settings override flows through

Fixture: a synthetic scanned PDF is built in-test by rasterizing the
first 2 pages of the committed PLS-2024 native fixture. No additional
binary committed. PaddleOCR cold-load makes the first scanned test
take ~10-15 sec; subsequent tests in the same process are warm.
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path

import pytest


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ocr"
PLS_2024 = FIXTURE_DIR / "PLS-2024-Technical-Report.pdf"

# Keep this small — each page is ~6 sec of CPU OCR.
SCANNED_PAGES_FOR_TEST = 2


@pytest.fixture(scope="module")
def native_pdf_path() -> Path:
    if not PLS_2024.exists():
        pytest.skip(f"fixture not found: {PLS_2024}")
    return PLS_2024


@pytest.fixture(scope="module")
def synthetic_scanned_pdf(tmp_path_factory: pytest.TempPathFactory, native_pdf_path: Path) -> Path:
    """Build a small image-only PDF from the first N pages of the
    native fixture. Cached at module scope so multiple parse_scanned
    tests can share one synthesized fixture (and one PaddleOCR
    cold-load).
    """
    import pypdfium2 as pdfium
    from PIL import Image

    out_path = tmp_path_factory.mktemp("scanned_fixture") / "scanned_pls_2024.pdf"
    src = pdfium.PdfDocument(str(native_pdf_path))
    try:
        page_count = min(SCANNED_PAGES_FOR_TEST, len(src))
        images: list[Image.Image] = []
        for page_idx in range(page_count):
            bitmap = src[page_idx].render(scale=2.0)
            images.append(bitmap.to_pil().convert("RGB"))
        buf = io.BytesIO()
        images[0].save(buf, format="PDF", save_all=True, append_images=images[1:])
        out_path.write_bytes(buf.getvalue())
        return out_path
    finally:
        try:
            src.close()
        except Exception:
            pass


# ----- render_page -----

def test_render_page_returns_png_bytes(native_pdf_path: Path) -> None:
    from app.ocr.render import render_page

    png = asyncio.run(render_page(native_pdf_path, 0))

    assert isinstance(png, bytes)
    assert len(png) > 100  # at least non-trivial size
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # PNG magic bytes


def test_render_page_invalid_page_raises(native_pdf_path: Path) -> None:
    from app.ocr.render import render_page

    with pytest.raises(IndexError):
        asyncio.run(render_page(native_pdf_path, 9999))


def test_render_page_invalid_scale_raises(native_pdf_path: Path) -> None:
    from app.ocr.render import render_page

    with pytest.raises(ValueError):
        asyncio.run(render_page(native_pdf_path, 0, scale=0))


def test_render_page_missing_file_raises(tmp_path: Path) -> None:
    from app.ocr.render import render_page

    with pytest.raises(FileNotFoundError):
        asyncio.run(render_page(tmp_path / "missing.pdf", 0))


# ----- parse_scanned -----

def test_parse_scanned_produces_passages_with_confidence(
    synthetic_scanned_pdf: Path,
) -> None:
    from app.ocr.parse_scanned import parse_scanned

    result = asyncio.run(parse_scanned(synthetic_scanned_pdf))

    assert result["parser_used"] == "scanned_paddleocr"
    assert result["page_count"] == SCANNED_PAGES_FOR_TEST
    assert len(result["per_page_ocr_confidence"]) == SCANNED_PAGES_FOR_TEST
    assert len(result["per_page_text_line_counts"]) == SCANNED_PAGES_FOR_TEST

    # At least one page should produce >0 OCR'd lines on a real fixture.
    total_lines = sum(result["per_page_text_line_counts"])
    assert total_lines > 0, (
        "PaddleOCR should produce >0 lines on rasterized native PDF"
    )

    # Confidences in [0, 1]
    for conf in result["per_page_ocr_confidence"]:
        assert 0.0 <= conf <= 1.0

    # Passages have valid bboxes + non-empty text
    for passage in result["passages"]:
        assert len(passage["bbox"]) == 4
        assert passage["source_method"] == "paddleocr_pp_ocrv5"
        assert passage["text_content"].strip() != ""
        assert 0.0 <= passage["extraction_confidence"] <= 1.0


def test_parse_scanned_pages_slice(synthetic_scanned_pdf: Path) -> None:
    from app.ocr.parse_scanned import parse_scanned

    result = asyncio.run(parse_scanned(synthetic_scanned_pdf, pages=[0]))

    assert result["page_count"] == 1
    assert all(p["page"] == 0 for p in result["passages"])


def test_parse_scanned_settings_override_flows_through(
    synthetic_scanned_pdf: Path,
) -> None:
    from app.ocr.parse_scanned import parse_scanned

    result = asyncio.run(parse_scanned(
        synthetic_scanned_pdf,
        pages=[0],
        settings={"render_scale": 1.5, "lang": "en"},
    ))

    assert result["settings_used"]["render_scale"] == 1.5
    assert result["settings_used"]["lang"] == "en"
    # use_angle_cls retains default since override didn't touch it
    assert result["settings_used"]["use_angle_cls"] is True
