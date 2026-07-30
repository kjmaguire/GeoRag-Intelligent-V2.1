"""Multi-signal OCR quality scoring and fail-closed routing."""

from __future__ import annotations

import json
import os
import re
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, fields
from enum import StrEnum

_WORD_RE = re.compile(r"\b[\w'-]+\b", re.UNICODE)
_REPEATED_CHARACTER_RE = re.compile(r"(.)\1{4,}", re.UNICODE)
ROUTING_THRESHOLDS_ENV = "OCR_ROUTING_THRESHOLDS_JSON"


class OcrRoutingTier(StrEnum):
    CatastrophicFailure = "catastrophic_failure"
    MandatoryReview = "mandatory_review"
    SpotCheck = "spot_check"
    AutoAccept = "auto_accept"


@dataclass(frozen=True, slots=True)
class OcrQualitySignals:
    mean_confidence: float
    median_confidence: float
    low_confidence_word_ratio: float
    output_coverage_ratio: float
    empty_output: bool
    seam_duplicate_ratio: float
    gibberish_word_ratio: float
    repeated_character_ratio: float
    word_count: int
    detected_region_count: int


@dataclass(frozen=True, slots=True)
class OcrRoutingThresholds:
    """Corpus-calibrated routing thresholds.

    No defaults are supplied deliberately. Auto-accept behavior must be
    backed by measured corpus data or explicit SME-approved configuration.
    """

    catastrophic_max_mean_confidence: float
    catastrophic_max_coverage_ratio: float
    mandatory_min_mean_confidence: float
    mandatory_min_median_confidence: float
    mandatory_max_low_confidence_word_ratio: float
    mandatory_min_coverage_ratio: float
    mandatory_max_gibberish_word_ratio: float
    mandatory_max_repeated_character_ratio: float
    mandatory_max_seam_duplicate_ratio: float
    spot_check_min_mean_confidence: float
    spot_check_min_median_confidence: float
    spot_check_max_low_confidence_word_ratio: float
    spot_check_min_coverage_ratio: float
    spot_check_max_gibberish_word_ratio: float
    spot_check_max_repeated_character_ratio: float
    spot_check_max_seam_duplicate_ratio: float

    def __post_init__(self) -> None:
        for field in fields(self):
            name = field.name
            value = getattr(self, name)
            if not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be in [0, 1]")

        ordered_minimums = (
            (
                "mean_confidence",
                self.catastrophic_max_mean_confidence,
                self.mandatory_min_mean_confidence,
                self.spot_check_min_mean_confidence,
            ),
            (
                "coverage_ratio",
                self.catastrophic_max_coverage_ratio,
                self.mandatory_min_coverage_ratio,
                self.spot_check_min_coverage_ratio,
            ),
        )
        for name, catastrophic, mandatory, spot_check in ordered_minimums:
            if not catastrophic <= mandatory <= spot_check:
                raise ValueError(f"{name} thresholds must satisfy catastrophic <= mandatory <= spot_check")

        if self.mandatory_min_median_confidence > self.spot_check_min_median_confidence:
            raise ValueError("median_confidence thresholds must satisfy mandatory <= spot_check")

        ordered_maximums = (
            (
                "low_confidence_word_ratio",
                self.mandatory_max_low_confidence_word_ratio,
                self.spot_check_max_low_confidence_word_ratio,
            ),
            (
                "gibberish_word_ratio",
                self.mandatory_max_gibberish_word_ratio,
                self.spot_check_max_gibberish_word_ratio,
            ),
            (
                "repeated_character_ratio",
                self.mandatory_max_repeated_character_ratio,
                self.spot_check_max_repeated_character_ratio,
            ),
            (
                "seam_duplicate_ratio",
                self.mandatory_max_seam_duplicate_ratio,
                self.spot_check_max_seam_duplicate_ratio,
            ),
        )
        for name, mandatory, spot_check in ordered_maximums:
            if mandatory < spot_check:
                raise ValueError(f"{name} thresholds must satisfy mandatory >= spot_check")


@dataclass(frozen=True, slots=True)
class OcrQualityAssessment:
    signals: OcrQualitySignals
    tier: OcrRoutingTier
    reasons: tuple[str, ...]
    thresholds_calibrated: bool

    @property
    def review_queue_routing_decision(self) -> str:
        """Map internal bands onto the existing Postgres enum."""

        if self.tier is OcrRoutingTier.AutoAccept:
            return "auto_pass"
        return "review_required"


def calculate_ocr_quality(
    text: str,
    word_confidences: Sequence[float],
    *,
    detected_region_count: int,
    seam_duplicate_count: int = 0,
    low_confidence_word_threshold: float = 0.5,
) -> OcrQualitySignals:
    """Calculate independent OCR confidence and text-quality signals."""

    if not 0.0 <= low_confidence_word_threshold <= 1.0:
        raise ValueError("low_confidence_word_threshold must be in [0, 1]")

    confidences = tuple(max(0.0, min(1.0, float(confidence))) for confidence in word_confidences)
    words = _WORD_RE.findall(text)
    word_count = len(words)
    mean_confidence = statistics.fmean(confidences) if confidences else 0.0
    median_confidence = statistics.median(confidences) if confidences else 0.0
    low_confidence_word_ratio = (
        sum(confidence < low_confidence_word_threshold for confidence in confidences) / len(confidences)
        if confidences
        else 1.0
    )
    output_coverage_ratio = (
        min(1.0, word_count / detected_region_count) if detected_region_count > 0 else (1.0 if word_count > 0 else 0.0)
    )
    bounded_duplicate_count = max(0, seam_duplicate_count)
    seam_duplicate_ratio = bounded_duplicate_count / max(
        word_count + bounded_duplicate_count,
        1,
    )
    gibberish_word_ratio = sum(_is_gibberish_word(word) for word in words) / word_count if words else 1.0
    repeated_character_ratio = (
        sum(bool(_REPEATED_CHARACTER_RE.search(word)) for word in words) / word_count if words else 1.0
    )

    return OcrQualitySignals(
        mean_confidence=mean_confidence,
        median_confidence=median_confidence,
        low_confidence_word_ratio=low_confidence_word_ratio,
        output_coverage_ratio=output_coverage_ratio,
        empty_output=not text.strip() or word_count == 0,
        seam_duplicate_ratio=seam_duplicate_ratio,
        gibberish_word_ratio=gibberish_word_ratio,
        repeated_character_ratio=repeated_character_ratio,
        word_count=word_count,
        detected_region_count=max(0, detected_region_count),
    )


def assess_ocr_quality(
    signals: OcrQualitySignals,
    thresholds: OcrRoutingThresholds | None = None,
) -> OcrQualityAssessment:
    """Assign a routing band, defaulting safely to review if uncalibrated."""

    if signals.empty_output:
        return OcrQualityAssessment(
            signals,
            OcrRoutingTier.CatastrophicFailure,
            ("empty_output",),
            thresholds is not None,
        )

    if thresholds is None:
        return OcrQualityAssessment(
            signals,
            OcrRoutingTier.MandatoryReview,
            ("routing_thresholds_not_calibrated",),
            False,
        )

    catastrophic_reasons: list[str] = []
    if signals.mean_confidence <= thresholds.catastrophic_max_mean_confidence:
        catastrophic_reasons.append("catastrophic_mean_confidence")
    if signals.output_coverage_ratio <= thresholds.catastrophic_max_coverage_ratio:
        catastrophic_reasons.append("catastrophic_output_coverage")
    if catastrophic_reasons:
        return OcrQualityAssessment(
            signals,
            OcrRoutingTier.CatastrophicFailure,
            tuple(catastrophic_reasons),
            True,
        )

    mandatory_reasons = _threshold_reasons(
        signals,
        min_mean=thresholds.mandatory_min_mean_confidence,
        min_median=thresholds.mandatory_min_median_confidence,
        max_low_ratio=thresholds.mandatory_max_low_confidence_word_ratio,
        min_coverage=thresholds.mandatory_min_coverage_ratio,
        max_gibberish=thresholds.mandatory_max_gibberish_word_ratio,
        max_repeated=thresholds.mandatory_max_repeated_character_ratio,
        max_duplicates=thresholds.mandatory_max_seam_duplicate_ratio,
    )
    if mandatory_reasons:
        return OcrQualityAssessment(
            signals,
            OcrRoutingTier.MandatoryReview,
            mandatory_reasons,
            True,
        )

    spot_check_reasons = _threshold_reasons(
        signals,
        min_mean=thresholds.spot_check_min_mean_confidence,
        min_median=thresholds.spot_check_min_median_confidence,
        max_low_ratio=thresholds.spot_check_max_low_confidence_word_ratio,
        min_coverage=thresholds.spot_check_min_coverage_ratio,
        max_gibberish=thresholds.spot_check_max_gibberish_word_ratio,
        max_repeated=thresholds.spot_check_max_repeated_character_ratio,
        max_duplicates=thresholds.spot_check_max_seam_duplicate_ratio,
    )
    if spot_check_reasons:
        return OcrQualityAssessment(
            signals,
            OcrRoutingTier.SpotCheck,
            spot_check_reasons,
            True,
        )

    return OcrQualityAssessment(
        signals,
        OcrRoutingTier.AutoAccept,
        (),
        True,
    )


def load_routing_thresholds_from_env() -> OcrRoutingThresholds | None:
    """Load an explicitly calibrated threshold set, or return fail-closed None."""

    raw = os.environ.get(ROUTING_THRESHOLDS_ENV, "").strip()
    if not raw:
        return None
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{ROUTING_THRESHOLDS_ENV} must contain valid JSON") from exc
    if not isinstance(values, dict):
        raise ValueError(f"{ROUTING_THRESHOLDS_ENV} must contain a JSON object")
    try:
        return OcrRoutingThresholds(**values)
    except TypeError as exc:
        raise ValueError(f"{ROUTING_THRESHOLDS_ENV} does not match the calibrated threshold schema") from exc


def _threshold_reasons(
    signals: OcrQualitySignals,
    *,
    min_mean: float,
    min_median: float,
    max_low_ratio: float,
    min_coverage: float,
    max_gibberish: float,
    max_repeated: float,
    max_duplicates: float,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if signals.mean_confidence < min_mean:
        reasons.append("mean_confidence")
    if signals.median_confidence < min_median:
        reasons.append("median_confidence")
    if signals.low_confidence_word_ratio > max_low_ratio:
        reasons.append("low_confidence_word_ratio")
    if signals.output_coverage_ratio < min_coverage:
        reasons.append("output_coverage_ratio")
    if signals.gibberish_word_ratio > max_gibberish:
        reasons.append("gibberish_word_ratio")
    if signals.seam_duplicate_ratio > max_duplicates:
        reasons.append("seam_duplicate_ratio")
    if signals.repeated_character_ratio > max_repeated:
        reasons.append("repeated_character_ratio")
    return tuple(reasons)


def _is_gibberish_word(word: str) -> bool:
    if not word:
        return True
    if _REPEATED_CHARACTER_RE.search(word):
        return True
    alpha_count = sum(character.isalpha() for character in word)
    digit_count = sum(character.isdigit() for character in word)
    symbol_count = len(word) - alpha_count - digit_count
    if len(word) >= 4 and alpha_count == 0:
        return True
    if len(word) >= 5 and symbol_count / len(word) > 0.4:
        return True
    if len(word) >= 24 and not any(character in "-'" for character in word):
        return True
    return False


__all__ = [
    "OcrQualityAssessment",
    "OcrQualitySignals",
    "OcrRoutingThresholds",
    "OcrRoutingTier",
    "ROUTING_THRESHOLDS_ENV",
    "assess_ocr_quality",
    "calculate_ocr_quality",
    "load_routing_thresholds_from_env",
]
