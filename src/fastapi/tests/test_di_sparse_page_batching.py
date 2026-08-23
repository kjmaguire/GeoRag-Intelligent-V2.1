"""Batched Document Intelligence for the MIXED-document path (2026-08-23).

The fully-scanned path has batched since 2026-08-20. The mixed path -- a
text-native report with an appendix of plates, or scattered scanned
inserts -- kept issuing one DI request per short page, and it is the
commoner shape in this corpus: a report with 40 image pages was 40
submissions plus their polling.

What is pinned here:

  1. A sparse page set is batched by SELECTION, not by contiguous run.
     Grouping scattered pages into runs either yields runs of length one
     (no saving at all) or sweeps in pages that already had text and are
     billed per page regardless.
  2. The local-to-absolute page remap is POSITIONAL. DI numbers the
     uploaded document's pages 1..N; the uploaded document is the selected
     pages in order, so `first_page + local - 1` -- correct for the
     contiguous path -- is wrong for every page after a gap. This is the
     subtle failure the selection path can have, and it would surface as
     OCR text attached to the wrong page rather than as an error.
  3. The budget is charged for the pages actually sent.
  4. A page the batch answered with no text does NOT get a second billed
     per-page DI request; a batch that never ran leaves the per-page path
     exactly as it was.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services.ingest import document_intelligence_client as di
from app.services.ingest import pdf_report


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AZURE_DI_PAGES_PER_BATCH", raising=False)


class TestTheRemapIsPositional:
    def test_pages_after_a_gap_keep_their_own_text(self) -> None:
        # Selected 3, 9, 40. DI answers about its own pages 1, 2, 3.
        selected = [3, 9, 40]
        block = {
            1: di.PageOcrResult("PAGE-3", 0.9),
            2: di.PageOcrResult("PAGE-9", 0.9),
            3: di.PageOcrResult("PAGE-40", 0.9),
        }

        with patch.object(pdf_report, "_di_budget_take", return_value=True), \
             patch.object(pdf_report, "_slice_page_selection_pdf_bytes", return_value=b"%PDF"), \
             patch.object(di, "ocr_page_block_sync", return_value=block):
            out = pdf_report._ocr_page_selection_di("/tmp/x.pdf", selected)

        assert sorted(out) == [3, 9, 40]
        # Arithmetic remap (first_page + local - 1) would give 3, 4, 5 --
        # page 9's text filed under page 4, and page 40 lost entirely.
        assert out[3].text == "PAGE-3"
        assert out[9].text == "PAGE-9"
        assert out[40].text == "PAGE-40"

    def test_the_selection_is_deduplicated_and_ordered(self) -> None:
        with patch.object(pdf_report, "_di_budget_take", return_value=True) as budget, \
             patch.object(pdf_report, "_slice_page_selection_pdf_bytes", return_value=b"%PDF") as slicer, \
             patch.object(di, "ocr_page_block_sync", return_value={}):
            pdf_report._ocr_page_selection_di("/tmp/x.pdf", [9, 3, 9, 40])

        # Sorted and deduped before slicing: the remap indexes by position,
        # so an unsorted or duplicated selection would misfile text.
        assert slicer.call_args[0][1] == [3, 9, 40]
        # Billed for three pages, not four.
        assert budget.call_args[0][1] == 3


class TestFailureModes:
    def test_an_exhausted_budget_sends_nothing(self) -> None:
        with patch.object(pdf_report, "_di_budget_take", return_value=False), \
             patch.object(di, "ocr_page_block_sync") as sync:
            out = pdf_report._ocr_page_selection_di("/tmp/x.pdf", [3, 9])

        assert out == {}
        sync.assert_not_called()

    def test_a_failed_slice_degrades_to_an_empty_mapping(self) -> None:
        # The caller reads {} as "drive these pages individually", so a
        # slice failure must not raise into the parse.
        with patch.object(pdf_report, "_di_budget_take", return_value=True), \
             patch.object(
                 pdf_report,
                 "_slice_page_selection_pdf_bytes",
                 side_effect=RuntimeError("pikepdf said no"),
             ), \
             patch.object(di, "ocr_page_block_sync") as sync:
            out = pdf_report._ocr_page_selection_di("/tmp/x.pdf", [3, 9])

        assert out == {}
        sync.assert_not_called()

    def test_an_empty_selection_costs_nothing(self) -> None:
        with patch.object(pdf_report, "_di_budget_take") as budget:
            assert pdf_report._ocr_page_selection_di("/tmp/x.pdf", []) == {}
        budget.assert_not_called()

    def test_a_page_di_never_mentioned_is_absent_not_empty(self) -> None:
        # Absent means "the batch did not answer" -> per-page path WITH its
        # own DI request. Present-but-empty means "DI looked and found
        # nothing" -> skip the second billed request. Conflating them
        # either doubles the bill or skips a page that was never tried.
        with patch.object(pdf_report, "_di_budget_take", return_value=True), \
             patch.object(pdf_report, "_slice_page_selection_pdf_bytes", return_value=b"%PDF"), \
             patch.object(
                 di, "ocr_page_block_sync",
                 return_value={1: di.PageOcrResult("", 0.0)},
             ):
            out = pdf_report._ocr_page_selection_di("/tmp/x.pdf", [3, 9])

        assert 3 in out and out[3].text == ""
        assert 9 not in out

    def test_a_result_index_outside_the_selection_is_dropped(self) -> None:
        # Defensive: DI returning a page number the upload does not have
        # would otherwise IndexError inside the parse.
        with patch.object(pdf_report, "_di_budget_take", return_value=True), \
             patch.object(pdf_report, "_slice_page_selection_pdf_bytes", return_value=b"%PDF"), \
             patch.object(
                 di, "ocr_page_block_sync",
                 return_value={1: di.PageOcrResult("ok", 0.9), 7: di.PageOcrResult("?", 0.9)},
             ):
            out = pdf_report._ocr_page_selection_di("/tmp/x.pdf", [3, 9])

        assert sorted(out) == [3]


class TestTheBudgetIsRefundedForPagesNeverSent:
    """A block is charged up front and can then answer for fewer pages.

    The charge has to happen before the request, because the check and the
    increment must be atomic across the OCR threads. But every page the
    request did not answer for goes back to the per-page path and is
    charged a SECOND time -- so one failed slice spent twice its pages of
    a cap whose whole purpose is to bound spend, and a document could
    report "budget exhausted" (and lose every table past the cap to
    tesseract) having sent a fraction of it.
    """

    @pytest.fixture(autouse=True)
    def _clean_budget(self):
        for registry in (
            pdf_report._DI_PAGES_USED,
            pdf_report._DI_CAP_LOGGED,
            pdf_report._DI_CAP_EXHAUSTED,
        ):
            registry.clear()
        yield
        for registry in (
            pdf_report._DI_PAGES_USED,
            pdf_report._DI_CAP_LOGGED,
            pdf_report._DI_CAP_EXHAUSTED,
        ):
            registry.clear()

    def test_a_failed_slice_costs_nothing(self) -> None:
        with patch.object(
            pdf_report,
            "_slice_page_selection_pdf_bytes",
            side_effect=RuntimeError("pikepdf said no"),
        ), patch.object(di, "ocr_page_block_sync") as sync:
            assert pdf_report._ocr_page_selection_di("/x.pdf", [3, 9, 40]) == {}

        sync.assert_not_called()
        assert pdf_report._DI_PAGES_USED["/x.pdf"] == 0

    def test_a_failed_request_costs_nothing(self) -> None:
        # ocr_page_block_sync degrades to {} on a failed or timed-out
        # analysis -- one Azure does not bill for either.
        with patch.object(
            pdf_report, "_slice_page_selection_pdf_bytes", return_value=b"%PDF"
        ), patch.object(di, "ocr_page_block_sync", return_value={}):
            assert pdf_report._ocr_page_selection_di("/x.pdf", [3, 9]) == {}

        assert pdf_report._DI_PAGES_USED["/x.pdf"] == 0

    def test_only_the_pages_it_answered_for_stay_charged(self) -> None:
        with patch.object(
            pdf_report, "_slice_page_selection_pdf_bytes", return_value=b"%PDF"
        ), patch.object(
            di, "ocr_page_block_sync", return_value={1: di.PageOcrResult("ok", 0.9)}
        ):
            out = pdf_report._ocr_page_selection_di("/x.pdf", [3, 9, 40])

        assert sorted(out) == [3]
        # Pages 9 and 40 are re-driven individually and pay there.
        assert pdf_report._DI_PAGES_USED["/x.pdf"] == 1

    def test_a_fully_answered_block_keeps_its_whole_charge(self) -> None:
        answered = {
            local: di.PageOcrResult(f"p{local}", 0.9) for local in (1, 2, 3)
        }
        with patch.object(
            pdf_report, "_slice_page_selection_pdf_bytes", return_value=b"%PDF"
        ), patch.object(di, "ocr_page_block_sync", return_value=answered):
            pdf_report._ocr_page_selection_di("/x.pdf", [3, 9, 40])

        assert pdf_report._DI_PAGES_USED["/x.pdf"] == 3
