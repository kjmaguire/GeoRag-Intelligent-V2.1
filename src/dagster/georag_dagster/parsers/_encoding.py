"""Shared encoding detection helper for CSV parsers.

Uses charset-normalizer to detect the encoding of raw bytes before
decoding. Falls back to utf-8 when confidence is below the threshold.
"""

from __future__ import annotations

import logging
from io import StringIO

logger = logging.getLogger(__name__)

_CONFIDENCE_THRESHOLD = 0.5


def detect_encoding(data: bytes) -> str:
    """Detect the character encoding of *data* using charset-normalizer.

    Returns the encoding name (e.g. "utf-8", "cp1252") as a string.
    Falls back to "utf-8" when the best guess has confidence < 0.5 or
    when the library is unavailable.
    """
    try:
        from charset_normalizer import from_bytes  # noqa: PLC0415
    except ImportError:
        logger.debug("charset_normalizer not available — defaulting to utf-8")
        return "utf-8"

    results = from_bytes(data)
    best = results.best()
    if best is None:
        return "utf-8"

    encoding = best.encoding
    confidence = best.chaos  # chaos is 0.0 for clean text, higher for messy

    # charset-normalizer exposes confidence differently depending on version.
    # We check the 'chaos' score: low chaos = high confidence.  Convert to
    # a 0-1 confidence scale: confidence = 1 - chaos (chaos is 0..1).
    actual_confidence = 1.0 - confidence

    if actual_confidence < _CONFIDENCE_THRESHOLD:
        logger.debug(
            "Encoding detection confidence %.2f below threshold — defaulting to utf-8 "
            "(detected: %s)",
            actual_confidence,
            encoding,
        )
        return "utf-8"

    return encoding


def open_csv_bytes(data: bytes) -> tuple[StringIO, str]:
    """Detect encoding of *data*, decode, and return (StringIO, encoding_name).

    Callers should log the returned encoding at INFO level if it is not utf-8.
    """
    encoding = detect_encoding(data)
    try:
        decoded = data.decode(encoding, errors="replace")
    except (LookupError, UnicodeDecodeError):
        decoded = data.decode("utf-8", errors="replace")
        encoding = "utf-8"
    return StringIO(decoded), encoding
