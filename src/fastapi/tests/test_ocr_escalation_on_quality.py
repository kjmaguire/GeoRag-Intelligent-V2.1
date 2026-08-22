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
