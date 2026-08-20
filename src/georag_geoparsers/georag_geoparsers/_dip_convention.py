"""Dip sign-convention detection and normalisation.

The silver.collars table enforces dip >= -90 AND dip <= 0 (down-negative).
Some CSV exports use down-positive convention (positive values for downward dip).
This module detects which convention a batch of dip values uses and normalises
them to down-negative before insertion.
"""

from __future__ import annotations

from typing import Literal

DipConvention = Literal["down_negative", "down_positive", "ambiguous"]

_MINIMUM_SAMPLES = 5
_MAJORITY_THRESHOLD = 0.80


def detect_dip_convention(dips: list[float]) -> DipConvention:
    """Classify the sign convention used in *dips*.

    Heuristic:
    - Requires at least ``_MINIMUM_SAMPLES`` non-null values to make a
      confident determination.  Below that, returns ``"down_negative"``
      (the DB convention) implicitly — callers should check and warn.
    - If > 80 % of values are in [-90, 0]  → ``"down_negative"``
    - If > 80 % of values are in [0, 90]   → ``"down_positive"``
    - Otherwise                             → ``"ambiguous"``

    Parameters
    ----------
    dips:
        Raw float dip values from the CSV (NaN/None already filtered out
        by the caller before passing).

    Returns
    -------
    DipConvention
    """
    valid = [d for d in dips if d is not None]
    if len(valid) < _MINIMUM_SAMPLES:
        # Insufficient data — default to DB convention, ambiguous flag handled
        # by callers (they emit a warning regardless).
        return "down_negative"

    neg = sum(1 for d in valid if -90.0 <= d <= 0.0)
    pos = sum(1 for d in valid if 0.0 <= d <= 90.0)
    total = len(valid)

    if neg / total >= _MAJORITY_THRESHOLD:
        return "down_negative"
    if pos / total >= _MAJORITY_THRESHOLD:
        return "down_positive"
    return "ambiguous"


def normalize_dip(value: float, source_convention: DipConvention) -> float:
    """Return *value* normalised to down-negative convention.

    If *source_convention* is ``"down_positive"``, the sign is flipped.
    For ``"down_negative"`` or ``"ambiguous"`` the value is returned as-is.

    Parameters
    ----------
    value:
        Raw dip value from the CSV.
    source_convention:
        Convention detected by :func:`detect_dip_convention`.
    """
    if source_convention == "down_positive":
        return -value
    return value
