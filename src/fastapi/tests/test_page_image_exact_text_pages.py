"""An image-only page inside a multi-page chunk still gets its image (audit finding 14).

Under ``IMAGE_EMBED_PAGE_SCOPE=figures`` only the pages the parser could not
read as text are rendered. That set was inferred from the SECTIONS' page spans
(``page_image.text_pages_from_sections``), and the unified sliding-window
chunker emits chunks that span several pages: a chunk covering pages 3-5 marked
all three as text, so a figure-only page 4 in the middle of a text run was never
selected and never got an image passage.

The parser now returns the exact set (``ReportParseResult.text_pages``,
``pdf_report.pages_read_as_text``), ``_run_parser_subprocess`` hands it on and
``stage_page_images(text_pages=...)`` uses it.
"""
from __future__ import annotations

# ruff: noqa: F811 - the `staging` fixture is imported, then named as a parameter
from types import SimpleNamespace

import pytest

from app.hatchet_workflows import ingest_pdf as mod
from app.services.ingest import page_image
from app.services.ingest.pdf_report import PER_PAGE_MIN_CHARS, pages_read_as_text

# The fake 6-page document + storage the sibling suite stages against.
from tests.test_page_image_scope_and_persist import staging  # noqa: F401 - a fixture

_TEXT = "Drill hole DDH-12 intersected 12.4 m at 3.2 g/t Au from 120 m depth. " * 3


class TestPagesReadAsText:
    def test_a_blank_page_between_two_text_pages_is_not_a_text_page(self) -> None:
        """What the page-span inference got wrong: one chunk, pages 1-3, page 2 empty."""
        per_page_text = [(1, _TEXT), (3, _TEXT)]            # the producer skips page 2

        assert pages_read_as_text(per_page_text, {1: "fitz_native", 3: "fitz_native"}) == [1, 3]

    def test_a_blank_entry_is_not_text(self) -> None:
        assert pages_read_as_text([(1, "   "), (2, ""), (3, None)], {}) == []   # type: ignore[list-item]

    def test_a_native_page_counts_whatever_its_length(self) -> None:
        """A short salvaged text-layer page was a text page under the span rule too."""
        assert pages_read_as_text([(4, "FIGURE 3")], {4: "fitz_native"}) == [4]
        assert pages_read_as_text([(4, "FIGURE 3")], {4: "pdfplumber_native"}) == [4]
        assert pages_read_as_text([(4, "FIGURE 3")], {}) == [4]       # no method: text layer

    @pytest.mark.parametrize("method", ["cohere_parse", "tesseract"])
    def test_an_ocr_page_needs_enough_text_to_be_a_text_page(self, method: str) -> None:
        enough = "x" * PER_PAGE_MIN_CHARS
        short = "Legend  0  500 m"

        assert pages_read_as_text([(7, enough)], {7: method}) == [7]
        assert pages_read_as_text([(8, short)], {8: method}) == []          # the map: a figure

    def test_an_engine_page_that_had_a_text_layer_counts_whatever_its_length(self) -> None:
        """PDF_PARSE_MODE=all: Parse re-read a page that already had text."""
        assert pages_read_as_text([(2, "short")], {2: "cohere_parse"}, {2}) == [2]
        assert pages_read_as_text([(2, "short")], {2: "cohere_parse"}) == []

    def test_the_result_is_sorted_and_unique(self) -> None:
        pages = [(5, _TEXT), (1, _TEXT), (5, _TEXT)]

        assert pages_read_as_text(pages, {1: "fitz_native", 5: "fitz_native"}) == [1, 5]

    def test_no_text_at_all(self) -> None:
        assert pages_read_as_text([], {}) == []
        assert pages_read_as_text(None, {}) == []


class TestStagingUsesTheExactSet:
    # `staging` fakes a 6-page PDF; the sections below are ONE native chunk
    # spanning pages 1-3, the shape the sliding-window chunker produces.
    _SPANNING_CHUNK = [
        {"page_first": 1, "page_last": 3, "ocr_method": "fitz_native", "text": _TEXT},
    ]

    def test_the_span_inference_hides_the_blank_page(self, staging, monkeypatch) -> None:
        """The defect: without the exact set, pages 1-3 are all 'text'."""
        monkeypatch.setenv("IMAGE_EMBED_PAGE_SCOPE", "figures")

        manifest = page_image.stage_page_images(staging.path, "sha", self._SPANNING_CHUNK)

        assert [m["page_number"] for m in manifest] == [4, 5, 6]        # page 2 is missing

    def test_the_exact_set_selects_it(self, staging, monkeypatch) -> None:
        monkeypatch.setenv("IMAGE_EMBED_PAGE_SCOPE", "figures")

        manifest = page_image.stage_page_images(
            staging.path, "sha", self._SPANNING_CHUNK, text_pages={1, 3},
        )

        assert [m["page_number"] for m in manifest] == [2, 4, 5, 6]

    def test_an_empty_exact_set_means_every_page_is_a_figure(self, staging, monkeypatch) -> None:
        """No text anywhere (None would mean 'not computed')."""
        monkeypatch.setenv("IMAGE_EMBED_PAGE_SCOPE", "figures")

        manifest = page_image.stage_page_images(
            staging.path, "sha", self._SPANNING_CHUNK, text_pages=set(),
        )

        assert [m["page_number"] for m in manifest] == [1, 2, 3, 4, 5, 6]

    def test_scope_all_ignores_the_set(self, staging) -> None:
        manifest = page_image.stage_page_images(
            staging.path, "sha", self._SPANNING_CHUNK, text_pages={1, 2, 3},
        )

        assert len(manifest) == 6


class TestSubprocessHandsTheSetOn:
    """``_run_parser_subprocess`` -> ``stage_page_images(text_pages=...)``."""

    @staticmethod
    def _run(monkeypatch, tmp_path, result: SimpleNamespace) -> dict:
        import app.services.ingest.pdf_report as pdf_report

        calls: dict = {}

        def _stage(_path, _sha, _sections, **kwargs):
            calls.update(kwargs)
            return []

        monkeypatch.setattr(pdf_report, "parse_pdf_report", lambda *_a, **_k: result)
        monkeypatch.setattr(page_image, "stage_page_images", _stage)
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.7 fake")
        mod._run_parser_subprocess(str(pdf), "sha")
        return calls

    @staticmethod
    def _result(**extra) -> SimpleNamespace:
        base = dict(sections=[], warnings=[], page_languages=[], resource_tables=[])
        base.update(extra)
        return SimpleNamespace(**base)

    def test_the_exact_set_reaches_staging(self, monkeypatch, tmp_path) -> None:
        calls = self._run(monkeypatch, tmp_path, self._result(text_pages=[1, 3]))

        assert calls["text_pages"] == {1, 3}

    def test_a_result_without_the_field_stages_the_old_way(self, monkeypatch, tmp_path) -> None:
        calls = self._run(monkeypatch, tmp_path, self._result())

        assert "text_pages" not in calls

    def test_an_empty_set_is_passed_as_empty_not_dropped(self, monkeypatch, tmp_path) -> None:
        calls = self._run(monkeypatch, tmp_path, self._result(text_pages=[]))

        assert calls["text_pages"] == set()
