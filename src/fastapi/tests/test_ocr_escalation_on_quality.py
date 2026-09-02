"""The OCR ladder falls through on an unusable page, never accepts it silently.

`_ocr_single_page` computes a full quality assessment for whatever the
remote engine returned and reads its tier before accepting the page. The
router's word for unusable is `catastrophic_failure`: reached by empty
output, by mean confidence at or below `catastrophic_max_mean_confidence`,
or by output coverage at or below `catastrophic_max_coverage_ratio` — and
the latter two only when routing thresholds are calibrated, and the
confidence one only when the engine reports confidence at all.

Since ADR-0019 the engine is Cohere Parse, which reports NO confidence.
That collapses the catastrophic tier for Parse pages to "empty output", and
it is what lets the ladder lose its former middle rung (bounded raster
tiles reconstructed from word polygons, which Parse cannot supply): a Parse
page is either non-empty — accepted, with its assessment attached so the
router can still route it to review on content signals — or empty/failed,
in which case tesseract is the floor.

What is pinned here:

  1. The trigger: catastrophic is a trigger, mandatory_review is not (the
     cost guard: an uncalibrated deployment routes EVERY page to review).
  2. The ladder has exactly two rungs: engine, then tesseract.
  3. The grouped short-circuit (`_usable_batched_page`) makes the same
     decision as the per-page path, and a page it declines is re-driven
     WITHOUT a second billed request.
"""

from __future__ import annotations

import json

import pytest

from app.services.ingest.ocr_quality import (
    assess_ocr_quality,
    calculate_ocr_quality,
    load_routing_thresholds_from_env,
)
from app.services.ingest.ocr_types import PageOcrResult
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
# Repeated-character smear: trips the content rules without any confidence.
SMEARED = "aaaaaa bbbbbb cccccc dddddd eeeeee ffffff"


def _assess(text: str, confidence: float | None, thresholds) -> dict:
    signals = calculate_ocr_quality(
        text,
        None if confidence is None else [confidence] * 12,
        detected_region_count=0 if confidence is None else 12,
    )
    return {"tier": assess_ocr_quality(signals, thresholds).tier.value}


@pytest.fixture
def calibrated(monkeypatch):
    monkeypatch.setenv("OCR_ROUTING_THRESHOLDS_JSON", json.dumps(CALIBRATED))
    return load_routing_thresholds_from_env()


class TestTheEscalationTrigger:
    def test_a_shredded_page_with_confidence_escalates(self, calibrated) -> None:
        """Tesseract still reports confidence; the old trigger still fires for it."""
        assert _is_catastrophic_assessment(_assess(SHREDDED, 0.18, calibrated))

    def test_a_clean_page_does_not(self, calibrated) -> None:
        assert not _is_catastrophic_assessment(_assess(CLEAN, 0.96, calibrated))

    def test_an_empty_page_still_does(self, calibrated) -> None:
        assert _is_catastrophic_assessment(_assess("", 0.0, calibrated))
        assert _is_catastrophic_assessment(_assess("", None, calibrated))

    def test_without_confidence_only_empty_output_is_catastrophic(self, calibrated) -> None:
        """Parse pages have no confidence to be catastrophic ON.

        A smeared read is still routed to review — by the content rules —
        but it is kept, because the only engine below it is tesseract and
        that read carries no table structure.
        """
        assert not _is_catastrophic_assessment(_assess(SMEARED, None, calibrated))
        assert _assess(SMEARED, None, calibrated)["tier"] == "mandatory_review"

    def test_mandatory_review_is_deliberately_not_a_trigger(self) -> None:
        """The cost guard.

        With no calibration every page routes to mandatory_review, so
        treating that as an escalation signal would re-drive every page of
        every document through the fallback engine.
        """
        uncalibrated = _assess(SHREDDED, 0.18, None)

        assert uncalibrated["tier"] == "mandatory_review"
        assert not _is_catastrophic_assessment(uncalibrated)

    def test_an_absent_assessment_is_not_a_trigger(self) -> None:
        assert not _is_catastrophic_assessment(None)
        assert not _is_catastrophic_assessment({})


class TestTheLadderHasTwoRungs:
    """Structure, not prose: engine → tesseract, nothing in between."""

    @staticmethod
    def _ladder_source() -> str:
        import inspect

        from app.services.ingest.pdf_report import _ocr_single_page

        return " ".join(inspect.getsource(_ocr_single_page).split())

    def test_a_failed_request_falls_through_to_tesseract(self) -> None:
        src = self._ladder_source()

        assert "if not result.request_succeeded:" in src
        assert "falling back to tesseract" in src

    def test_the_tiled_rung_is_gone(self) -> None:
        """Parse returns no word polygons, so tiles cannot be reconstructed."""
        import app.services.ingest.pdf_report as pdf_report

        assert not hasattr(pdf_report, "_ocr_tiled_pdf_page")
        assert "tiled" not in self._ladder_source().replace("The Document Intelligence era had a third", "")

    def test_a_skipped_page_is_the_empty_sentinel_not_a_request(self, monkeypatch) -> None:
        """A group already paid for this page and got nothing back."""
        from unittest.mock import patch

        from app.services.ingest import pdf_report

        monkeypatch.setenv("OCR_ENGINE", "cohere_parse")
        monkeypatch.setenv("AZURE_FOUNDRY_ENDPOINT", "https://foundry.example.invalid")
        monkeypatch.setenv("AZURE_FOUNDRY_API_KEY", "k")
        monkeypatch.setenv("AZURE_FOUNDRY_PARSE_DEPLOYMENT", "Cohere-parse-v5")

        with patch.object(pdf_report, "_engine_single_page_request") as single, \
             patch.object(pdf_report, "_ocr_budget_take") as budget, \
             patch.dict("sys.modules", {"pytesseract": None, "pdf2image": None}):
            out = pdf_report._ocr_single_page(
                "/nonexistent.pdf", 4,
                return_confidence=True, return_assessment=True,
                skip_engine_page_request=True,
            )

        single.assert_not_called()
        budget.assert_not_called()
        # Tesseract is absent too, so the page comes back empty with the
        # engine's provenance — it DID run, inside the group.
        assert out[0] == ""
        assert out[2]["ocr_method"] == "cohere_parse"


class TestTheGroupedShortCircuitUsesTheSameTrigger:
    """`_usable_batched_page` is the single decision both callers make."""

    @staticmethod
    def _page(text: str, confidence: float | None):
        return PageOcrResult(
            text,
            0.0 if confidence is None else confidence,
            detected_region_count=0 if confidence is None else 12,
            tables=[[["Au g/t", "1.2"]]],
            confidence_reported=confidence is not None,
        )

    def _usable(self, text: str, confidence: float | None):
        from app.services.ingest.pdf_report import _usable_batched_page

        return _usable_batched_page(
            self._page(text, confidence), page_num=7, pdf_path="/scan.pdf"
        )

    def test_a_smeared_parse_page_is_accepted_and_routed_to_review(self, calibrated) -> None:
        """No confidence to escalate on; the content rules do the routing."""
        usable = self._usable(SMEARED, None)

        assert usable is not None
        _text, _confidence, assessment, _tables = usable
        assert assessment["tier"] == "mandatory_review"
        assert "repeated_character_ratio" in assessment["reasons"]
        assert assessment["signals"]["confidence_reported"] is False

    def test_a_clean_page_is_accepted_with_its_tables(self, calibrated) -> None:
        usable = self._usable(CLEAN, None)

        assert usable is not None
        text, confidence, assessment, tables = usable
        assert text == CLEAN
        assert confidence == 0.0
        # The tables are the reason the engine read is worth keeping at all
        # -- the tesseract fallback carries none.
        assert tables == [[["Au g/t", "1.2"]]]
        assert assessment["ocr_method"] == "cohere_parse"
        assert assessment["tier"] == "auto_accept"

    def test_an_empty_page_is_not_accepted(self, calibrated) -> None:
        assert self._usable("", None) is None

    def test_a_page_the_group_never_answered_is_not_accepted(self) -> None:
        from app.services.ingest.pdf_report import _usable_batched_page

        assert _usable_batched_page(None, page_num=7, pdf_path="/scan.pdf") is None

    def test_a_confidence_reporting_engine_on_this_seam_still_escalates(self, calibrated) -> None:
        """Guard for a future engine that does report confidence."""
        assert self._usable(SHREDDED, 0.18) is None

    def test_an_uncalibrated_deployment_still_accepts_a_poor_read(self) -> None:
        assert self._usable(SMEARED, None) is not None


@pytest.fixture
def stub_page_count():
    """pdf2image.pdfinfo_from_path without requiring pdf2image."""
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


class TestAGroupedPageReachesTheLadder:
    """End to end: the group's answer, the trigger, and the fall-through.

    An empty page in a good group is re-driven through `_ocr_single_page`
    with the skip flag (no second billed request); the non-empty page
    beside it is not re-driven at all.
    """

    def test_an_empty_group_page_is_re_driven_and_a_clean_one_is_not(
        self, calibrated, stub_page_count, monkeypatch
    ) -> None:
        from unittest.mock import patch

        from app.services.ingest import pdf_report

        stub_page_count(2)
        block = {
            1: PageOcrResult("", 0.0, confidence_reported=False),
            2: PageOcrResult(CLEAN, 0.0, confidence_reported=False),
        }
        rescued: list[tuple[int, bool]] = []

        def fake_single(_path, page_num, **kwargs):
            rescued.append((page_num, kwargs.get("skip_engine_page_request", False)))
            return (
                f"TESS-{page_num}",
                0.71,
                pdf_report._assess_ocr_result(
                    CLEAN, [0.71] * 12,
                    detected_region_count=12,
                    ocr_method="tesseract",
                ),
                [],
            )

        with patch.object(pdf_report, "_ocr_page_block", return_value=block), \
             patch.object(pdf_report, "_ocr_single_page", side_effect=fake_single), \
             patch.object(pdf_report, "_postprocess_ocr_text", side_effect=lambda t: t):
            result = pdf_report._attempt_ocr_cohere_parse("/scan.pdf")

        assert rescued == [(1, True)]
        assert result.pages[0].text == "TESS-1"
        assert result.pages[1].text == CLEAN
        assert result.parser_used == "ocr_mixed"
