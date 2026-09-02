"""Unit tests for multi-signal OCR quality routing."""

from __future__ import annotations

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


def _thresholds() -> OcrRoutingThresholds:
    """Synthetic test-only thresholds, not production calibration."""

    return OcrRoutingThresholds(
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


def test_empty_output_is_catastrophic() -> None:
    signals = calculate_ocr_quality(
        "",
        [],
        detected_region_count=10,
    )

    assessment = assess_ocr_quality(signals, _thresholds())

    assert assessment.tier is OcrRoutingTier.CatastrophicFailure
    assert assessment.review_queue_routing_decision == "review_required"
    assert "empty_output" in assessment.reasons


def test_uncalibrated_nonempty_output_fails_closed_to_mandatory_review() -> None:
    signals = calculate_ocr_quality(
        "Geological report text",
        [0.98, 0.96, 0.97],
        detected_region_count=3,
    )

    assessment = assess_ocr_quality(signals)

    assert assessment.tier is OcrRoutingTier.MandatoryReview
    assert assessment.thresholds_calibrated is False
    assert assessment.reasons == ("routing_thresholds_not_calibrated",)


def test_calibrated_clean_output_is_auto_accept() -> None:
    signals = calculate_ocr_quality(
        "Geological report contains mineral resource estimates",
        [0.98] * 7,
        detected_region_count=6,
    )

    assessment = assess_ocr_quality(signals, _thresholds())

    assert assessment.tier is OcrRoutingTier.AutoAccept
    assert assessment.review_queue_routing_decision == "auto_pass"


def test_median_and_low_word_ratio_can_force_mandatory_review() -> None:
    signals = calculate_ocr_quality(
        "one two three four five",
        [0.95, 0.40, 0.40, 0.40, 0.95],
        detected_region_count=5,
    )

    assessment = assess_ocr_quality(signals, _thresholds())

    assert assessment.tier is OcrRoutingTier.MandatoryReview
    assert "median_confidence" in assessment.reasons
    assert "low_confidence_word_ratio" in assessment.reasons


def test_repeated_characters_and_seam_duplicates_are_independent_signals() -> None:
    signals = calculate_ocr_quality(
        "normal AAAAAA words",
        [0.90, 0.90, 0.90],
        detected_region_count=3,
        seam_duplicate_count=2,
    )

    assert signals.repeated_character_ratio > 0
    assert signals.gibberish_word_ratio > 0
    assert signals.seam_duplicate_ratio == 0.4


def test_thresholds_load_only_from_explicit_json(
    monkeypatch,
) -> None:
    monkeypatch.delenv(ROUTING_THRESHOLDS_ENV, raising=False)
    assert load_routing_thresholds_from_env() is None

    import json

    monkeypatch.setenv(ROUTING_THRESHOLDS_ENV, json.dumps(asdict(_thresholds())))

    assert load_routing_thresholds_from_env() == _thresholds()


def test_rejects_inverted_routing_bands() -> None:
    values = asdict(_thresholds())
    values["spot_check_min_mean_confidence"] = 0.50

    with pytest.raises(ValueError, match="catastrophic <= mandatory <= spot_check"):
        OcrRoutingThresholds(**values)


def test_rejects_invalid_low_confidence_cutoff() -> None:
    with pytest.raises(ValueError, match=r"must be in \[0, 1\]"):
        calculate_ocr_quality(
            "text",
            [0.9],
            detected_region_count=1,
            low_confidence_word_threshold=1.1,
        )


def test_negative_duplicate_count_cannot_create_negative_ratio() -> None:
    signals = calculate_ocr_quality(
        "valid text",
        [0.9, 0.9],
        detected_region_count=2,
        seam_duplicate_count=-5,
    )

    assert signals.seam_duplicate_ratio == 0.0


# ---------------------------------------------------------------------------
# Engines with no confidence (Cohere Parse, ADR-0019)
# ---------------------------------------------------------------------------


def test_no_confidence_is_recorded_as_neutral_not_as_zero_quality() -> None:
    signals = calculate_ocr_quality(
        "Geological report contains mineral resource estimates",
        None,
        detected_region_count=0,
    )

    assert signals.confidence_reported is False
    assert signals.mean_confidence == 0.0
    assert signals.median_confidence == 0.0
    assert signals.low_confidence_word_ratio == 0.0
    assert signals.output_coverage_ratio == 1.0


def test_an_empty_sequence_still_means_confidence_was_reported() -> None:
    signals = calculate_ocr_quality("x", [], detected_region_count=1)

    assert signals.confidence_reported is True
    assert signals.low_confidence_word_ratio == 1.0


def test_calibrated_clean_text_without_confidence_auto_accepts() -> None:
    signals = calculate_ocr_quality(
        "Geological report contains mineral resource estimates",
        None,
        detected_region_count=0,
    )

    assessment = assess_ocr_quality(signals, _thresholds())

    assert assessment.tier is OcrRoutingTier.AutoAccept
    assert assessment.reasons == ()


def test_gibberish_without_confidence_still_routes_to_review() -> None:
    signals = calculate_ocr_quality(
        "aaaaaa bbbbbb cccccc dddddd eeeeee ffffff",
        None,
        detected_region_count=0,
    )

    assessment = assess_ocr_quality(signals, _thresholds())

    assert assessment.tier is OcrRoutingTier.MandatoryReview
    assert "gibberish_word_ratio" in assessment.reasons
    # The confidence bands were skipped, not tripped.
    assert "mean_confidence" not in assessment.reasons
    assert "low_confidence_word_ratio" not in assessment.reasons


def test_empty_output_without_confidence_is_still_catastrophic() -> None:
    signals = calculate_ocr_quality("", None, detected_region_count=0)

    assessment = assess_ocr_quality(signals, _thresholds())

    assert assessment.tier is OcrRoutingTier.CatastrophicFailure
    assert assessment.reasons == ("empty_output",)


def test_uncalibrated_without_confidence_still_fails_closed() -> None:
    signals = calculate_ocr_quality("Clean prose", None, detected_region_count=0)

    assessment = assess_ocr_quality(signals, None)

    assert assessment.tier is OcrRoutingTier.MandatoryReview
    assert assessment.reasons == ("routing_thresholds_not_calibrated",)


def test_spot_check_is_its_own_routing_decision() -> None:
    """Queued for a sample, not demoted: spot_check != review_required."""
    signals = calculate_ocr_quality(
        "Geological report contains mineral resource estimates",
        [0.80] * 7,
        detected_region_count=6,
    )

    assessment = assess_ocr_quality(signals, _thresholds())

    assert assessment.tier is OcrRoutingTier.SpotCheck
    assert assessment.review_queue_routing_decision == "spot_check"
