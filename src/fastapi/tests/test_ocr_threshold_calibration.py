"""`thresholds_calibrated` must mean calibrated, and engines get their own bands.

WHY THIS FILE EXISTS
    Two defects in the OCR routing config, both of the same kind: the
    system reported something it had not established.

    1. ``thresholds_calibrated`` was literally ``thresholds is not None``.
       It said "a threshold set was supplied" while the class docstring
       says "Auto-accept behavior must be backed by measured corpus data".
       The set deployed in .env.example, docker-compose.yml and the live
       hatchet-worker-cc is an identical trio of round numbers
       (0.30 / 0.60 / 0.90) with no calibration artefact anywhere in the
       repo -- so every page assessment recorded calibrated=true, and the
       planned XGBoost quality classifier would have trained on those
       labels believing them evidence-backed.

    2. One threshold set spanned two engines whose confidence scales are
       not comparable. Document Intelligence reports 0.95-0.99 even when
       confidently wrong; Tesseract reports 0.70-0.85 when it is fine.
       With ``spot_check_min_mean_confidence = 0.90``, virtually every DI
       page auto-accepts and virtually every Tesseract page is routed to
       review -- the inverse of the intent, and a large part of why the
       review queue fills with pages that are probably fine while wrong DI
       pages pass silently.

WHAT IS NOT TESTED HERE, BECAUSE IT DOES NOT EXIST YET
    Whether the numbers are right. This file locks the SHAPE -- that a
    calibration claim costs someone an artefact, and that per-engine bands
    can be expressed. The numbers still have to come from hand-labelling
    pages across the live corpus, and inventing them here would repeat the
    exact mistake being fixed.
"""
from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from app.services.ingest.ocr_quality import (
    ROUTING_THRESHOLDS_ENV,
    OcrRoutingThresholds,
    OcrRoutingTier,
    assess_ocr_quality,
    calculate_ocr_quality,
    load_routing_thresholds_from_env,
)

BANDS = dict(
    catastrophic_max_mean_confidence=0.20,
    catastrophic_max_coverage_ratio=0.10,
    mandatory_min_mean_confidence=0.60,
    mandatory_min_median_confidence=0.65,
    mandatory_max_low_confidence_word_ratio=0.50,
    mandatory_min_coverage_ratio=0.50,
    mandatory_max_gibberish_word_ratio=0.40,
    mandatory_max_repeated_character_ratio=0.30,
    mandatory_max_seam_duplicate_ratio=0.30,
    spot_check_min_mean_confidence=0.85,
    spot_check_min_median_confidence=0.85,
    spot_check_max_low_confidence_word_ratio=0.15,
    spot_check_min_coverage_ratio=0.90,
    spot_check_max_gibberish_word_ratio=0.10,
    spot_check_max_repeated_character_ratio=0.05,
    spot_check_max_seam_duplicate_ratio=0.05,
)


def thresholds(**overrides) -> OcrRoutingThresholds:
    return OcrRoutingThresholds(**{**BANDS, **overrides})


def clean_signals():
    """A page that clears every band, so the tier is not what varies."""
    return calculate_ocr_quality(
        "The Athabasca Basin hosts unconformity related uranium deposits "
        "within Paleoproterozoic metasedimentary rocks of the Wollaston "
        "Domain along the eastern margin",
        # 20 words in the text above, so detected_region_count must
        # match or output_coverage_ratio falls under the spot-check
        # floor and every page here lands in the wrong tier.
        [0.97] * 20,
        detected_region_count=20,
    )


def set_env(monkeypatch, payload: dict) -> None:
    monkeypatch.setenv(ROUTING_THRESHOLDS_ENV, json.dumps(payload))


# ---------------------------------------------------------------------------
# The calibration claim
# ---------------------------------------------------------------------------

class TestCalibrationIsClaimedNotAssumed:
    def test_a_threshold_set_with_no_artefact_is_not_calibrated(self) -> None:
        """The deployed configuration, exactly: numbers and nothing else."""
        assessment = assess_ocr_quality(clean_signals(), thresholds())

        assert assessment.tier is OcrRoutingTier.AutoAccept
        assert assessment.thresholds_calibrated is False, (
            "supplying numbers is not the same as measuring them"
        )

    def test_naming_an_artefact_makes_it_calibrated(self) -> None:
        assessment = assess_ocr_quality(
            clean_signals(),
            thresholds(calibrated_from="ops/baselines/ocr-2026-09-01.json"),
        )
        assert assessment.thresholds_calibrated is True

    @pytest.mark.parametrize("blank", [None, "", "   ", "\t\n"])
    def test_whitespace_does_not_count_as_an_artefact(self, blank) -> None:
        """Satisfying the schema is not satisfying the requirement."""
        assert thresholds(calibrated_from=blank).is_calibrated is False

    def test_every_tier_reports_the_same_calibration_answer(self) -> None:
        """The flag describes the CONFIG, not the page.

        It was hard-coded True on four of the five return paths, so a page
        could not disagree with another page about whether the thresholds
        were measured -- but all five said the wrong thing together.
        """
        pages = {
            "auto_accept": clean_signals(),
            "mandatory": calculate_ocr_quality(
                "aaaaa bbbbb ccccc ddddd", [0.30] * 4, detected_region_count=4),
            "catastrophic": calculate_ocr_quality(
                "x y", [0.05] * 2, detected_region_count=200),
            "empty": calculate_ocr_quality("", [], detected_region_count=8),
        }
        uncalibrated = thresholds()
        calibrated = thresholds(calibrated_from="ops/baselines/x.json")

        assert {
            name: assess_ocr_quality(s, uncalibrated).thresholds_calibrated
            for name, s in pages.items()
        } == dict.fromkeys(pages, False)

        assert {
            name: assess_ocr_quality(s, calibrated).thresholds_calibrated
            for name, s in pages.items()
        } == dict.fromkeys(pages, True)

    def test_no_thresholds_at_all_still_fails_closed_to_review(self) -> None:
        """Unchanged, and the reason it must stay unchanged: 'uncalibrated'
        now describes a supplied set too, so the no-config path needs its
        own distinct outcome."""
        assessment = assess_ocr_quality(clean_signals(), None)

        assert assessment.tier is OcrRoutingTier.MandatoryReview
        assert assessment.reasons == ("routing_thresholds_not_calibrated",)
        assert assessment.thresholds_calibrated is False

    def test_calibrated_from_is_not_range_checked_as_a_threshold(self) -> None:
        """__post_init__ validates every field into [0, 1]. A string field
        added to that loop would raise on construction."""
        assert thresholds(calibrated_from="a/path.json").calibrated_from == (
            "a/path.json")


# ---------------------------------------------------------------------------
# Per-engine bands
# ---------------------------------------------------------------------------

class TestPerEngineThresholds:
    def test_the_deployed_flat_shape_still_loads(self, monkeypatch) -> None:
        """The live env var is a flat object. It must keep working
        untouched -- this change adds a shape, it does not replace one."""
        set_env(monkeypatch, asdict(thresholds()))

        assert load_routing_thresholds_from_env() == thresholds()
        assert load_routing_thresholds_from_env("tesseract") == thresholds()

    def test_an_engine_override_is_merged_over_the_defaults(
        self, monkeypatch,
    ) -> None:
        """An override states only what DIFFERS.

        Requiring all sixteen fields per engine would mean three copies of
        the same numbers drifting apart, which is how the single shared set
        became wrong for both engines in the first place.
        """
        set_env(monkeypatch, {
            **asdict(thresholds()),
            "by_ocr_method": {
                "tesseract": {"spot_check_min_mean_confidence": 0.72},
                "document_intelligence": {
                    "spot_check_min_mean_confidence": 0.97},
            },
        })

        tess = load_routing_thresholds_from_env("tesseract")
        di = load_routing_thresholds_from_env("document_intelligence")

        assert tess.spot_check_min_mean_confidence == 0.72
        assert di.spot_check_min_mean_confidence == 0.97
        # Everything not overridden comes from the shared block.
        assert tess.mandatory_min_mean_confidence == 0.60
        assert di.mandatory_min_mean_confidence == 0.60

    def test_the_split_actually_changes_the_routing(self, monkeypatch) -> None:
        """The point of the whole exercise.

        A Tesseract page at mean 0.80 is a normal good page. Under the
        shared 0.85 cut-off it goes to review; under a Tesseract-calibrated
        0.72 it does not. The same 0.80 from Document Intelligence is
        genuinely poor and still goes to review under its 0.97.
        """
        set_env(monkeypatch, {
            **asdict(thresholds(calibrated_from="ops/baselines/x.json")),
            "by_ocr_method": {
                "tesseract": {
                    "spot_check_min_mean_confidence": 0.72,
                    "spot_check_min_median_confidence": 0.72,
                },
                "document_intelligence": {
                    "spot_check_min_mean_confidence": 0.97,
                    "spot_check_min_median_confidence": 0.97,
                },
            },
        })

        page = calculate_ocr_quality(
            "The Athabasca Basin hosts unconformity related uranium deposits "
            "within Paleoproterozoic metasedimentary rocks of the Wollaston "
            "Domain along the eastern margin",
            [0.80] * 20,
            detected_region_count=20,
        )

        tess = assess_ocr_quality(
            page, load_routing_thresholds_from_env("tesseract"))
        di = assess_ocr_quality(
            page, load_routing_thresholds_from_env("document_intelligence"))

        assert tess.tier is OcrRoutingTier.AutoAccept
        assert di.tier is OcrRoutingTier.SpotCheck
        assert "mean_confidence" in di.reasons

    def test_an_unknown_engine_gets_the_shared_set(self, monkeypatch) -> None:
        set_env(monkeypatch, {
            **asdict(thresholds()),
            "by_ocr_method": {"tesseract": {
                "spot_check_min_mean_confidence": 0.72}},
        })

        assert load_routing_thresholds_from_env("azure_vision") == thresholds()
        assert load_routing_thresholds_from_env(None) == thresholds()

    def test_calibrated_from_survives_the_merge(self, monkeypatch) -> None:
        """A per-engine block must not silently erase the artefact claim
        made for the file as a whole."""
        set_env(monkeypatch, {
            **asdict(thresholds(calibrated_from="ops/baselines/x.json")),
            "by_ocr_method": {"tesseract": {
                "spot_check_min_mean_confidence": 0.72}},
        })

        loaded = load_routing_thresholds_from_env("tesseract")
        assert loaded.is_calibrated is True
        assert loaded.calibrated_from == "ops/baselines/x.json"

    def test_an_engine_may_state_its_own_artefact(self, monkeypatch) -> None:
        """Engines are calibrated separately, so they may be calibrated at
        different times or not at all."""
        set_env(monkeypatch, {
            **asdict(thresholds()),
            "by_ocr_method": {"tesseract": {
                "calibrated_from": "ops/baselines/tesseract-2026-09-01.json"}},
        })

        assert load_routing_thresholds_from_env("tesseract").is_calibrated
        assert not load_routing_thresholds_from_env(
            "document_intelligence").is_calibrated

    def test_a_malformed_engine_map_is_rejected_loudly(
        self, monkeypatch,
    ) -> None:
        set_env(monkeypatch, {**asdict(thresholds()),
                              "by_ocr_method": ["tesseract"]})
        with pytest.raises(ValueError, match="keyed by ocr_method"):
            load_routing_thresholds_from_env("tesseract")

    def test_a_malformed_engine_block_is_rejected_loudly(
        self, monkeypatch,
    ) -> None:
        set_env(monkeypatch, {**asdict(thresholds()),
                              "by_ocr_method": {"tesseract": 0.9}})
        with pytest.raises(ValueError, match="must be a JSON object"):
            load_routing_thresholds_from_env("tesseract")

    def test_an_override_that_inverts_the_bands_still_raises(
        self, monkeypatch,
    ) -> None:
        """The ordering invariant is enforced in __post_init__, so it
        applies to a merged set as well as a declared one -- otherwise
        per-engine config would be a way around the validation."""
        set_env(monkeypatch, {
            **asdict(thresholds()),
            "by_ocr_method": {"tesseract": {
                "spot_check_min_mean_confidence": 0.10}},
        })

        with pytest.raises(
            ValueError,
            match="catastrophic <= mandatory <= spot_check",
        ):
            load_routing_thresholds_from_env("tesseract")

    def test_an_unknown_key_is_still_rejected(self, monkeypatch) -> None:
        """by_ocr_method is popped before construction; nothing else is."""
        set_env(monkeypatch, {**asdict(thresholds()), "typo_field": 0.5})
        with pytest.raises(ValueError, match="threshold schema"):
            load_routing_thresholds_from_env()


# ---------------------------------------------------------------------------
# The engine reaches the loader
# ---------------------------------------------------------------------------

class TestTheEngineIsPassedInNotStampedAfterwards:
    def test_assess_ocr_result_selects_the_engine_bands(
        self, monkeypatch,
    ) -> None:
        """End of the wire. Every call site used to set
        ``assessment["ocr_method"]`` on the line AFTER the call -- one line
        too late for the threshold loader to use it.
        """
        from app.services.ingest.pdf_report import _assess_ocr_result

        set_env(monkeypatch, {
            **asdict(thresholds(calibrated_from="ops/baselines/x.json")),
            "by_ocr_method": {
                "tesseract": {
                    "spot_check_min_mean_confidence": 0.72,
                    "spot_check_min_median_confidence": 0.72,
                },
                "document_intelligence": {
                    "spot_check_min_mean_confidence": 0.97,
                    "spot_check_min_median_confidence": 0.97,
                },
            },
        })

        text = (
            "The Athabasca Basin hosts unconformity related uranium deposits "
            "within Paleoproterozoic metasedimentary rocks of the Wollaston "
            "Domain along the eastern margin"
        )
        confidences = [0.80] * 20

        tess = _assess_ocr_result(
            text, confidences, detected_region_count=20,
            ocr_method="tesseract")
        di = _assess_ocr_result(
            text, confidences, detected_region_count=20,
            ocr_method="document_intelligence")

        assert tess["tier"] == "auto_accept"
        assert di["tier"] == "spot_check"

    def test_the_engine_still_lands_in_the_returned_payload(self) -> None:
        """It used to be stamped on by each caller. Moving it inside means
        a review_queue row cannot arrive without naming its engine."""
        from app.services.ingest.pdf_report import _assess_ocr_result

        result = _assess_ocr_result(
            "some text here", [0.9] * 3, detected_region_count=3,
            ocr_method="tesseract")

        assert result["ocr_method"] == "tesseract"

    def test_omitting_the_engine_omits_the_key(self) -> None:
        """The three empty-output placeholder call sites pass no engine,
        and an ``ocr_method: None`` in the payload would be worse than an
        absent key -- it reads as "we looked and there wasn't one"."""
        from app.services.ingest.pdf_report import _assess_ocr_result

        result = _assess_ocr_result("", [], detected_region_count=0)

        assert "ocr_method" not in result
        assert result["tier"] == "catastrophic_failure"

    def test_no_call_site_stamps_the_engine_afterwards_any_more(self) -> None:
        """Two ways to set one field is how a page ends up with neither."""
        from pathlib import Path

        source = (
            Path(__file__).resolve().parent.parent
            / "app" / "services" / "ingest" / "pdf_report.py"
        ).read_text(encoding="utf-8")

        assert '["ocr_method"] = "' not in source, (
            "a call site is setting ocr_method after the call again -- pass "
            "it in, or the threshold loader cannot see it"
        )
