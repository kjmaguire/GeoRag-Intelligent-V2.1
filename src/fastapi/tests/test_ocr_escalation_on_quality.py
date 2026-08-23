"""The OCR escalation ladder fired on an empty page, never on an unusable one.

`_ocr_single_page` computed a full quality assessment for whatever Document
Intelligence returned and then branched on `result.text.strip()` alone. So a
page that came back as fragmented tokens at catastrophic confidence -- the
ordinary outcome for a skewed 1970s plan sheet, and precisely the page the
tiled escalation exists for -- was accepted, stored, embedded and cited,
while the escalation sat one branch away. The assessment was attached to the
return value and read by nobody at that point.

The router already had a word for this: `catastrophic_failure`. It is
reached by empty output, by mean confidence at or below
`catastrophic_max_mean_confidence`, or by output coverage at or below
`catastrophic_max_coverage_ratio` -- and the latter two only when routing
thresholds are calibrated. That is what makes reading the tier safe rather
than expensive: on an uncalibrated deployment every page routes to
`mandatory_review` by design, so escalating on THAT would fire four or more
billed DI requests per page of every document.
"""

from __future__ import annotations

import json

import pytest

from app.services.ingest.ocr_quality import (
    assess_ocr_quality,
    calculate_ocr_quality,
    load_routing_thresholds_from_env,
)
from app.services.ingest.pdf_report import _is_catastrophic_assessment

CALIBRATED = {
    "catastrophic_max_mean_confidence": 0.35,
    "catastrophic_max_coverage_ratio": 0.05,
    "mandatory_min_mean_confidence": 0.70,
    "mandatory_min_median_confidence": 0.70,
    "mandatory_max_low_confidence_word_ratio": 0.30,
    "mandatory_min_coverage_ratio": 0.50,
    "mandatory_max_gibberish_word_ratio": 0.30,
    "mandatory_max_repeated_character_ratio": 0.05,
    "mandatory_max_seam_duplicate_ratio": 0.10,
    "spot_check_min_mean_confidence": 0.85,
    "spot_check_min_median_confidence": 0.85,
    "spot_check_max_low_confidence_word_ratio": 0.10,
    "spot_check_min_coverage_ratio": 0.80,
    "spot_check_max_gibberish_word_ratio": 0.10,
    "spot_check_max_repeated_character_ratio": 0.02,
    "spot_check_max_seam_duplicate_ratio": 0.05,
}

# What a 3-degree skew does to a page of text under `--psm 3`.
SHREDDED = "Th e re su lt s of th e dr il li ng pr og ra m me"
CLEAN = "The results of the drilling programme are summarised in Table 14-1."


def _assess(text: str, confidence: float, thresholds) -> dict:
    signals = calculate_ocr_quality(
        text, [confidence] * 12, detected_region_count=12
    )
    return {"tier": assess_ocr_quality(signals, thresholds).tier.value}


@pytest.fixture
def calibrated(monkeypatch):
    monkeypatch.setenv("OCR_ROUTING_THRESHOLDS_JSON", json.dumps(CALIBRATED))
    return load_routing_thresholds_from_env()


class TestTheEscalationTrigger:
    def test_a_shredded_page_now_escalates(self, calibrated) -> None:
        assert _is_catastrophic_assessment(_assess(SHREDDED, 0.18, calibrated))

    def test_a_clean_page_does_not(self, calibrated) -> None:
        assert not _is_catastrophic_assessment(_assess(CLEAN, 0.96, calibrated))

    def test_an_empty_page_still_does(self, calibrated) -> None:
        assert _is_catastrophic_assessment(_assess("", 0.0, calibrated))

    def test_mandatory_review_is_deliberately_not_a_trigger(self) -> None:
        """The cost guard.

        With no calibration every page routes to mandatory_review, so
        treating that as an escalation signal would tile every page of
        every document -- four or more billed DI requests each.
        """
        uncalibrated = _assess(SHREDDED, 0.18, None)

        assert uncalibrated["tier"] == "mandatory_review"
        assert not _is_catastrophic_assessment(uncalibrated)

    def test_an_absent_assessment_is_not_a_trigger(self) -> None:
        assert not _is_catastrophic_assessment(None)
        assert not _is_catastrophic_assessment({})


class TestTheLadderKeepsItsFloor:
    """Escalating must never lose the read it started from."""

    @staticmethod
    def _ladder_source() -> str:
        import inspect

        from app.services.ingest.pdf_report import _ocr_single_page

        return " ".join(inspect.getsource(_ocr_single_page).split())

    def test_a_catastrophic_di_read_is_kept_when_tiles_do_no_better(self) -> None:
        """DI is the only one of the two that carries table structure.

        Falling through to tesseract here would trade a poor read that has
        tables for a poor read that has none.
        """
        src = self._ladder_source()

        # Structure, not prose: after the tiled attempt the DI read is
        # returned rather than falling through to the tesseract block.
        assert "if di_assessment is not None:" in src
        assert "routing it to review" in src

    def test_an_empty_di_read_still_takes_any_tiled_text(self) -> None:
        """The pre-existing contract for the empty case is unchanged.

        Requiring tiles to clear the quality bar when DI returned nothing
        would be a regression: any text beats no text.
        """
        src = self._ladder_source()

        assert "di_assessment is None or not _is_catastrophic_assessment(" in src


class TestTheBatchedShortCircuitUsesTheSameTrigger:
    """The batched paths had the bug this module fixed, one level up.

    Batching (2026-08-20 for whole scans, 2026-08-23 for mixed documents)
    added two more places where a Document Intelligence read is accepted,
    and both accepted it on `text.strip()` alone -- computing the very
    assessment `_ocr_single_page` reads and then only attaching it to the
    return value. So the fix above held for a page DI was asked about
    individually and not for the same page inside a block, which on a
    scanned report is every page.

    `_usable_batched_page` is now the single decision both callers make.
    """

    @staticmethod
    def _page(text: str, confidence: float):
        from app.services.ingest.document_intelligence_client import PageOcrResult

        return PageOcrResult(
            text,
            confidence,
            detected_region_count=12,
            tables=[[["Au g/t", "1.2"]]],
        )

    def _usable(self, text: str, confidence: float):
        from app.services.ingest.pdf_report import _usable_batched_page

        return _usable_batched_page(
            self._page(text, confidence), page_num=7, pdf_path="/scan.pdf"
        )

    def test_a_shredded_batched_page_is_not_accepted(self, calibrated) -> None:
        # None means "drive this page individually", which is where the
        # bounded raster tiles are. Before this, the shredded read was
        # returned and stored.
        assert self._usable(SHREDDED, 0.18) is None

    def test_a_clean_batched_page_is_accepted_with_its_tables(
        self, calibrated
    ) -> None:
        usable = self._usable(CLEAN, 0.96)

        assert usable is not None
        text, confidence, assessment, tables = usable
        assert text == CLEAN
        assert confidence == 0.96
        # The tables are the reason the DI read is worth keeping at all --
        # the tesseract fallback carries none.
        assert tables == [[["Au g/t", "1.2"]]]
        assert assessment["ocr_method"] == "document_intelligence"

    def test_an_empty_batched_page_is_not_accepted(self, calibrated) -> None:
        assert self._usable("", 0.0) is None

    def test_a_page_the_batch_never_answered_is_not_accepted(self) -> None:
        from app.services.ingest.pdf_report import _usable_batched_page

        assert _usable_batched_page(None, page_num=7, pdf_path="/scan.pdf") is None

    def test_an_uncalibrated_deployment_still_accepts_a_poor_read(self) -> None:
        """The cost guard, restated on this path.

        With no thresholds every page routes to mandatory_review. If that
        counted as unusable, batching would fall through to per-page tiles
        for every page of every document -- the opposite of what batching
        is for.
        """
        assert self._usable(SHREDDED, 0.18) is not None


@pytest.fixture
def stub_page_count():
    """pdf2image.pdfinfo_from_path without requiring pdf2image.

    The function under test imports it lazily and the poppler wrapper is
    absent from some images.
    """
    import sys
    import types

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


class TestABatchedPageReachesTheLadder:
    """End to end: the block's answer, the trigger, and the escalation.

    The unit test above proves `_usable_batched_page` says no. This proves
    the caller then does the thing saying no is FOR -- re-drives the page
    through `_ocr_single_page`, which starts at the bounded raster tiles --
    and that it does not do so for the clean page beside it, which would
    turn one billed request per block back into one per page.
    """

    def test_a_shredded_block_page_is_re_driven_and_a_clean_one_is_not(
        self, calibrated, stub_page_count, monkeypatch
    ) -> None:
        from unittest.mock import patch

        from app.services.ingest import pdf_report
        from app.services.ingest.document_intelligence_client import PageOcrResult

        stub_page_count(2)
        block = {
            1: PageOcrResult(SHREDDED, 0.18, detected_region_count=12),
            2: PageOcrResult(CLEAN, 0.96, detected_region_count=12),
        }
        rescued: list[tuple[int, bool]] = []

        def fake_single(_path, page_num, **kwargs):
            rescued.append((page_num, kwargs.get("skip_di_page_request", False)))
            return (
                f"TILED-{page_num}",
                0.91,
                pdf_report._assess_ocr_result(
                    CLEAN, [0.91] * 12,
                    detected_region_count=12,
                    ocr_method="document_intelligence",
                ),
                [],
            )

        with patch.object(pdf_report, "_ocr_page_block_di", return_value=block), \
             patch.object(pdf_report, "_ocr_single_page", side_effect=fake_single), \
             patch.object(pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t):
            result = pdf_report._attempt_ocr_document_intelligence("/scan.pdf")

        # Page 1 only, and without a second billed DI page request: the
        # block already paid for it.
        assert rescued == [(1, True)]
        assert result.pages[0].text == "TILED-1"
        assert result.pages[1].text == CLEAN
