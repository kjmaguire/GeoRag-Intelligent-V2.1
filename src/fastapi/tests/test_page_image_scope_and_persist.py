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

    def test_tesseract_pages_never_count(self) -> None:
        sections = [{"page_first": 4, "page_last": 4, "ocr_method": "tesseract"}]
        assert page_image.text_pages_from_sections(sections, engine_text_pages={4}) == set()


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
async def test_a_failed_copy_keeps_its_pending_object_and_is_reported() -> None:
    from tests.test_pdf_run_outcome import _run_persist

    store = MagicMock()

    def _copy(_sb, src, _db, _dest, **_kw):
        if src.endswith("page_00002.png"):
            raise OSError("s3 down")

    store.copy.side_effect = _copy

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
