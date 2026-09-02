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
    #: False when the engine reports no per-word confidence (Cohere Parse,
    #: ADR-0019). The confidence-based bands are then skipped and the page
    #: is judged on its content signals; the fail-closed default is unchanged.
    confidence_reported: bool = True


_FLOOR_TIERS: frozenset[str] = frozenset({OcrRoutingTier.SpotCheck.value, OcrRoutingTier.MandatoryReview.value})
_NON_THRESHOLD_FIELDS: frozenset[str] = frozenset({"calibrated_from", "floor_tier"})


@dataclass(frozen=True, slots=True)
class OcrRoutingThresholds:
    """Routing thresholds, and a statement of where they came from.

    No numeric defaults are supplied deliberately. Auto-accept behavior
    must be backed by measured corpus data or explicit SME-approved
    configuration.

    ``calibrated_from`` is what makes that sentence checkable. Until
    2026-08-21 the assessment recorded ``thresholds_calibrated=True``
    whenever the env var PARSED -- so a set of round numbers with no
    artefact behind it was indistinguishable from a measured one, and the
    planned quality classifier would have trained on those labels as
    evidence. It now records whether someone named an artefact.

    Set it to a path or identifier that a reader can go and look at, e.g.
    ``"ops/baselines/ocr-calibration-2026-09-01.json"``. Leave it unset and
    the routing still works exactly as before; it simply stops claiming to
    be calibrated.
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

    #: Free-text pointer to the measurement this set came from. Not a
    #: threshold, so it is skipped by the range check below.
    calibrated_from: str | None = None

    #: Worst tier a page may auto-route to under this set: ``"spot_check"``
    #: or ``"mandatory_review"``. Added 2026-09-02 for engines that report
    #: no confidence (Cohere Parse, ADR-0019): with the confidence bands
    #: skipped, a clean page auto-accepts on content signals alone, and
    #: there is no threshold value that can express "never auto-accept
    #: this engine until it is calibrated" — every rule is a strict
    #: inequality a clean page satisfies. This is that switch, per engine
    #: via ``by_ocr_method``. Not a threshold, so it too skips the range
    #: check.
    floor_tier: str | None = None

    @property
    def is_calibrated(self) -> bool:
        """True only when an artefact was named.

        Whitespace does not count: `"calibrated_from": " "` is someone
        satisfying the schema rather than the requirement.
        """
        return bool((self.calibrated_from or "").strip())

    def __post_init__(self) -> None:
        if self.floor_tier is not None and self.floor_tier not in _FLOOR_TIERS:
            raise ValueError(f"floor_tier must be one of {sorted(_FLOOR_TIERS)} or null")
        for field in fields(self):
            name = field.name
            if name in _NON_THRESHOLD_FIELDS:
                continue
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
    word_confidences: Sequence[float] | None,
    *,
    detected_region_count: int,
    seam_duplicate_count: int = 0,
    low_confidence_word_threshold: float = 0.5,
) -> OcrQualitySignals:
    """Calculate independent OCR confidence and text-quality signals.

    ``word_confidences=None`` means the engine reports no confidence at all
    (as opposed to an empty sequence, which means it reported confidence
    and found no words). The confidence signals are then recorded as
    neutral values — ``0.0`` means and a ``0.0`` low-confidence ratio, NOT
    ``1.0``, so a rule that forgets to check ``confidence_reported`` cannot
    trip the mandatory band by accident — and ``confidence_reported`` is
    False so `assess_ocr_quality` skips those bands.
    """

    if not 0.0 <= low_confidence_word_threshold <= 1.0:
        raise ValueError("low_confidence_word_threshold must be in [0, 1]")

    confidence_reported = word_confidences is not None
    confidences = tuple(max(0.0, min(1.0, float(confidence))) for confidence in (word_confidences or ()))
    words = _WORD_RE.findall(text)
    word_count = len(words)
    mean_confidence = statistics.fmean(confidences) if confidences else 0.0
    median_confidence = statistics.median(confidences) if confidences else 0.0
    if not confidence_reported:
        low_confidence_word_ratio = 0.0
    elif confidences:
        low_confidence_word_ratio = (
            sum(confidence < low_confidence_word_threshold for confidence in confidences) / len(confidences)
        )
    else:
        low_confidence_word_ratio = 1.0
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
        confidence_reported=confidence_reported,
    )


def assess_ocr_quality(
    signals: OcrQualitySignals,
    thresholds: OcrRoutingThresholds | None = None,
) -> OcrQualityAssessment:
    """Assign a routing band, defaulting safely to review if uncalibrated."""

    calibrated = thresholds is not None and thresholds.is_calibrated

    if signals.empty_output:
        return OcrQualityAssessment(
            signals,
            OcrRoutingTier.CatastrophicFailure,
            ("empty_output",),
            calibrated,
        )

    if thresholds is None:
        return OcrQualityAssessment(
            signals,
            OcrRoutingTier.MandatoryReview,
            ("routing_thresholds_not_calibrated",),
            False,
        )

    catastrophic_reasons: list[str] = []
    if signals.confidence_reported and signals.mean_confidence <= thresholds.catastrophic_max_mean_confidence:
        catastrophic_reasons.append("catastrophic_mean_confidence")
    if signals.output_coverage_ratio <= thresholds.catastrophic_max_coverage_ratio:
        catastrophic_reasons.append("catastrophic_output_coverage")
    if catastrophic_reasons:
        return OcrQualityAssessment(
            signals,
            OcrRoutingTier.CatastrophicFailure,
            tuple(catastrophic_reasons),
            calibrated,
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
            calibrated,
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
    if thresholds.floor_tier == OcrRoutingTier.MandatoryReview.value:
        return OcrQualityAssessment(
            signals,
            OcrRoutingTier.MandatoryReview,
            (*spot_check_reasons, "floor_tier"),
            calibrated,
        )
    if spot_check_reasons or thresholds.floor_tier == OcrRoutingTier.SpotCheck.value:
        return OcrQualityAssessment(
            signals,
            OcrRoutingTier.SpotCheck,
            spot_check_reasons or ("floor_tier",),
            calibrated,
        )

    return OcrQualityAssessment(
        signals,
        OcrRoutingTier.AutoAccept,
        (),
        calibrated,
    )


#: Key under which a per-engine threshold map may be supplied.
BY_OCR_METHOD_KEY = "by_ocr_method"


def load_routing_thresholds_from_env(
    ocr_method: str | None = None,
) -> OcrRoutingThresholds | None:
    """Load the threshold set for one engine, or fail-closed None.

    TWO ACCEPTED SHAPES

    Flat -- one set for every engine. This is what is deployed today::

        {"catastrophic_max_mean_confidence": 0.30, ...}

    Per-engine -- a default plus overrides keyed by ``ocr_method``::

        {
          "calibrated_from": "ops/baselines/ocr-calibration-2026-09-01.json",
          "catastrophic_max_mean_confidence": 0.30, ...,
          "by_ocr_method": {
            "tesseract":    {"spot_check_min_mean_confidence": 0.72, ...},
            "cohere_parse": {"floor_tier": "spot_check"}
          }
        }

    An engine's block is merged OVER the top-level set, so an override
    states only what differs. An engine with no block gets the top-level
    set, which is also what an unknown or absent ``ocr_method`` gets.

    WHY PER-ENGINE AT ALL
        Engines do not report confidence on a comparable scale — or at all.
        Tesseract sits at 0.70-0.85 when it is doing fine; the retired
        Document Intelligence sat at 0.95-0.99 even when confidently wrong;
        Cohere Parse (ADR-0019) reports none, so its pages carry
        ``confidence_reported=False`` and the confidence keys in its block
        are simply never consulted. A ``cohere_parse`` block therefore
        states only content-signal keys — and, until a calibration artefact
        exists, ``"floor_tier": "spot_check"`` keeps every Parse page out
        of auto-accept regardless of how clean its text looks.

        The shape is available; the numbers still have to be measured.
        Nothing here invents them.
    """

    raw = os.environ.get(ROUTING_THRESHOLDS_ENV, "").strip()
    if not raw:
        return None
    try:
        values = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{ROUTING_THRESHOLDS_ENV} must contain valid JSON") from exc
    if not isinstance(values, dict):
        raise ValueError(f"{ROUTING_THRESHOLDS_ENV} must contain a JSON object")

    per_engine = values.pop(BY_OCR_METHOD_KEY, None)
    if per_engine is not None and not isinstance(per_engine, dict):
        raise ValueError(
            f"{ROUTING_THRESHOLDS_ENV}.{BY_OCR_METHOD_KEY} must be a JSON object "
            f"keyed by ocr_method"
        )

    if per_engine and ocr_method:
        override = per_engine.get(ocr_method)
        if override is not None:
            if not isinstance(override, dict):
                raise ValueError(
                    f"{ROUTING_THRESHOLDS_ENV}.{BY_OCR_METHOD_KEY}.{ocr_method} "
                    f"must be a JSON object"
                )
            # Merged, not replaced: an override says what DIFFERS for this
            # engine. Requiring all sixteen fields per engine would mean
            # three copies of the same numbers drifting apart.
            values = {**values, **override}

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
    if signals.confidence_reported:
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


#: Characters that may appear inside a legitimate numeric token alongside
#: digits. `_WORD_RE` already splits on "." and whitespace, so "145.20"
#: arrives as "145" and "20"; what survives whole are grid references,
#: sample numbers and ranges.
_NUMERIC_TOKEN_EXTRAS = frozenset("-_,'")


def _is_numeric_token(word: str) -> bool:
    """A token made only of digits and numeric separators.

    Added 2026-08-21. `_is_gibberish_word` used to return True for ANY token
    of length >= 4 containing no letters, and in this corpus that is not a
    description of gibberish — it is a description of data:

        612345      easting
        5412345     northing
        1250        elevation, or a sample number
        2022        a year
        22-041      a hole suffix

    Measured against this module on a 20-row collar table shaped
    ``DDH-22-041  612345  5412345  1250``: gibberish_word_ratio was **0.714**
    before this change and 0.000 after. Three of every four tokens on a
    clean, correctly-read page were being called garbage.

    The expensive consequence is not the review queue — it is
    ``pdf_report._native_text_screen_reason``, which runs on the DEFAULT
    path with no calibrated thresholds required. It rejects a native text
    layer when gibberish_word_ratio exceeds
    ``NATIVE_TEXT_MAX_GIBBERISH_RATIO = 0.4``. At 0.714 a **born-digital**
    assay or collar page failed that screen, so its perfect embedded text
    was discarded and the page was routed to OCR — substituting a billed
    Document Intelligence read of a rendered image for text that was already
    correct. Verified end to end: the same page now returns
    ``_native_text_screen_reason(...) is None``.

    Where calibrated thresholds ARE set, the page additionally tiered to
    MandatoryReview -> review_required -> ocr_status='low_confidence', into
    a queue nothing triages. Either way the gate was biased hardest against
    the highest-value pages in the corpus.

    A digit run is still capped for length below: a 24-character number is
    not a coordinate, it is a smeared line.
    """
    if not word:
        return False
    has_digit = False
    for character in word:
        if character.isdigit():
            has_digit = True
        elif character not in _NUMERIC_TOKEN_EXTRAS:
            return False
    return has_digit


def _is_gibberish_word(word: str) -> bool:
    if not word:
        return True
    if _REPEATED_CHARACTER_RE.search(word):
        return True
    alpha_count = sum(character.isalpha() for character in word)
    digit_count = sum(character.isdigit() for character in word)
    symbol_count = len(word) - alpha_count - digit_count
    if len(word) >= 4 and alpha_count == 0 and not _is_numeric_token(word):
        # Letterless AND not a plain number: "|||#", "~~^^", "()()" —
        # the shapes a failed OCR actually produces.
        return True
    if len(word) >= 5 and symbol_count / len(word) > 0.4:
        return True
    if len(word) >= 24 and not any(character in "-'" for character in word):
        return True
    return False


def numeric_token_ratio(words: Sequence[str]) -> float:
    """Fraction of tokens that are plain numbers.

    Reported alongside gibberish_word_ratio rather than folded into it. A
    page that is 70% numbers is a data table, which is a fact worth knowing
    about a page — it is just not evidence that the OCR failed, which is
    what the gibberish signal is asked to mean.
    """
    if not words:
        return 0.0
    return sum(1 for word in words if _is_numeric_token(word)) / len(words)


__all__ = [
    "OcrQualityAssessment",
    "OcrQualitySignals",
    "OcrRoutingThresholds",
    "OcrRoutingTier",
    "ROUTING_THRESHOLDS_ENV",
    "assess_ocr_quality",
    "calculate_ocr_quality",
    "load_routing_thresholds_from_env",
    "numeric_token_ratio",
]
