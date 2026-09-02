"""Pages are rendered under the Parse pixel cap — downscaled, never tiled."""

from __future__ import annotations

import io
import logging

import pytest

from app.services.ingest import cohere_parse_client as cpc
from app.services.ingest.page_image import (
    EMBED_V4_MAX_PIXELS,
    dpi_for_page,
    render_page_png,
)

LETTER = (612.0, 792.0)
A0 = (2384.0, 3370.0)


def _pdf_with_pages(sizes) -> bytes:
    pdfium = pytest.importorskip("pypdfium2")
    doc = pdfium.PdfDocument.new()
    for w, h in sizes:
        doc.new_page(w, h)
    buf = io.BytesIO()
    doc.save(buf)
    doc.close()
    return buf.getvalue()


class TestDpiForPage:
    def test_default_cap_is_unchanged_for_embedding_callers(self) -> None:
        assert dpi_for_page(*LETTER) == dpi_for_page(
            *LETTER, max_pixels=EMBED_V4_MAX_PIXELS
        )

    def test_a_larger_cap_allows_a_higher_dpi(self) -> None:
        assert dpi_for_page(*LETTER, max_pixels=8_000_000) > dpi_for_page(*LETTER)

    def test_an_a0_sheet_lands_under_the_cap(self) -> None:
        dpi = dpi_for_page(*A0, max_pixels=4_000_000)
        px = (A0[0] / 72 * dpi) * (A0[1] / 72 * dpi)

        assert px <= 4_000_000
        assert dpi < 100  # this is the documented regression: downscaled, not tiled


class TestRenderPagePng:
    def test_render_honours_a_custom_cap(self, tmp_path) -> None:
        pdf = _pdf_with_pages([LETTER])

        png, w, h, dpi = render_page_png(pdf, 1, max_pixels=1_000_000)

        assert w * h <= 1_000_000
        assert png.startswith(b"\x89PNG")


class TestClientRendering:
    def test_pages_render_under_the_env_cap_and_warn_when_downscaled(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        from PIL import Image

        path = tmp_path / "sheets.pdf"
        path.write_bytes(_pdf_with_pages([LETTER, A0]))
        monkeypatch.setenv("COHERE_PARSE_MAX_PIXELS", "3000000")

        with caplog.at_level(logging.WARNING, logger="georag.ingest.cohere_parse"):
            rendered = cpc._render_pages(str(path), [1, 2, 9])

        assert sorted(rendered) == [1, 2]
        for png in rendered.values():
            with Image.open(io.BytesIO(png)) as img:
                assert img.width * img.height <= 3_000_000
        downscaled = [r for r in caplog.records if "downscaled" in r.getMessage()]
        assert len(downscaled) == 1 and "page 2" in downscaled[0].getMessage()
        out_of_range = [r for r in caplog.records if "outside 1..2" in r.getMessage()]
        assert len(out_of_range) == 1

    def test_max_pixels_has_a_floor(self, monkeypatch) -> None:
        monkeypatch.setenv("COHERE_PARSE_MAX_PIXELS", "10")
        assert cpc.max_pixels() == 100_000
        monkeypatch.setenv("COHERE_PARSE_MAX_PIXELS", "not-a-number")
        assert cpc.max_pixels() == 4_000_000
