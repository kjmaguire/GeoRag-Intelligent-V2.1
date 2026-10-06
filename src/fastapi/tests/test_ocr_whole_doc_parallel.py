"""Whole-document remote OCR runs pages concurrently (2026-08-18).

`_attempt_ocr_cohere_parse` (Document Intelligence until ADR-0019) is the path a fully scanned report takes,
and it was strictly serial: one network round-trip per page, awaited, then the
next. A 300-page scan meant 300 sequential round-trips — the slowest thing in
the ingest pipeline.

Two properties are pinned here, and the second is the one that would corrupt a
document rather than merely slow it:

  1. Pages are OCR'd concurrently.
  2. Results are assembled in PAGE order regardless of completion order. OCR
     latency varies wildly per page, so a fanout that assembled in completion
     order would silently interleave a report's text.
"""

from __future__ import annotations

import random
import sys
import time
import types
from unittest.mock import patch

import pytest

from app.services.ingest import pdf_report


@pytest.fixture
def stub_page_count():
    """Provide pdf2image.pdfinfo_from_path without requiring pdf2image.

    The function under test imports it lazily, and the package is absent from
    some container images (it is a poppler wrapper, only needed on the
    rasterise path). Injecting a stub keeps this test about ordering and
    concurrency rather than about which image it happens to run in.
    """
    def _install(pages: int):
        stub = types.ModuleType("pdf2image")
        stub.pdfinfo_from_path = lambda *_a, **_kw: {"Pages": pages}
        sys.modules["pdf2image"] = stub

    original = sys.modules.get("pdf2image")
    yield _install
    if original is not None:
        sys.modules["pdf2image"] = original
    else:
        sys.modules.pop("pdf2image", None)


def _fake_page_result(page_num: int):
    """Shape of _ocr_single_page(..., return_confidence/assessment/tables)."""
    return (
        f"TEXT-OF-PAGE-{page_num}",
        0.9,
        pdf_report._assess_ocr_result(
            f"TEXT-OF-PAGE-{page_num}", [0.9], detected_region_count=1,
        ),
        [],
    )


class TestWholeDocumentParallelism:
    def test_pages_are_assembled_in_page_order_not_completion_order(self, stub_page_count) -> None:
        """The corruption risk: jittered latency must not reorder the document."""
        pages = 24
        stub_page_count(pages)

        def jittered(path, page_num, **_kw):
            # Later pages finish FIRST — the worst case for a naive fanout.
            time.sleep((pages - page_num) * 0.004)
            return _fake_page_result(page_num)

        with patch.object(pdf_report, "_ocr_single_page", side_effect=jittered), \
             patch.object(pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t):
            result = pdf_report._attempt_ocr_cohere_parse("/nonexistent.pdf")

        assert [p.page_number for p in result.pages] == list(range(1, pages + 1))

        # And the concatenated text must read in page order too.
        positions = [
            result.text.index(f"TEXT-OF-PAGE-{n}") for n in range(1, pages + 1)
        ]
        assert positions == sorted(positions), "document text is out of page order"

    def test_pages_actually_run_concurrently(self, stub_page_count) -> None:
        """Serial would take pages * delay; concurrent takes a fraction."""
        pages = 16
        delay = 0.05
        stub_page_count(pages)

        def slow(path, page_num, **_kw):
            time.sleep(delay)
            return _fake_page_result(page_num)

        with patch.object(pdf_report, "_ocr_single_page", side_effect=slow), \
             patch.object(pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t), \
             patch.dict("os.environ", {"PDF_OCR_PAGE_CONCURRENCY": "8"}):
            started = time.monotonic()
            pdf_report._attempt_ocr_cohere_parse("/nonexistent.pdf")
            elapsed = time.monotonic() - started

        serial_floor = pages * delay
        # Generous bound — proving "much faster than serial", not a precise
        # speedup, so this can't go flaky on a loaded CI box.
        assert elapsed < serial_floor * 0.6, (
            f"took {elapsed:.2f}s against a {serial_floor:.2f}s serial floor — "
            "pages do not appear to be running concurrently"
        )

    def test_one_failing_page_does_not_abort_the_document(self, stub_page_count) -> None:
        """Fail-soft per page: a 300-page scan must survive one bad page."""
        pages = 8
        stub_page_count(pages)

        def flaky(path, page_num, **_kw):
            if page_num == 5:
                raise RuntimeError("transient engine failure")
            return _fake_page_result(page_num)

        with patch.object(pdf_report, "_ocr_single_page", side_effect=flaky), \
             patch.object(pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t):
            result = pdf_report._attempt_ocr_cohere_parse("/nonexistent.pdf")

        # Every page still has a row, in order; the failed one is just empty.
        assert [p.page_number for p in result.pages] == list(range(1, pages + 1))
        assert result.pages[4].text == ""
        assert "TEXT-OF-PAGE-4" in result.text
        assert "TEXT-OF-PAGE-6" in result.text

    def test_every_page_failing_still_raises_for_the_tesseract_fallback(self, stub_page_count) -> None:
        """The caller's except is what routes to tesseract — don't swallow it."""
        stub_page_count(4)

        def always_fails(path, page_num, **_kw):
            raise RuntimeError("foundry down")

        with patch.object(pdf_report, "_ocr_single_page", side_effect=always_fails):
            try:
                pdf_report._attempt_ocr_cohere_parse("/nonexistent.pdf")
            except RuntimeError as exc:
                assert "no text" in str(exc)
            else:
                raise AssertionError("expected RuntimeError so the caller falls back")

    def test_random_jitter_never_reorders(self, stub_page_count) -> None:
        """Repeat under random latency — reordering bugs hide from fixed timings."""
        pages = 20
        stub_page_count(pages)
        rng = random.Random(1234)

        def jittered(path, page_num, **_kw):
            time.sleep(rng.random() * 0.01)
            return _fake_page_result(page_num)

        with patch.object(pdf_report, "_ocr_single_page", side_effect=jittered), \
             patch.object(pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t):
            result = pdf_report._attempt_ocr_cohere_parse("/nonexistent.pdf")

        assert [p.page_number for p in result.pages] == list(range(1, pages + 1))


class TestOcrProgressBeacon:
    """The whole-document scan path never wrote the progress beacon, and the
    mixed path wrote it only after every page had finished, so a scanned report
    showed no movement for the whole multi-hour OCR."""

    def test_a_scanned_document_advances_the_beacon_page_by_page(
        self, stub_page_count, tmp_path, monkeypatch,
    ) -> None:
        import json

        pages = 6
        stub_page_count(pages)
        beacon = tmp_path / "progress.json"
        seen: list[int] = []

        def watching(path, page_num, **_kw):
            if beacon.exists():
                seen.append(json.loads(beacon.read_text())["done"])
            return _fake_page_result(page_num)

        monkeypatch.setattr(pdf_report, "_ACTIVE_PROGRESS_FILE", str(beacon))
        monkeypatch.setattr(pdf_report, "_PROGRESS_TICK_INTERVAL_S", 0.0)
        monkeypatch.setenv("PDF_OCR_PAGE_CONCURRENCY", "1")
        monkeypatch.setenv("OCR_PAGES_PER_BATCH", "1")
        with patch.object(pdf_report, "_ocr_single_page", side_effect=watching), \
             patch.object(pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t):
            pdf_report._attempt_ocr_cohere_parse("/nonexistent.pdf")

        final = json.loads(beacon.read_text())
        assert final == {"phase": "ocr", "done": pages, "total": pages}
        # Strictly increasing while pages were still being read: it moved
        # DURING the pass, not once at the end.
        assert seen == sorted(seen) and len(set(seen)) > 1

    def test_the_counter_never_exceeds_the_total(self, tmp_path) -> None:
        import json

        beacon = tmp_path / "p.json"
        progress = pdf_report._OcrProgress(str(beacon), 3)
        for _ in range(5):
            progress.advance()
        assert json.loads(beacon.read_text())["done"] == 3

    def test_no_progress_file_is_a_no_op(self) -> None:
        pdf_report._OcrProgress(None, 3).advance(2)  # must not raise
