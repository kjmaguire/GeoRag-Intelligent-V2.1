"""Batched Document Intelligence requests (2026-08-20).

Document Intelligence bills per page but does not require one HTTP request
per page. The one-page-per-request shape predates the S0 tier: it exists
because F0 rejected ``pages=N`` for N > 2. Measured on the live resource,
1,930 DI calls produced 603 billed pages — ~3.2 round-trips per page once
async polling is counted — so the request count, not the page count, was
what a fully scanned report waited on.

What is pinned here:

  1. Blocks are planned to cover every page exactly once, in order.
  2. A multi-page result is fanned back out per page, tables included, and
     a page DI said nothing about still appears (empty, not missing).
  3. The whole-document path issues ceil(pages / block) requests instead
     of one per page, and still assembles in page order.
  4. Recovery is layered, and the two failure modes are NOT the same:
       - block ran, page came back empty  -> per-page path WITHOUT a
         second billed DI request (straight to raster tiles/tesseract);
       - block never ran                  -> per-page path WITH its own
         DI request, i.e. exactly the pre-batching behavior.
  5. The billed-page metric counts pages, not requests.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.ingest import document_intelligence_client as di
from app.services.ingest import pdf_report


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(di.ENGINE_ENV, raising=False)
    monkeypatch.delenv(di.ENDPOINT_ENV, raising=False)
    monkeypatch.delenv(di.KEY_ENV, raising=False)
    monkeypatch.delenv("AZURE_DI_PAGES_PER_BATCH", raising=False)


@pytest.fixture
def stub_page_count():
    """Provide pdf2image.pdfinfo_from_path without requiring pdf2image.

    Mirrors test_di_whole_doc_parallel: the function under test imports it
    lazily and the poppler wrapper is absent from some images.
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


def _sdk_page(page_number: int, *, lines: list[str] | None = None):
    """One entry of an analyze result's `pages` collection."""
    words = [MagicMock(content=f"w{page_number}", confidence=0.9, polygon=None)]
    line_mocks = [MagicMock(content=text) for text in (lines or [f"PAGE-{page_number}"])]
    return MagicMock(page_number=page_number, words=words, lines=line_mocks)


def _sdk_table(page_number: int, rows: list[list[str]]):
    """One entry of an analyze result's `tables` collection."""
    cells = [
        MagicMock(row_index=r, column_index=c, content=value)
        for r, row in enumerate(rows)
        for c, value in enumerate(row)
    ]
    return MagicMock(
        row_count=len(rows),
        column_count=len(rows[0]),
        cells=cells,
        bounding_regions=[MagicMock(page_number=page_number)],
    )


class TestBlockPlan:
    def test_exact_multiple(self) -> None:
        assert pdf_report._ocr_block_plan(50, 25) == [(1, 25), (26, 25)]

    def test_trailing_partial_block(self) -> None:
        assert pdf_report._ocr_block_plan(7, 3) == [(1, 3), (4, 3), (7, 1)]

    def test_block_larger_than_document(self) -> None:
        assert pdf_report._ocr_block_plan(4, 25) == [(1, 4)]

    def test_every_page_covered_exactly_once(self) -> None:
        covered = [
            page
            for first, count in pdf_report._ocr_block_plan(203, 25)
            for page in range(first, first + count)
        ]
        assert covered == list(range(1, 204))


class TestPagesPerBatch:
    def test_default(self) -> None:
        assert di.pages_per_batch() == 25

    def test_one_disables_batching(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZURE_DI_PAGES_PER_BATCH", "1")
        assert di.pages_per_batch() == 1

    def test_clamped_to_ceiling(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZURE_DI_PAGES_PER_BATCH", "5000")
        assert di.pages_per_batch() == di._MAX_BLOCK_SIZE

    def test_zero_and_negative_clamp_up_to_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZURE_DI_PAGES_PER_BATCH", "0")
        assert di.pages_per_batch() == 1
        monkeypatch.setenv("AZURE_DI_PAGES_PER_BATCH", "-4")
        assert di.pages_per_batch() == 1

    def test_garbage_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZURE_DI_PAGES_PER_BATCH", "twenty")
        assert di.pages_per_batch() == 25


class TestSplitResultByPage:
    def test_text_and_tables_land_on_their_own_page(self) -> None:
        result = MagicMock(
            pages=[_sdk_page(1), _sdk_page(2), _sdk_page(3)],
            tables=[_sdk_table(2, [["Hole", "Au"], ["DDH-1", "1.2"]])],
        )

        per_page = di._split_result_by_page(result, 3)

        assert per_page[1].text == "PAGE-1"
        assert per_page[2].text == "PAGE-2"
        assert per_page[3].text == "PAGE-3"
        # The table is anchored to page 2 and must not leak onto 1 or 3 —
        # a table rendered under the wrong page is a citation that points
        # a geologist at the wrong hole.
        assert per_page[1].tables == []
        assert per_page[2].tables == [[["Hole", "Au"], ["DDH-1", "1.2"]]]
        assert per_page[3].tables == []

    def test_page_di_said_nothing_about_is_present_but_empty(self) -> None:
        """Missing != failed.

        An empty-but-successful result is the signal `_ocr_single_page`
        already uses to escalate to raster tiles. Dropping the key instead
        would make a blank page indistinguishable from a failed block.
        """
        result = MagicMock(pages=[_sdk_page(1), _sdk_page(3)], tables=[])

        per_page = di._split_result_by_page(result, 3)

        assert set(per_page) == {1, 2, 3}
        assert per_page[2].text == ""
        assert per_page[2].request_succeeded is True

    def test_table_without_a_bounding_region_is_dropped_not_misfiled(self) -> None:
        table = _sdk_table(1, [["a"]])
        table.bounding_regions = []
        result = MagicMock(pages=[_sdk_page(1)], tables=[table])

        per_page = di._split_result_by_page(result, 1)

        assert per_page[1].tables == []


class TestAnalyzePageBlock:
    pytestmark = pytest.mark.asyncio

    async def _client_returning(self, analyze_result):
        poller = AsyncMock()
        poller.result = AsyncMock(return_value=analyze_result)
        client = MagicMock()
        client.begin_analyze_document = AsyncMock(return_value=poller)
        return client

    async def test_sends_one_request_with_no_pages_selector(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(di.ENDPOINT_ENV, "https://example.cognitiveservices.azure.com")
        monkeypatch.setenv(di.KEY_ENV, "fake-key")
        client = await self._client_returning(
            MagicMock(pages=[_sdk_page(1), _sdk_page(2)], tables=[])
        )

        with patch.object(di, "_build_client", return_value=client):
            per_page = await di.analyze_page_block(b"%PDF-1.4 block", 2)

        from azure.ai.documentintelligence.models import DocumentContentFormat

        assert sorted(per_page) == [1, 2]
        # No `pages` kwarg: the block IS the document, so a selector would
        # only re-introduce the F0-era per-page narrowing.
        client.begin_analyze_document.assert_awaited_once_with(
            "prebuilt-layout",
            body=b"%PDF-1.4 block",
            output_content_format=DocumentContentFormat.MARKDOWN,
        )

    async def test_request_failure_returns_empty_mapping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(di.ENDPOINT_ENV, "https://example.cognitiveservices.azure.com")
        monkeypatch.setenv(di.KEY_ENV, "fake-key")
        client = MagicMock()
        client.begin_analyze_document = AsyncMock(side_effect=RuntimeError("boom"))

        with patch.object(di, "_build_client", return_value=client):
            per_page = await di.analyze_page_block(b"%PDF-1.4 block", 5)

        # {} not {1: empty, ...}: the caller must be able to tell "never
        # ran" from "ran and page was blank".
        assert per_page == {}

    async def test_meters_pages_not_requests(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(di.ENDPOINT_ENV, "https://example.cognitiveservices.azure.com")
        monkeypatch.setenv(di.KEY_ENV, "fake-key")
        client = await self._client_returning(
            MagicMock(pages=[_sdk_page(n) for n in range(1, 26)], tables=[])
        )

        with patch.object(di, "_build_client", return_value=client), \
             patch.object(di, "_meter_pages") as meter:
            await di.analyze_page_block(b"%PDF-1.4 block", 25)

        meter.assert_called_once_with(25)

    async def test_not_configured_still_raises(self) -> None:
        with pytest.raises(di.DocumentIntelligenceNotConfigured):
            await di.analyze_page_block(b"%PDF-1.4 block", 3)


class TestSkipDiPageRequest:
    def test_skips_the_duplicate_billed_request(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A block already asked about this page; asking again costs a page
        to get the same empty answer."""
        monkeypatch.setenv(di.ENGINE_ENV, "azure_document_intelligence")

        with patch.object(pdf_report, "_di_single_page_request") as single, \
             patch.object(pdf_report, "_ocr_tiled_pdf_page", side_effect=RuntimeError("no tiles")), \
             patch.object(pdf_report, "_ocr_budget_take") as budget:
            pdf_report._ocr_single_page(
                "/nonexistent.pdf", 4, skip_di_page_request=True,
            )

        single.assert_not_called()
        # The block already paid for this page — charging the per-document
        # budget twice would shrink the effective cap.
        budget.assert_not_called()

    def test_without_the_flag_the_request_still_happens(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(di.ENGINE_ENV, "azure_document_intelligence")

        with patch.object(
            pdf_report, "_di_single_page_request",
            return_value=di.PageOcrResult("", 0.0, request_succeeded=False, error="x"),
        ) as single, patch.object(pdf_report, "_ocr_budget_take", return_value=True):
            pdf_report._ocr_single_page("/nonexistent.pdf", 4)

        single.assert_called_once()


class TestWholeDocumentBatching:
    """The reason for the change: request count on the full-scan path."""

    def _run(self, path: str = "/scan.pdf"):
        return pdf_report._attempt_ocr_document_intelligence(path)

    def test_one_request_per_block_not_per_page(self, stub_page_count) -> None:
        stub_page_count(50)
        calls: list[tuple[int, int]] = []

        def fake_block(_path, first_page, page_count):
            calls.append((first_page, page_count))
            return {
                first_page + offset: di.PageOcrResult(f"PAGE-{first_page + offset}", 0.9)
                for offset in range(page_count)
            }

        with patch.object(pdf_report, "_ocr_page_block_di", side_effect=fake_block), \
             patch.object(pdf_report, "_ocr_single_page") as per_page, \
             patch.object(pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t):
            result = self._run()

        assert calls == [(1, 25), (26, 25)]
        per_page.assert_not_called()
        assert [p.page_number for p in result.pages] == list(range(1, 51))
        assert [p.text for p in result.pages] == [f"PAGE-{n}" for n in range(1, 51)]

    def test_batched_pages_keep_document_intelligence_provenance(
        self, stub_page_count
    ) -> None:
        """parser_used is derived from per-page ocr_method; a batched page
        that lost its label would downgrade the whole doc to 'ocr_mixed'."""
        stub_page_count(3)

        with patch.object(
            pdf_report, "_ocr_page_block_di",
            side_effect=lambda _p, first, count: {
                first + o: di.PageOcrResult(f"PAGE-{first + o}", 0.9)
                for o in range(count)
            },
        ), patch.object(pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t):
            result = self._run()

        assert result.parser_used == "ocr_document_intelligence"
        assert all(
            page.assessment["ocr_method"] == "document_intelligence"
            for page in result.pages
        )

    def test_empty_page_in_a_good_block_recovers_without_a_second_di_call(
        self, stub_page_count
    ) -> None:
        stub_page_count(3)
        seen: dict[int, bool] = {}

        def fake_single(_path, page_num, **kwargs):
            seen[page_num] = kwargs["skip_di_page_request"]
            return (
                f"TESS-{page_num}", 0.5,
                pdf_report._assess_ocr_result(f"TESS-{page_num}", [0.5], detected_region_count=1),
                [],
            )

        with patch.object(
            pdf_report, "_ocr_page_block_di",
            # Page 2 came back blank; 1 and 3 are fine.
            return_value={
                1: di.PageOcrResult("PAGE-1", 0.9),
                2: di.PageOcrResult("", 0.0),
                3: di.PageOcrResult("PAGE-3", 0.9),
            },
        ), patch.object(pdf_report, "_ocr_single_page", side_effect=fake_single), \
             patch.object(pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t):
            result = self._run()

        assert seen == {2: True}, "only the blank page should be re-driven"
        assert [p.text for p in result.pages] == ["PAGE-1", "TESS-2", "PAGE-3"]

    def test_failed_block_falls_back_to_the_pre_batching_behavior(
        self, stub_page_count
    ) -> None:
        stub_page_count(4)
        seen: dict[int, bool] = {}

        def fake_single(_path, page_num, **kwargs):
            seen[page_num] = kwargs["skip_di_page_request"]
            return (
                f"PAGE-{page_num}", 0.9,
                pdf_report._assess_ocr_result(f"PAGE-{page_num}", [0.9], detected_region_count=1),
                [],
            )

        with patch.object(pdf_report, "_ocr_page_block_di", return_value={}), \
             patch.object(pdf_report, "_ocr_single_page", side_effect=fake_single), \
             patch.object(pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t):
            result = self._run()

        # Every page re-driven, and each still gets its OWN DI request —
        # a failed block must not silently downgrade the document to
        # tesseract-only.
        assert seen == {1: False, 2: False, 3: False, 4: False}
        assert [p.page_number for p in result.pages] == [1, 2, 3, 4]

    def test_batch_size_one_bypasses_the_block_pass_entirely(
        self, stub_page_count, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The escape hatch has to actually escape: no block request at all."""
        monkeypatch.setenv("AZURE_DI_PAGES_PER_BATCH", "1")
        stub_page_count(3)

        def fake_single(_path, page_num, **kwargs):
            assert kwargs["skip_di_page_request"] is False
            return (
                f"PAGE-{page_num}", 0.9,
                pdf_report._assess_ocr_result(f"PAGE-{page_num}", [0.9], detected_region_count=1),
                [],
            )

        with patch.object(pdf_report, "_ocr_page_block_di") as block, \
             patch.object(pdf_report, "_ocr_single_page", side_effect=fake_single), \
             patch.object(pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t):
            result = self._run()

        block.assert_not_called()
        assert [p.page_number for p in result.pages] == [1, 2, 3]

    def test_block_tables_survive_onto_their_page(self, stub_page_count) -> None:
        stub_page_count(2)
        grid = [["Hole", "Au g/t"], ["DDH-1", "1.2"]]

        with patch.object(
            pdf_report, "_ocr_page_block_di",
            return_value={
                1: di.PageOcrResult("PAGE-1", 0.9),
                2: di.PageOcrResult("PAGE-2", 0.9, tables=[grid]),
            },
        ), patch.object(pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t):
            result = self._run()

        assert result.pages[0].tables == ()
        assert result.pages[1].tables == (grid,)


class TestBlockBudget:
    def test_block_charges_the_budget_for_every_page_it_covers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A block that only charged 1 page would let a 300-page cap pass
        7,500 pages through."""
        taken: list[int] = []

        with patch.object(
            pdf_report, "_ocr_budget_take",
            side_effect=lambda _p, pages: taken.append(pages) or True,
        ), patch.object(
            pdf_report, "_slice_page_selection_pdf_bytes", return_value=b"%PDF-1.4",
        ), patch.object(
            di, "ocr_page_block_sync", return_value={1: di.PageOcrResult("x", 0.9)},
        ):
            pdf_report._ocr_page_block_di("/scan.pdf", 26, 25)

        assert taken == [25]

    def test_exhausted_budget_yields_an_empty_mapping(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with patch.object(pdf_report, "_ocr_budget_take", return_value=False), \
             patch.object(pdf_report, "_slice_page_selection_pdf_bytes") as slicer:
            assert pdf_report._ocr_page_block_di("/scan.pdf", 1, 25) == {}
        slicer.assert_not_called()

    def test_local_page_numbers_are_rebased_onto_the_document(self) -> None:
        with patch.object(pdf_report, "_ocr_budget_take", return_value=True), \
             patch.object(
                 pdf_report, "_slice_page_selection_pdf_bytes", return_value=b"%PDF-1.4",
             ), patch.object(
                 di, "ocr_page_block_sync",
                 return_value={
                     1: di.PageOcrResult("a", 0.9),
                     2: di.PageOcrResult("b", 0.9),
                 },
             ):
            mapping = pdf_report._ocr_page_block_di("/scan.pdf", 51, 2)

        # Block-local 1,2 -> document 51,52. Off-by-one here would file
        # every page of the report under the wrong page number.
        assert sorted(mapping) == [51, 52]
        assert mapping[51].text == "a"
        assert mapping[52].text == "b"

    def test_slice_failure_yields_an_empty_mapping(self) -> None:
        with patch.object(pdf_report, "_ocr_budget_take", return_value=True), \
             patch.object(
                 pdf_report, "_slice_page_selection_pdf_bytes",
                 side_effect=RuntimeError("corrupt xref"),
             ):
            assert pdf_report._ocr_page_block_di("/scan.pdf", 1, 25) == {}
