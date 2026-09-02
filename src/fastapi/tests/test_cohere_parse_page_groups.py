"""Grouped Cohere Parse requests on the whole-document and mixed paths.

Ported from the Document Intelligence block/sparse batching tests when the
engine changed (ADR-0019). Parse takes one page per request, so a "batch"
is now a group of pages rendered together and posted concurrently by the
client; what the parser pins is unchanged:

  1. Blocks are planned to cover every page exactly once, in order.
  2. The whole-document path assembles in page order.
  3. Recovery is layered, and the two failure modes are NOT the same:
       - group ran, page came back empty  -> per-page path WITHOUT a
         second billed request (straight to tesseract);
       - group never answered for a page  -> per-page path WITH its own
         request, i.e. exactly the pre-grouping behaviour.
  4. The budget is charged for the pages sent and refunded for the pages
     the group did not answer for.
  5. The engine keys its mapping by ABSOLUTE page number, so the
     positional remap the upload path needed cannot misfile text.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import patch

import pytest

from app.services.ingest import cohere_parse_client as cpc
from app.services.ingest import pdf_report
from app.services.ingest.ocr_types import PageOcrResult


def _page(text: str, **kw) -> PageOcrResult:
    return PageOcrResult(text, 0.0, confidence_reported=False, **kw)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OCR_PAGES_PER_BATCH", raising=False)
    monkeypatch.setenv("OCR_ENGINE", "cohere_parse")
    # The selection path runs only for a selected AND configured engine.
    monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", "https://foundry.example.invalid")
    monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "k")
    monkeypatch.setenv("AZURE_FOUNDRY_PARSE_DEPLOYMENT", "Cohere-parse-v5")


@pytest.fixture
def stub_page_count():
    """Provide pdf2image.pdfinfo_from_path without requiring pdf2image."""

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


class TestSkipEnginePageRequest:
    def test_skips_the_duplicate_billed_request(self) -> None:
        """A group already asked about this page; asking again costs a page
        to get the same empty answer."""
        with patch.object(
            pdf_report, "_engine_single_page_request"
        ) as single, patch.object(pdf_report, "_ocr_budget_take") as budget, patch.dict(
            "sys.modules", {"pytesseract": None, "pdf2image": None}
        ):
            pdf_report._ocr_single_page(
                "/nonexistent.pdf",
                4,
                skip_engine_page_request=True,
            )

        single.assert_not_called()
        # The group already paid for this page — charging the per-document
        # budget twice would shrink the effective cap.
        budget.assert_not_called()

    def test_without_the_flag_the_request_still_happens(self) -> None:
        with patch.object(
            pdf_report,
            "_engine_single_page_request",
            return_value=_page("", request_succeeded=False, error="x"),
        ) as single, patch.object(
            pdf_report, "_ocr_budget_take", return_value=True
        ), patch.dict(
            "sys.modules", {"pytesseract": None, "pdf2image": None}
        ):
            pdf_report._ocr_single_page("/nonexistent.pdf", 4)

        single.assert_called_once()

    def test_an_exhausted_budget_skips_the_engine(self) -> None:
        with patch.object(
            pdf_report, "_engine_single_page_request"
        ) as single, patch.object(
            pdf_report, "_ocr_budget_take", return_value=False
        ), patch.dict(
            "sys.modules", {"pytesseract": None, "pdf2image": None}
        ):
            out = pdf_report._ocr_single_page(
                "/nonexistent.pdf",
                4,
                return_confidence=True,
                return_assessment=True,
            )

        single.assert_not_called()
        assert out[2]["ocr_method"] == "unavailable"

    def test_a_non_empty_engine_page_is_returned_with_null_confidence(self) -> None:
        grid = [["Hole", "Au"], ["DDH-1", "1.2"]]
        with patch.object(
            pdf_report,
            "_engine_single_page_request",
            return_value=_page(
                "Drill hole DDH-1 intersected mineralised quartz.", tables=[grid]
            ),
        ), patch.object(pdf_report, "_ocr_budget_take", return_value=True):
            text, confidence, assessment, tables = pdf_report._ocr_single_page(
                "/nonexistent.pdf",
                4,
                return_confidence=True,
                return_assessment=True,
                return_tables=True,
            )

        assert text.startswith("Drill hole")
        assert confidence == 0.0
        assert assessment["ocr_method"] == "cohere_parse"
        assert assessment["signals"]["confidence_reported"] is False
        assert tables == [grid]
        assert pdf_report._reported_confidence(assessment, confidence) is None

    def test_not_configured_is_one_critical_per_process_and_no_budget(
        self, monkeypatch, caplog
    ) -> None:
        import logging

        monkeypatch.delenv("AZURE_FOUNDRY_PARSE_DEPLOYMENT")
        monkeypatch.setattr(pdf_report, "_ENGINE_NOT_CONFIGURED_WARNED", False)

        with patch.object(
            pdf_report, "_engine_single_page_request"
        ) as single, patch.object(pdf_report, "_ocr_budget_take") as budget, patch.dict(
            "sys.modules", {"pytesseract": None, "pdf2image": None}
        ), caplog.at_level(
            logging.CRITICAL
        ):
            for page in (1, 2, 3):
                out = pdf_report._ocr_single_page(
                    "/nonexistent.pdf",
                    page,
                    return_confidence=True,
                    return_assessment=True,
                )

        single.assert_not_called()
        budget.assert_not_called()
        critical = [r for r in caplog.records if r.levelno == logging.CRITICAL]
        assert len(critical) == 1
        assert "AZURE_FOUNDRY_PARSE_DEPLOYMENT" in critical[0].getMessage()
        assert out[2]["ocr_method"] == "unavailable"

    def test_a_raised_not_configured_is_still_critical_and_refunded(
        self, monkeypatch, caplog
    ) -> None:
        import logging

        monkeypatch.setattr(pdf_report, "_ENGINE_NOT_CONFIGURED_WARNED", False)
        with patch.object(
            pdf_report,
            "_engine_single_page_request",
            side_effect=cpc.CohereParseNotConfigured("no key"),
        ), patch.object(
            pdf_report, "_ocr_budget_take", return_value=True
        ), patch.object(
            pdf_report, "_ocr_budget_refund"
        ) as refund, patch.dict(
            "sys.modules", {"pytesseract": None, "pdf2image": None}
        ), caplog.at_level(
            logging.CRITICAL
        ):
            pdf_report._ocr_single_page("/nonexistent.pdf", 4)

        assert any(r.levelno == logging.CRITICAL for r in caplog.records)
        refund.assert_called_once_with("/nonexistent.pdf", 1)

    def test_a_failed_request_refunds_the_page_it_charged(self) -> None:
        with patch.object(
            pdf_report,
            "_engine_single_page_request",
            return_value=_page("", request_succeeded=False, error="503: down"),
        ), patch.object(
            pdf_report, "_ocr_budget_take", return_value=True
        ), patch.object(
            pdf_report, "_ocr_budget_refund"
        ) as refund, patch.dict(
            "sys.modules", {"pytesseract": None, "pdf2image": None}
        ):
            pdf_report._ocr_single_page("/nonexistent.pdf", 4)

        refund.assert_called_once_with("/nonexistent.pdf", 1)

    def test_a_successful_request_keeps_its_charge(self) -> None:
        with patch.object(
            pdf_report,
            "_engine_single_page_request",
            return_value=_page("text on the page"),
        ), patch.object(
            pdf_report, "_ocr_budget_take", return_value=True
        ), patch.object(
            pdf_report, "_ocr_budget_refund"
        ) as refund:
            pdf_report._ocr_single_page("/nonexistent.pdf", 4)

        refund.assert_not_called()


class TestWholeDocumentGrouping:
    """The reason for grouping: request count on the full-scan path."""

    def _run(self, path: str = "/scan.pdf"):
        return pdf_report._attempt_ocr_cohere_parse(path)

    def test_one_group_per_block_not_per_page(
        self, stub_page_count, monkeypatch
    ) -> None:
        monkeypatch.setenv("OCR_PAGES_PER_BATCH", "25")
        stub_page_count(50)
        calls: list[tuple[int, int]] = []

        def fake_block(_path, first_page, page_count):
            calls.append((first_page, page_count))
            return {
                first_page + o: _page(f"PAGE-{first_page + o}")
                for o in range(page_count)
            }

        with patch.object(
            pdf_report, "_ocr_page_block", side_effect=fake_block
        ), patch.object(pdf_report, "_ocr_single_page") as per_page, patch.object(
            pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t
        ):
            result = self._run()

        assert calls == [(1, 25), (26, 25)]
        per_page.assert_not_called()
        assert [p.page_number for p in result.pages] == list(range(1, 51))
        assert [p.text for p in result.pages] == [f"PAGE-{n}" for n in range(1, 51)]

    def test_grouped_pages_keep_cohere_parse_provenance(self, stub_page_count) -> None:
        """parser_used is derived from per-page ocr_method; a grouped page
        that lost its label would downgrade the whole doc to 'ocr_mixed'."""
        stub_page_count(3)

        with patch.object(
            pdf_report,
            "_ocr_page_block",
            side_effect=lambda _p, first, count: {
                first + o: _page(f"PAGE-{first + o}") for o in range(count)
            },
        ), patch.object(pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t):
            result = self._run()

        assert result.parser_used == "ocr_cohere_parse"
        assert all(
            page.assessment["ocr_method"] == "cohere_parse" for page in result.pages
        )
        assert all(
            page.assessment["signals"]["confidence_reported"] is False
            for page in result.pages
        )

    def test_empty_page_in_a_good_group_recovers_without_a_second_call(
        self, stub_page_count
    ) -> None:
        stub_page_count(3)
        seen: dict[int, bool] = {}

        def fake_single(_path, page_num, **kwargs):
            seen[page_num] = kwargs["skip_engine_page_request"]
            return (
                f"TESS-{page_num}",
                0.5,
                pdf_report._assess_ocr_result(
                    f"TESS-{page_num}", [0.5], detected_region_count=1
                ),
                [],
            )

        with patch.object(
            pdf_report,
            "_ocr_page_block",
            return_value={1: _page("PAGE-1"), 2: _page(""), 3: _page("PAGE-3")},
        ), patch.object(
            pdf_report, "_ocr_single_page", side_effect=fake_single
        ), patch.object(
            pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t
        ):
            result = self._run()

        assert seen == {2: True}, "only the blank page should be re-driven"
        assert [p.text for p in result.pages] == ["PAGE-1", "TESS-2", "PAGE-3"]

    def test_failed_group_falls_back_to_the_pre_grouping_behaviour(
        self, stub_page_count
    ) -> None:
        stub_page_count(4)
        seen: dict[int, bool] = {}

        def fake_single(_path, page_num, **kwargs):
            seen[page_num] = kwargs["skip_engine_page_request"]
            return (
                f"PAGE-{page_num}",
                0.0,
                pdf_report._assess_ocr_result(
                    f"PAGE-{page_num}",
                    None,
                    detected_region_count=0,
                    ocr_method="cohere_parse",
                ),
                [],
            )

        with patch.object(pdf_report, "_ocr_page_block", return_value={}), patch.object(
            pdf_report, "_ocr_single_page", side_effect=fake_single
        ), patch.object(pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t):
            result = self._run()

        assert seen == {1: False, 2: False, 3: False, 4: False}
        assert [p.page_number for p in result.pages] == [1, 2, 3, 4]

    def test_batch_size_one_bypasses_the_group_pass_entirely(
        self, stub_page_count, monkeypatch
    ) -> None:
        monkeypatch.setenv("OCR_PAGES_PER_BATCH", "1")
        stub_page_count(3)

        def fake_single(_path, page_num, **kwargs):
            assert kwargs["skip_engine_page_request"] is False
            return (
                f"PAGE-{page_num}",
                0.9,
                pdf_report._assess_ocr_result(
                    f"PAGE-{page_num}", [0.9], detected_region_count=1
                ),
                [],
            )

        with patch.object(pdf_report, "_ocr_page_block") as block, patch.object(
            pdf_report, "_ocr_single_page", side_effect=fake_single
        ), patch.object(pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t):
            result = self._run()

        block.assert_not_called()
        assert [p.page_number for p in result.pages] == [1, 2, 3]

    def test_group_tables_survive_onto_their_page(self, stub_page_count) -> None:
        stub_page_count(2)
        grid = [["Hole", "Au g/t"], ["DDH-1", "1.2"]]

        with patch.object(
            pdf_report,
            "_ocr_page_block",
            return_value={1: _page("PAGE-1"), 2: _page("PAGE-2", tables=[grid])},
        ), patch.object(pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t):
            result = self._run()

        assert result.pages[0].tables == ()
        assert result.pages[1].tables == (grid,)


class TestSelectionAndBudget:
    @pytest.fixture(autouse=True)
    def _clean_budget(self):
        for registry in (
            pdf_report._OCR_PAGES_USED,
            pdf_report._OCR_CAP_LOGGED,
            pdf_report._OCR_CAP_EXHAUSTED,
        ):
            registry.clear()
        yield
        for registry in (
            pdf_report._OCR_PAGES_USED,
            pdf_report._OCR_CAP_LOGGED,
            pdf_report._OCR_CAP_EXHAUSTED,
        ):
            registry.clear()

    def test_pages_after_a_gap_keep_their_own_text(self) -> None:
        block = {3: _page("PAGE-3"), 9: _page("PAGE-9"), 40: _page("PAGE-40")}

        with patch.object(cpc, "ocr_page_block_sync", return_value=block):
            out = pdf_report._ocr_page_selection("/tmp/x.pdf", [3, 9, 40])

        assert sorted(out) == [3, 9, 40]
        assert out[3].text == "PAGE-3"
        assert out[9].text == "PAGE-9"
        assert out[40].text == "PAGE-40"

    def test_the_selection_is_deduplicated_and_ordered(self) -> None:
        with patch.object(cpc, "ocr_page_block_sync", return_value={}) as sync:
            pdf_report._ocr_page_selection("/tmp/x.pdf", [9, 3, 9, 40])

        assert sync.call_args[0][1] == [3, 9, 40]

    def test_a_block_charges_the_budget_for_every_page_it_covers(self) -> None:
        taken: list[int] = []

        with patch.object(
            pdf_report,
            "_ocr_budget_take",
            side_effect=lambda _p, pages: taken.append(pages) or True,
        ), patch.object(cpc, "ocr_page_block_sync", return_value={26: _page("x")}):
            pdf_report._ocr_page_block("/scan.pdf", 26, 25)

        assert taken == [25]

    def test_an_exhausted_budget_sends_nothing(self) -> None:
        with patch.object(
            pdf_report, "_ocr_budget_take", return_value=False
        ), patch.object(cpc, "ocr_page_block_sync") as sync:
            assert pdf_report._ocr_page_selection("/tmp/x.pdf", [3, 9]) == {}

        sync.assert_not_called()

    def test_an_empty_selection_costs_nothing(self) -> None:
        with patch.object(pdf_report, "_ocr_budget_take") as budget:
            assert pdf_report._ocr_page_selection("/tmp/x.pdf", []) == {}

        budget.assert_not_called()

    def test_a_page_the_engine_never_answered_is_absent_not_empty(self) -> None:
        with patch.object(cpc, "ocr_page_block_sync", return_value={3: _page("")}):
            out = pdf_report._ocr_page_selection("/tmp/x.pdf", [3, 9])

        assert 3 in out and out[3].text == ""
        assert 9 not in out

    def test_a_page_outside_the_selection_is_dropped(self) -> None:
        with patch.object(
            cpc, "ocr_page_block_sync", return_value={3: _page("ok"), 7: _page("?")}
        ):
            out = pdf_report._ocr_page_selection("/tmp/x.pdf", [3, 9])

        assert sorted(out) == [3]

    def test_a_failed_group_costs_nothing(self) -> None:
        with patch.object(cpc, "ocr_page_block_sync", return_value={}):
            assert pdf_report._ocr_page_selection("/x.pdf", [3, 9]) == {}

        assert pdf_report._OCR_PAGES_USED["/x.pdf"] == 0

    def test_only_the_pages_it_answered_for_stay_charged(self) -> None:
        with patch.object(cpc, "ocr_page_block_sync", return_value={3: _page("ok")}):
            out = pdf_report._ocr_page_selection("/x.pdf", [3, 9, 40])

        assert sorted(out) == [3]
        assert pdf_report._OCR_PAGES_USED["/x.pdf"] == 1

    def test_a_fully_answered_group_keeps_its_whole_charge(self) -> None:
        answered = {n: _page(f"p{n}") for n in (3, 9, 40)}
        with patch.object(cpc, "ocr_page_block_sync", return_value=answered):
            pdf_report._ocr_page_selection("/x.pdf", [3, 9, 40])

        assert pdf_report._OCR_PAGES_USED["/x.pdf"] == 3

    def test_an_unselected_engine_sends_nothing_and_charges_nothing(
        self, monkeypatch
    ) -> None:
        """Foundry credentials are shared with the embedder; they are not consent."""
        monkeypatch.setenv("OCR_ENGINE", "tesseract")
        with patch.object(cpc, "ocr_page_block_sync") as sync:
            assert pdf_report._ocr_page_selection("/x.pdf", [3, 9]) == {}

        sync.assert_not_called()
        assert "/x.pdf" not in pdf_report._OCR_PAGES_USED
