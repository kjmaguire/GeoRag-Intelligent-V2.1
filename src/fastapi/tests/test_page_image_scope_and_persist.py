"""Page images under PDF_PARSE_MODE=all: scope, staging warnings, pending cleanup.

Audit 2026-10-04:
  * ``figures`` scope degenerated to ``all`` under PDF_PARSE_MODE=all, because
    every section on a Parse-read page is `cohere_parse` and only native
    sections counted as text.
  * persist copied each ``_pending`` render to its final key and never deleted
    it.
  * a truncation to IMAGE_EMBED_MAX_PAGES_PER_DOC and per-page staging
    failures only logged.
  * ParseOut had no ``page_image_manifest`` field, so pydantic dropped the
    manifest between parse and persist and nothing was ever finalised.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest
from georag_object_storage import Bucket

from app.hatchet_workflows import ingest_pdf as mod
from app.services.ingest import page_image

# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


class TestFiguresScopeUnderParseAll:
    def test_a_page_the_engine_read_that_had_a_text_layer_is_a_text_page(self) -> None:
        sections = [
            {"page_first": 1, "page_last": 1, "ocr_method": "cohere_parse"},
            {"page_first": 2, "page_last": 2, "ocr_method": "cohere_parse"},
        ]
        assert page_image.text_pages_from_sections(
            sections, engine_text_pages={1, 2}
        ) == {1, 2}

    def test_a_scan_the_engine_read_is_still_a_figure_page(self) -> None:
        """The reason the fix is NOT 'count every cohere_parse page': under
        ocr_only (and for scans under `all`) Parse reads the pages that have no
        text layer, which are exactly the ones `figures` exists to embed."""
        sections = [
            {"page_first": 1, "page_last": 1, "ocr_method": "cohere_parse"},  # had text
            {"page_first": 2, "page_last": 2, "ocr_method": "cohere_parse"},  # a scan
        ]
        assert page_image.text_pages_from_sections(sections, engine_text_pages={1}) == {1}
        assert page_image.text_pages_from_sections(sections) == set()

    def test_a_window_spanning_pages_counts_only_its_engine_text_pages(self) -> None:
        sections = [{"page_first": 3, "page_last": 5, "ocr_method": "cohere_parse"}]
        assert page_image.text_pages_from_sections(sections, engine_text_pages={3, 4}) == {3, 4}

    def test_native_fallback_pages_still_count_through_their_native_sections(self) -> None:
        sections = [
            {"page_first": 1, "page_last": 1, "ocr_method": "cohere_parse"},
            {"page_first": 2, "page_last": 2, "ocr_method": "fitz_native"},  # Parse failed
        ]
        assert page_image.text_pages_from_sections(sections, engine_text_pages={1}) == {1, 2}

    def test_tesseract_pages_with_no_text_never_count(self) -> None:
        sections = [{"page_first": 4, "page_last": 4, "ocr_method": "tesseract"}]
        assert page_image.text_pages_from_sections(sections, engine_text_pages={4}) == set()


class TestOcrReadPagesAreTextPages:
    """Under `figures` a scan OCR actually read is not a figure page.

    Counting every OCR'd page as a figure turned a 300-page scan into 300
    renders (hundreds of MB) and 300 placeholder passages.
    """

    _PROSE = "Drill hole DDH-12 intersected 12.4 m at 3.2 g/t Au from 120 m depth. "

    def test_an_ocr_page_with_enough_text_is_a_text_page(self) -> None:
        from app.services.ingest.pdf_report import PER_PAGE_MIN_CHARS

        text = "x" * PER_PAGE_MIN_CHARS
        assert len(text.strip()) >= PER_PAGE_MIN_CHARS
        for method in ("cohere_parse", "tesseract"):
            sections = [{"page_first": 7, "page_last": 7, "ocr_method": method, "text": text}]
            assert page_image.text_pages_from_sections(sections) == {7}

    def test_a_near_empty_ocr_page_is_still_a_figure_page(self) -> None:
        sections = [
            {"page_first": 8, "page_last": 8, "ocr_method": "cohere_parse",
             "text": "Legend  0  500 m"},
        ]
        assert page_image.text_pages_from_sections(sections) == set()

    def test_short_chunks_on_one_page_add_up(self) -> None:
        from app.services.ingest.pdf_report import PER_PAGE_MIN_CHARS

        half = "x" * (PER_PAGE_MIN_CHARS // 2 + 1)
        sections = [
            {"page_first": 3, "page_last": 3, "ocr_method": "tesseract", "text": half},
            {"page_first": 3, "page_last": 3, "ocr_method": "tesseract", "text": half},
        ]
        assert page_image.text_pages_from_sections(sections) == {3}

    def test_a_multi_page_window_is_shared_out_evenly(self) -> None:
        from app.services.ingest.pdf_report import PER_PAGE_MIN_CHARS

        # 3 pages, 2.5 x the minimum in total: ~0.83 x per page -> none qualifies
        sections = [{"page_first": 1, "page_last": 3, "ocr_method": "cohere_parse",
                     "text": "y" * int(PER_PAGE_MIN_CHARS * 2.5)}]
        assert page_image.text_pages_from_sections(sections) == set()
        sections[0]["text"] = "y" * (PER_PAGE_MIN_CHARS * 3)
        assert page_image.text_pages_from_sections(sections) == {1, 2, 3}

    def test_a_scanned_document_stages_only_its_empty_pages(self, staging, monkeypatch) -> None:
        monkeypatch.setenv("IMAGE_EMBED_PAGE_SCOPE", "figures")
        sections = [
            {"page_first": p, "page_last": p, "ocr_method": "cohere_parse",
             "text": self._PROSE * 3}
            for p in (1, 2, 3, 4, 5)
        ]  # page 6 came back empty
        manifest = page_image.stage_page_images(staging.path, "sha", sections)
        assert [m["page_number"] for m in manifest] == [6]


# ---------------------------------------------------------------------------
# Staging warnings
# ---------------------------------------------------------------------------


@pytest.fixture
def staging(monkeypatch, tmp_path):
    """stage_page_images with a fake PDF of N pages and fake storage."""
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.7 fake")

    class _Doc:
        def __init__(self, _bytes):
            self.pages = 6

        def __len__(self):
            return self.pages

        def close(self):
            pass

    fake_pdfium = types.ModuleType("pypdfium2")
    fake_pdfium.PdfDocument = _Doc
    monkeypatch.setitem(sys.modules, "pypdfium2", fake_pdfium)

    store = MagicMock()
    monkeypatch.setattr("georag_object_storage.get_storage_client", lambda: store)
    monkeypatch.setattr(page_image, "_embedding_backend_can_embed_images", lambda: True)
    monkeypatch.setenv("IMAGE_EMBED_PAGE_SCOPE", "all")

    state = types.SimpleNamespace(store=store, path=str(pdf), failing=set())

    def _render(_pdf_bytes, page_number, **_kw):
        if page_number in state.failing:
            raise RuntimeError("render failed")
        return b"png", 100, 100, 144.0

    monkeypatch.setattr(page_image, "render_page_png", _render)
    return state


class TestStagingWarnings:
    def test_a_truncation_to_the_cap_is_reported(self, staging) -> None:
        warnings: list[dict] = []
        manifest = page_image.stage_page_images(
            staging.path, "sha", [], max_pages=4, warnings_out=warnings,
        )
        assert [m["page_number"] for m in manifest] == [1, 2, 3, 4]
        (w,) = warnings
        assert w["code"] == "page_images_capped"
        assert w["cap"] == 4 and w["count"] == 2
        assert "IMAGE_EMBED_MAX_PAGES_PER_DOC=4" in w["detail"]

    def test_per_page_failures_are_reported_once_with_the_pages(self, staging) -> None:
        staging.failing = {2, 5}
        warnings: list[dict] = []
        manifest = page_image.stage_page_images(
            staging.path, "sha", [], warnings_out=warnings,
        )
        assert [m["page_number"] for m in manifest] == [1, 3, 4, 6]
        (w,) = warnings
        assert w["code"] == "page_image_staging_failed"
        assert w["pages"] == [2, 5] and w["count"] == 2

    def test_a_clean_stage_adds_no_warnings(self, staging) -> None:
        warnings: list[dict] = []
        page_image.stage_page_images(staging.path, "sha", [], warnings_out=warnings)
        assert warnings == []

    def test_the_old_call_shape_still_works(self, staging) -> None:
        assert len(page_image.stage_page_images(staging.path, "sha", [])) == 6

    def test_a_backend_with_no_image_encoder_stages_nothing(
        self, staging, monkeypatch
    ) -> None:
        """EMBEDDING_BACKEND=local cannot embed an image; the passage would sit
        unembedded forever and hold its run open."""
        monkeypatch.setattr(page_image, "_embedding_backend_can_embed_images", lambda: False)
        assert page_image.stage_page_images(staging.path, "sha", []) == []
        staging.store.put_bytes.assert_not_called()

    def test_figures_scope_uses_the_engine_text_pages(self, staging, monkeypatch) -> None:
        monkeypatch.setenv("IMAGE_EMBED_PAGE_SCOPE", "figures")
        sections = [
            {"page_first": p, "page_last": p, "ocr_method": "cohere_parse"}
            for p in range(1, 7)
        ]
        manifest = page_image.stage_page_images(
            staging.path, "sha", sections, engine_text_pages={1, 2, 3, 4, 5},
        )
        # Only page 6 (a scan Parse read) is a figure page.
        assert [m["page_number"] for m in manifest] == [6]


class TestFiguresScopeCap:
    @staticmethod
    def _many_pages(monkeypatch, n: int) -> None:
        class _Doc:
            def __init__(self, _bytes):
                pass

            def __len__(self):
                return n

            def close(self):
                pass

        fake = types.ModuleType("pypdfium2")
        fake.PdfDocument = _Doc
        monkeypatch.setitem(sys.modules, "pypdfium2", fake)

    def test_figures_scope_is_capped_at_fifty_pages(self, staging, monkeypatch) -> None:
        self._many_pages(monkeypatch, 300)
        monkeypatch.setenv("IMAGE_EMBED_PAGE_SCOPE", "figures")
        monkeypatch.delenv("IMAGE_EMBED_MAX_PAGES_PER_DOC", raising=False)
        warnings: list[dict] = []
        manifest = page_image.stage_page_images(
            staging.path, "sha", [], warnings_out=warnings,  # no text anywhere: a 300-page scan
        )
        assert len(manifest) == page_image.FIGURES_SCOPE_MAX_PAGES == 50
        (w,) = warnings
        assert w["code"] == "page_images_capped" and w["cap"] == 50 and w["count"] == 250

    def test_the_lower_of_the_env_cap_and_fifty_applies_under_figures(
        self, staging, monkeypatch
    ) -> None:
        self._many_pages(monkeypatch, 300)
        monkeypatch.setenv("IMAGE_EMBED_PAGE_SCOPE", "figures")
        monkeypatch.setenv("IMAGE_EMBED_MAX_PAGES_PER_DOC", "10")
        assert len(page_image.stage_page_images(staging.path, "sha", [])) == 10

    def test_scope_all_keeps_the_configured_cap(self, staging, monkeypatch) -> None:
        self._many_pages(monkeypatch, 300)
        monkeypatch.setenv("IMAGE_EMBED_PAGE_SCOPE", "all")
        monkeypatch.delenv("IMAGE_EMBED_MAX_PAGES_PER_DOC", raising=False)
        assert len(page_image.stage_page_images(staging.path, "sha", [])) == 300


def test_backend_gate_follows_the_embedding_backend(monkeypatch) -> None:
    from app.services import embedding

    for backend, ok in (("cohere", True), ("bedrock", True), ("local", False)):
        monkeypatch.setattr(embedding, "EMBEDDING_BACKEND", backend)
        assert page_image._embedding_backend_can_embed_images() is ok


# ---------------------------------------------------------------------------
# ParseOut carries the manifest
# ---------------------------------------------------------------------------


def test_parse_out_no_longer_drops_the_page_image_manifest() -> None:
    out = mod.ParseOut(
        sha256="x",
        page_image_manifest=[{"page_number": 1, "pending_key": "page-images/_pending/x/p1.png"}],
        page_image_warnings=[{"code": "page_images_capped"}],
    )
    dumped = out.model_dump()
    assert dumped["page_image_manifest"][0]["page_number"] == 1
    assert dumped["page_image_warnings"][0]["code"] == "page_images_capped"


# ---------------------------------------------------------------------------
# persist: pending renders are deleted after the commit
# ---------------------------------------------------------------------------


def _images(pages: list[int]) -> list[dict]:
    return [
        {"page_number": p, "pending_key": f"page-images/_pending/sha/page_{p:05d}.png"}
        for p in pages
    ]


_SECTIONS = [{"section_number": None, "section_title": "Preamble",
              "text": "Cross-section A-A' through the eastern fault zone, drill traces shown.",
              "page_first": 1, "page_last": 1, "ocr_method": "fitz_native"}]


@pytest.mark.asyncio
async def test_pending_renders_are_deleted_only_after_the_transaction_commits() -> None:
    from tests.test_pdf_run_outcome import _run_persist

    events: list[str] = []
    store = MagicMock()
    store.copy.side_effect = lambda *a, **k: events.append("copy")
    store.delete.side_effect = lambda *a, **k: events.append("delete")

    final, _diag, txn_events = await _run_persist(
        {"sections": _SECTIONS, "text_page_coverage_pct": 1.0, "is_scanned": False,
         "parser_used": "fitz", "page_image_manifest": _images([1, 2])},
        page_count=2, store=store,
    )

    assert store.copy.call_count == 2
    assert store.delete.call_count == 2
    deleted = sorted(c.args[1] for c in store.delete.call_args_list)
    assert deleted == [f"page-images/_pending/sha/page_{p:05d}.png" for p in (1, 2)]
    assert all(c.args[0] == Bucket.BRONZE_RASTER for c in store.delete.call_args_list)
    # Never inside the transaction: a rollback + Hatchet retry still needs them.
    assert txn_events[-1] == "commit"
    assert [w for w in final.run_warnings if w["code"] == "page_image_persist_failed"] == []


@pytest.mark.asyncio
async def test_the_page_copies_happen_before_any_database_transaction_opens() -> None:
    """A 400-page report is 400 sequential S3 copies. Inside persist's single
    transaction that held a database transaction open (and a pooled connection)
    for the whole of them, on the Postgres that also runs the Hatchet queue
    (Hatchet audit 2026-10, finding 13). They are S3-only, so they come first; a
    rollback + retry copies again from the same untouched pending objects."""
    from tests.test_pdf_run_outcome import _run_persist

    events: list[str] = []
    store = MagicMock()
    store.copy.side_effect = lambda *a, **k: events.append("copy")

    final, _diag, txn_events = await _run_persist(
        {"sections": _SECTIONS, "text_page_coverage_pct": 1.0, "is_scanned": False,
         "parser_used": "fitz", "page_image_manifest": _images([1, 2, 3])},
        page_count=3, store=store, events=events,
    )

    assert txn_events is events
    assert events.count("copy") == 3
    assert "begin" in events
    assert max(i for i, e in enumerate(events) if e == "copy") < events.index("begin"), (
        f"a page was copied inside the transaction: {events}"
    )
    assert [w for w in final.run_warnings if w["code"] == "page_image_persist_failed"] == []


@pytest.mark.asyncio
async def test_a_failed_copy_keeps_its_pending_object_and_is_reported() -> None:
    from tests.test_pdf_run_outcome import _run_persist

    store = MagicMock()

    def _copy(_sb, src, _db, _dest, **_kw):
        if src.endswith("page_00002.png"):
            raise OSError("s3 down")

    store.copy.side_effect = _copy
    store.exists.return_value = False  # nothing at the final key either

    final, _diag, _ = await _run_persist(
        {"sections": _SECTIONS, "text_page_coverage_pct": 1.0, "is_scanned": False,
         "parser_used": "fitz", "page_image_manifest": _images([1, 2])},
        page_count=2, store=store,
    )

    deleted = [c.args[1] for c in store.delete.call_args_list]
    assert deleted == ["page-images/_pending/sha/page_00001.png"]
    (w,) = [w for w in final.run_warnings if w["code"] == "page_image_persist_failed"]
    assert w["pages"] == [2]
    assert w["severity"] == "warning"


@pytest.mark.asyncio
async def test_staging_warnings_reach_the_run() -> None:
    from tests.test_pdf_run_outcome import _run_persist

    final, diag, _ = await _run_persist(
        {"sections": _SECTIONS, "text_page_coverage_pct": 1.0, "is_scanned": False,
         "parser_used": "fitz",
         "page_image_warnings": [{"code": "page_images_capped", "detail": "dropped 5"}]},
        page_count=2,
    )
    assert [w["code"] for w in final.run_warnings] == ["page_images_capped"]
    assert diag.await_args.kwargs["warnings"] == final.run_warnings


@pytest.mark.asyncio
async def test_a_retry_after_commit_does_not_report_finalised_pages_as_failed() -> None:
    """The pending objects are gone (deleted post-commit) so every copy fails,
    but every page is already under its final key: that is success."""
    from tests.test_pdf_run_outcome import _run_persist

    store = MagicMock()
    store.copy.side_effect = OSError("NoSuchKey: pending object is gone")
    store.exists.return_value = True

    final, _diag, _ = await _run_persist(
        {"sections": _SECTIONS, "text_page_coverage_pct": 1.0, "is_scanned": False,
         "parser_used": "fitz", "page_image_manifest": _images([1, 2])},
        page_count=2, store=store,
    )

    assert [c.args[1] for c in store.exists.call_args_list] == [
        page_image.final_key(final.report_id, 1), page_image.final_key(final.report_id, 2),
    ]
    assert all(c.args[0] == Bucket.BRONZE_RASTER for c in store.exists.call_args_list)
    assert [w for w in final.run_warnings if w["code"] == "page_image_persist_failed"] == []


@pytest.mark.asyncio
async def test_an_existence_check_that_itself_fails_is_a_failed_page() -> None:
    from tests.test_pdf_run_outcome import _run_persist

    store = MagicMock()
    store.copy.side_effect = OSError("s3 down")
    store.exists.side_effect = OSError("s3 still down")

    final, _diag, _ = await _run_persist(
        {"sections": _SECTIONS, "text_page_coverage_pct": 1.0, "is_scanned": False,
         "parser_used": "fitz", "page_image_manifest": _images([1])},
        page_count=1, store=store,
    )
    (w,) = [w for w in final.run_warnings if w["code"] == "page_image_persist_failed"]
    assert w["pages"] == [1]


# ---------------------------------------------------------------------------
# The run's verdict commits WITH the passages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_run_diagnostics_are_written_inside_the_persist_transaction() -> None:
    """A sweep that sees every passage embedded must find the diagnostics too:
    written after the commit there was a window for a bare ``completed``."""
    from tests.test_pdf_run_outcome import _run_persist

    final, diag, events = await _run_persist(
        {"sections": _SECTIONS, "text_page_coverage_pct": 1.0, "is_scanned": False,
         "parser_used": "fitz"},
        page_count=1,
    )

    diag.assert_awaited_once()  # the post-commit fallback did not run as well
    assert diag.await_args.kwargs["warnings"] == final.run_warnings
    assert diag.await_args.kwargs["rows_written"] == final.passages_written
    assert events.count("diagnostics_in_txn") == 1 and "diagnostics" not in events
    # the write precedes the outermost commit, which is the last event
    assert events[-1] == "commit"
    assert events.index("diagnostics_in_txn") < len(events) - 1


@pytest.mark.asyncio
async def test_a_failed_in_transaction_stamp_falls_back_after_the_commit() -> None:
    from tests.test_pdf_run_outcome import _run_persist

    final, diag, events = await _run_persist(
        {"sections": _SECTIONS, "text_page_coverage_pct": 1.0, "is_scanned": False,
         "parser_used": "fitz"},
        page_count=1, diagnostics_ok=False,
    )
    assert diag.await_count == 2
    assert events.index("diagnostics_in_txn") < events.index("diagnostics")
    assert diag.await_args_list[1].kwargs["warnings"] == final.run_warnings
