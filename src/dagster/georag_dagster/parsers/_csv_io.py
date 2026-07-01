"""Shared CSV I/O helpers for the GeoRAG CSV parser suite.

Provides:
  - DEFAULT_NULL_VALUES  — single source of truth for Polars null_values.
  - open_csv_with_encoding(source) — read raw bytes, detect encoding via
    charset-normalizer, return (StringIO, encoding_name, sha256_hex, byte_count).
  - detect_delimiter(content) — peek the first 5 lines and choose
    between ``,``, ``;``, ``\\t``, ``|`` by count + variance. Added
    2026-05-23 because EU-export semicolon CSVs were silently collapsing
    to a single column under Polars' default comma separator.
  - transform_decimal_comma(df) — column-aware EU decimal-comma transform.
    Replaces ``_check_decimal_comma`` which detected-and-warned only.
    Per the original docstring: "Decimal-comma detection is Sprint-2 scope.
    In Sprint 1 we detect and warn only." This *is* Sprint 2.
"""

from __future__ import annotations

import hashlib
import logging
import re
from io import StringIO
from pathlib import Path
from typing import IO, Union

import polars as pl

from georag_dagster.parsers._encoding import open_csv_bytes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Single source of truth for null string values used by Polars ingest.
#
# DEFAULT_NULL_VALUES applies to all non-assay parsers (collar, survey, lithology).
#
# SAMPLE_NULL_VALUES is the reduced set for the sample parser — it deliberately
# excludes below-detection tokens (BDL, <LOD, etc.) because those cells must
# reach the assay-parse helper as raw strings, not be silently nulled by Polars.
# The assay-parse path handles them via _parse_assay_value.
# ---------------------------------------------------------------------------

DEFAULT_NULL_VALUES: list[str] = [
    "-", "N/A", "NULL", "null", "n/a", "na", "NA", "NONE", "none", "",
    "<DL", "<LOD", "BDL", "bdl", "N.A.", "ND", "nd",
]

# Reduced null list for the sample CSV parser — assay-specific BDL tokens are
# intentionally omitted so they arrive as raw strings to _parse_assay_value.
SAMPLE_NULL_VALUES: list[str] = [
    "-", "N/A", "NULL", "null", "n/a", "na", "NA", "NONE", "none", "",
    "N.A.", "ND", "nd",
]


def _sha256_hex(raw: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of *raw*."""
    return hashlib.sha256(raw).hexdigest()


def open_csv_with_encoding(
    source: Union[str, Path, IO],  # noqa: UP007
) -> tuple[StringIO, str, str, int]:
    """Read *source* as bytes, detect encoding, return (StringIO, encoding_name, sha256_hex, byte_count).

    Parameters
    ----------
    source:
        A file path (str or Path) or a file-like object (text or binary).
        If the object is already a text stream (str content) it is wrapped
        directly and encoding is reported as "utf-8".

    Returns
    -------
    (StringIO, encoding_name, sha256_hex, byte_count)
        The decoded content wrapped in a StringIO, the detected encoding name,
        the lowercase hex SHA-256 of the raw bytes consumed, and the raw byte
        count.  For already-decoded text streams the hash is computed over the
        UTF-8 re-encoding of the string.

    Notes
    -----
    The sha256 is computed ONCE over the full raw bytes at read time — callers
    must not re-hash per-row.  For non-seekable file-like inputs the bytes are
    tee'd through a BytesIO during reading so the hash can still be computed.
    """
    raw: bytes

    if isinstance(source, (str, Path)):
        with open(str(source), "rb") as fh:
            raw = fh.read()
        stream, encoding = open_csv_bytes(raw)

    elif hasattr(source, "read"):
        content = source.read()
        if isinstance(content, bytes):
            raw = content
            stream, encoding = open_csv_bytes(raw)
        else:
            # Already decoded text — hash the UTF-8 re-encoding for consistency
            raw = content.encode("utf-8")
            stream = StringIO(content)
            encoding = "utf-8"
    else:
        # Assume already a string
        raw = str(source).encode("utf-8")
        stream = StringIO(str(source))
        encoding = "utf-8"

    sha256 = _sha256_hex(raw)
    byte_count = len(raw)
    return stream, encoding, sha256, byte_count


def _check_decimal_comma(content: str, encoding: str) -> bool:
    """Heuristic: detect European decimal-comma convention.

    Returns True if the file looks like it uses commas as decimal separators
    (e.g. "1,23" instead of "1.23").

    Retained for back-compat with the existing warn-only call sites in the
    four CSV parsers. New code should prefer :func:`transform_decimal_comma`
    which actually fixes the values rather than just warning about them.
    """
    if encoding.lower() in ("cp1252", "windows-1252", "latin-1", "iso-8859-1"):
        # Check for semicolon delimiter (strong signal for European CSVs)
        first_line = content.split("\n", 1)[0] if "\n" in content else content
        if ";" in first_line:
            return True
        # Check for numeric comma patterns: digit,digit
        if re.search(r"\b\d+,\d+\b", content[:2000]):
            return True
    return False


# ---------------------------------------------------------------------------
# Delimiter auto-detection (added 2026-05-23 per CSV audit gap #1)
# ---------------------------------------------------------------------------

_DELIMITER_CANDIDATES: tuple[str, ...] = (",", ";", "\t", "|")
_DETECT_LINE_LIMIT: int = 5  # examine first 5 non-empty lines


def detect_delimiter(content: str, default: str = ",") -> str:
    """Pick the most likely CSV delimiter from a content sample.

    Strategy: examine the first ``_DETECT_LINE_LIMIT`` non-empty lines.
    For each candidate delimiter compute (total_count, variance_across_lines).
    The right delimiter:

      * appears at least once (else max_count == 0, skip)
      * is the most populous (higher total_count means it's actually a
        separator, not incidental punctuation)
      * has the lowest variance across lines (well-formed CSV has the
        same number of separators per row — give or take quoted fields)

    Tie-break: ``default`` (which the caller can set to the legacy comma
    behaviour, so detection is strictly additive).

    Examples
    --------
    >>> detect_delimiter("a,b,c\\n1,2,3\\n")
    ','
    >>> detect_delimiter("a;b;c\\n1;2;3\\n")
    ';'
    >>> detect_delimiter("a\\tb\\tc\\n1\\t2\\t3\\n")
    '\\t'
    """
    lines = [ln for ln in content.splitlines() if ln.strip()][:_DETECT_LINE_LIMIT]
    if not lines:
        return default

    best_delim = default
    best_total = 0
    best_variance = float("inf")

    for delim in _DELIMITER_CANDIDATES:
        counts = [ln.count(delim) for ln in lines]
        if max(counts) == 0:
            continue
        total = sum(counts)
        mean = total / len(counts)
        variance = sum((c - mean) ** 2 for c in counts) / len(counts)

        # Higher total wins; on tie, lower variance wins.
        if total > best_total or (total == best_total and variance < best_variance):
            best_delim = delim
            best_total = total
            best_variance = variance

    return best_delim


# ---------------------------------------------------------------------------
# Decimal-comma transformation (added 2026-05-23 per CSV audit gap #2)
# ---------------------------------------------------------------------------

# Matches a number written with comma as decimal separator and NO period.
# Examples: "1,5", "-2,33", "1234,567"
# Does NOT match US thousand-separators like "1,234" (because we also require
# a fractional part of at least 1 digit after the comma in a way that's
# distinct from grouping). To distinguish: if the only commas in the column
# are followed by exactly 3 digits AND there's a period elsewhere, it's
# thousand-separator; we skip transformation. The pattern below admits any
# digit count after the comma, but the per-column gate (all values match
# the strict pattern) rules out the thousand-separator case naturally
# because a value like "1,234.56" would NOT match (contains a period).
_DECIMAL_COMMA_RE: re.Pattern = re.compile(r"^-?\d+,\d+$")
_PLAIN_INT_RE: re.Pattern = re.compile(r"^-?\d+$")

# Sample size — checking every cell is wasteful on big files. 500 rows is
# enough to be confident about whether the column uses decimal-comma
# consistently. False positives on a partial sample are mitigated by the
# all-must-match gate.
_DECIMAL_COMMA_SAMPLE_SIZE: int = 500


def transform_decimal_comma(
    df: pl.DataFrame,
    *,
    sample_size: int = _DECIMAL_COMMA_SAMPLE_SIZE,
) -> tuple[pl.DataFrame, list[str]]:
    """For each Utf8 column in ``df``, if every non-null value in the
    first ``sample_size`` rows matches the decimal-comma pattern, replace
    the comma with a period in-place. Returns ``(transformed_df,
    list_of_columns_transformed)``.

    Rules:
      * Column must be string-typed (Utf8). Already-numeric columns are
        left alone.
      * Column must have at least one non-null value.
      * Every sampled value must match either ``-?\\d+,\\d+`` (decimal
        comma) OR ``-?\\d+`` (plain integer — leaves room for mixed
        integer/decimal columns).
      * At least one sampled value must contain a comma (else there's
        nothing to transform).
      * A period anywhere in the sample disqualifies the column. This
        is the rule that distinguishes EU decimal-comma from US
        thousand-separator (``1,234.56``).

    Side effects: only the matching columns are rewritten. Other columns
    (text, IDs, dates) pass through untouched.
    """
    transformed: list[str] = []
    for col in df.columns:
        if df.schema[col] != pl.Utf8:
            continue
        non_null = df[col].drop_nulls()
        if non_null.is_empty():
            continue
        sample = non_null.head(sample_size).to_list()
        if not sample:
            continue

        # Disqualifiers
        has_comma_decimal = False
        all_match = True
        for v in sample:
            s = str(v).strip()
            if not s:
                # treat empty as match (will become null downstream)
                continue
            if "." in s:
                all_match = False
                break
            if _DECIMAL_COMMA_RE.match(s):
                has_comma_decimal = True
                continue
            if _PLAIN_INT_RE.match(s):
                continue
            # Anything else (text, mixed punctuation, units): not a
            # decimal-comma numeric column.
            all_match = False
            break

        if all_match and has_comma_decimal:
            df = df.with_columns(
                pl.col(col).str.replace(",", ".", literal=True).alias(col)
            )
            transformed.append(col)

    return df, transformed
