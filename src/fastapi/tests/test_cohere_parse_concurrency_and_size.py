"""Cohere Parse (Cohere's own API): in-flight cap, byte guard, throttle marker.

VEN-11 (2026-09-29): the group pass ran min(PDF_OCR_PAGE_CONCURRENCY, groups)
outer threads and each group opened its own PDF_OCR_PAGE_CONCURRENCY pool --
up to 16 concurrent Parse requests and resident PNGs per document at the
defaults, against a docstring promising 4. A throttle that exhausted its
retries logged a WARNING and the page quietly went to tesseract.

VEN-10: the byte size of a rendered page was never bounded or recorded; the
vendor limit is unprobed, so the cap is off by default and only the
mechanism is pinned here.

Nothing here reaches api.cohere.com: rendering and the HTTP seam are fakes.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from PIL import Image

from app.services.ingest import cohere_parse_client as cpc
from app.services.ingest.ocr_types import PageOcrResult


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OCR_ENGINE", "cohere_parse")
    monkeypatch.setenv("COHERE_API_KEY", "test-only-not-a-real-cohere-key")
    monkeypatch.delenv("BEDROCK_PARSE_MODEL_ID", raising=False)
    monkeypatch.delenv("COHERE_PARSE_MAX_IMAGE_BYTES", raising=False)
    for name in ("AZURE_FOUNDRY_ENDPOINT", "AZURE_FOUNDRY_API_KEY", "AZURE_FOUNDRY_PARSE_DEPLOYMENT"):
        monkeypatch.delenv(name, raising=False)


def test_nested_group_pools_never_exceed_the_page_concurrency(monkeypatch) -> None:
    """Four groups x their own worker pools, against one process-wide cap."""
    monkeypatch.setenv("PDF_OCR_PAGE_CONCURRENCY", "3")
    in_flight = 0
    peak = 0
    lock = threading.Lock()

    def fake_render(_path: str, _page: int) -> bytes:
        nonlocal in_flight, peak
        with lock:
            in_flight += 1
            peak = max(peak, in_flight)
        return b"png"

    def fake_parse(_png: bytes, *, log_page: int | None) -> PageOcrResult:
        nonlocal in_flight
        time.sleep(0.01)
        with lock:
            in_flight -= 1
        return PageOcrResult(f"p{log_page}", 0.0, confidence_reported=False)

    monkeypatch.setattr(cpc, "_page_count", lambda _p: 32)
    monkeypatch.setattr(cpc, "_render_page", fake_render)
    monkeypatch.setattr(cpc, "_parse_png", fake_parse)

    groups = [list(range(start, start + 8)) for start in (1, 9, 17, 25)]
    with ThreadPoolExecutor(max_workers=4) as outer:
        results = list(outer.map(lambda g: cpc.ocr_page_block_sync("doc.pdf", g), groups))

    assert sum(len(r) for r in results) == 32
    assert peak <= 3, f"{peak} pages in flight at once; the cap is 3"


def test_the_single_page_path_takes_a_slot_too(monkeypatch) -> None:
    monkeypatch.setenv("PDF_OCR_PAGE_CONCURRENCY", "1")
    slots = cpc._page_slots()
    held: list[bool] = []

    def fake_render(_path: str, _page: int) -> bytes:
        # With the one slot held, a non-blocking acquire must fail.
        got = slots.acquire(blocking=False)
        if got:
            slots.release()
        held.append(not got)
        return b"png"

    monkeypatch.setattr(cpc, "_render_page", fake_render)
    monkeypatch.setattr(
        cpc, "_parse_png", lambda _png, *, log_page: PageOcrResult("x", 0.0, confidence_reported=False)
    )
    cpc.ocr_page_sync("doc.pdf", 1)
    assert held == [True]


def test_an_exhausted_throttle_logs_its_own_marker(monkeypatch, caplog) -> None:
    def throttled(_model, _body):
        raise cpc.CohereParseHttpError(429, "rate limited")

    monkeypatch.setattr(cpc, "_invoke", throttled)
    with caplog.at_level(logging.WARNING, logger="georag.ingest.cohere_parse"):
        result = cpc._parse_png(b"png", log_page=7)

    assert result.request_succeeded is False
    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("COHERE_PARSE_THROTTLED") and "page 7" in m for m in messages)
    assert not any("COHERE_PARSE_REJECTED" in m for m in messages), "a throttle is not a key problem"


def test_a_rejection_names_the_request_size(monkeypatch, caplog) -> None:
    def too_big(_model, _body):
        raise cpc.CohereParseHttpError(413, "payload too large")

    monkeypatch.setattr(cpc, "_invoke", too_big)
    with caplog.at_level(logging.ERROR, logger="georag.ingest.cohere_parse"):
        cpc._parse_png(b"x" * 1234, log_page=2)
    assert any("COHERE_PARSE_REJECTED" in r.getMessage() and "1234 bytes" in r.getMessage() for r in caplog.records)


class TestByteCap:
    @staticmethod
    def _noisy(width: int = 400, height: int = 400) -> Image.Image:
        import os

        return Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))

    def _png(self, image: Image.Image) -> bytes:
        import io

        buf = io.BytesIO()
        image.save(buf, format="PNG", optimize=False)
        return buf.getvalue()

    def test_off_by_default(self) -> None:
        image = self._noisy()
        png = self._png(image)
        assert cpc.max_image_bytes() == 0
        assert cpc._fit_image_bytes(image, png, page_number=1, pdf_path="d.pdf") is png

    def test_an_oversized_render_is_shrunk_under_the_cap(self, monkeypatch, caplog) -> None:
        image = self._noisy()
        png = self._png(image)
        cap = len(png) // 3
        monkeypatch.setenv("COHERE_PARSE_MAX_IMAGE_BYTES", str(cap))

        with caplog.at_level(logging.WARNING, logger="georag.ingest.cohere_parse"):
            out = cpc._fit_image_bytes(image, png, page_number=4, pdf_path="d.pdf")

        assert len(out) <= cap
        assert any("downscaled" in r.getMessage() for r in caplog.records)

    def test_a_render_under_the_cap_is_untouched(self, monkeypatch) -> None:
        image = self._noisy(50, 50)
        png = self._png(image)
        monkeypatch.setenv("COHERE_PARSE_MAX_IMAGE_BYTES", str(len(png) * 2))
        assert cpc._fit_image_bytes(image, png, page_number=1, pdf_path="d.pdf") is png
