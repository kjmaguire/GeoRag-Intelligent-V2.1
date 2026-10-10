"""Shared CSV I/O helpers for the GeoRAG CSV parser suite.

Provides:
  - DEFAULT_NULL_VALUES  — single source of truth for Polars null_values.
  - open_csv_with_encoding(source) — read raw bytes, detect encoding via
    charset-normalizer, return (StringIO, encoding_name, sha256_hex, byte_count).
  - detect_delimiter(content) — peek the first 5 lines and choose
    between ``,``, ``;``, ``\\t``, ``|`` by count + variance. Added
    2026-05-23 because EU-export semicolon CSVs were silently collapsing
    to a single column under Polars' default comma separator.
  - count_preamble_lines(content) — title / comment lines above the header
    row (ING-13). ``open_csv_with_encoding`` drops them from the stream it
    returns and records how many on ``stream.preamble_lines``.
  - transform_decimal_comma(df) — column-aware EU decimal-comma transform.
    Replaces ``_check_decimal_comma`` which detected-and-warned only.
    Per the original docstring: "Decimal-comma detection is Sprint-2 scope.
    In Sprint 1 we detect and warn only." This *is* Sprint 2.
  - read_csv_checked(content, ...) — the one place the drill CSV parsers turn
    text into a Polars frame. A line with MORE fields than the header used to
    be silently truncated (``truncate_ragged_lines=True``), which is a column
    shift, not a truncation: an unquoted comma in ``Granite, coarse grained``
    moved every later value one column left and lost the last. Such rows are
    now located, reported (``ragged_row``) and skipped by the parser.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import re
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import IO, Any, Union

import polars as pl

from georag_geoparsers._encoding import open_csv_bytes

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

    # A title or comment block above the header ("# Exported from ...",
    # "Acme Gold Corp - Assays") made the parsers read the title as the
    # header and refuse the file (ING-13). Dropped here, once, so every
    # parser and the workflow's own header read agree; the hash above is
    # still over the bytes as uploaded.
    text = stream.getvalue()
    preamble = count_preamble_lines(text)
    if preamble:
        # Cut at the offset of the first kept line: splitting the WHOLE text
        # into lines to drop the first few made a second, line-by-line copy
        # of a file that can be 150 MB.
        skipped_chars = sum(
            len(line) for line in head_lines(text, preamble, keepends=True)
        )
        stream = StringIO(text[skipped_chars:])
        logger.info("csv_io: skipped %d preamble line(s) above the header", preamble)
    stream.preamble_lines = preamble  # type: ignore[attr-defined]
    return stream, encoding, sha256, byte_count


#: Characters of a text examined when only its first few lines are wanted.
_HEAD_CHARS: int = 1_000_000


def head_lines(
    content: str, limit: int, *, non_empty: bool = False, keepends: bool = False,
) -> list[str]:
    """The first *limit* lines of *content* (non-blank ones when *non_empty*).

    Reads a bounded prefix instead of ``content.splitlines()``: the header
    checks (delimiter, preamble, header row) only ever want a handful of
    lines, and splitting a 150 MB file into 2 million line objects to read
    five of them was one of the copies that made the CSV path hold about nine
    times the file in memory. The prefix doubles until enough complete lines
    are in it, so a file with enormous lines still answers correctly.
    """
    size = _HEAD_CHARS
    while True:
        chunk = content[:size]
        complete = len(content) <= size
        lines = chunk.splitlines(keepends=keepends)
        if not complete and lines:
            lines = lines[:-1]      # the last line of a cut prefix may be partial
        if non_empty:
            lines = [ln for ln in lines if ln.strip()]
        if complete or len(lines) >= limit:
            return lines[:limit]
        size *= 4


#: How many leading lines may be a title/comment block (ING-13).
_PREAMBLE_SCAN_LINES: int = 15
#: How many lines are sampled to learn the table's width.
_PREAMBLE_SAMPLE_LINES: int = 60


def count_preamble_lines(content: str) -> int:
    """How many leading lines of *content* sit ABOVE the header row.

    The table's width is the number of filled fields most sampled lines
    share. A leading line is preamble when it fills fewer than half of that
    (and fewer than 2) - a title, a "# exported by" comment, a blank line.
    The first line that fills at least half the width is the header. A file
    whose first line already does returns 0, so ordinary files are
    untouched; so does anything too narrow (one column) to judge.

    A header that merely STARTS with ``#`` ("#HoleID,From,To") is as wide as
    the data and is kept.
    """
    import csv
    from collections import Counter

    lines = head_lines(content, _PREAMBLE_SAMPLE_LINES)
    if len(lines) < 2:
        return 0

    #: (lines sharing the modal width, modal width, widths). The delimiter
    #: that splits MOST lines to one consistent width wins: "Sample;Au_ppm /
    #: P1;0,016" is a two-column ';' table, not a ',' table with a one-field
    #: header.
    best: tuple[int, int, list[int]] | None = None
    for delimiter in _DELIMITER_CANDIDATES:
        try:
            rows = list(csv.reader(lines, delimiter=delimiter))
        except csv.Error:
            logger.debug("csv_io: preamble scan could not split on %r", delimiter, exc_info=True)
            continue
        widths = [sum(1 for cell in row if cell.strip()) for row in rows]
        wide = [w for w in widths if w >= 2]
        if not wide:
            continue
        common, shared = Counter(wide).most_common(1)[0]
        if best is None or (shared, common) > (best[0], best[1]):
            best = (shared, common, widths)
    if best is None:
        return 0

    _shared, common, widths = best
    threshold = max(2, -(-common // 2))       # ceil(common / 2), at least 2
    for index, width in enumerate(widths[:_PREAMBLE_SCAN_LINES]):
        # A comment-marked line ("# Exported, 2024") is the header only if it
        # is as wide as the table; its one comma must not make it one.
        commented = lines[index].lstrip().startswith(_COMMENT_MARKERS)
        if width >= (common if commented else threshold):
            return index
    return 0


#: Leading characters that mark a comment line in exported CSVs.
_COMMENT_MARKERS: tuple[str, ...] = ("#", "'", "//", "!")


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
    lines = head_lines(content, _DETECT_LINE_LIMIT, non_empty=True)
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
# Ragged lines (audit finding 9)
# ---------------------------------------------------------------------------

#: Fields of a ragged line kept in its skip entry / warning examples.
_RAGGED_VALUES_KEPT: int = 16
#: Row numbers a ragged_row warning lists, and examples it quotes.
_RAGGED_ROWS_LISTED: int = 20
_RAGGED_EXAMPLES: int = 3


@dataclass(frozen=True)
class RaggedRow:
    """One CSV line that has more fields than the header."""

    #: Numbered as the parsers number rows: the header is row 1.
    row: int
    fields: int
    expected: int
    values: tuple[str, ...]

    def skip_entry(self) -> dict[str, Any]:
        """The line in the parsers' ``skipped_details`` shape."""
        return {
            "row": self.row,
            "code": "ragged_row",
            "reason": (
                f"row {self.row}: {self.fields} fields but the header has "
                f"{self.expected} - a value probably contains an unquoted "
                f"delimiter, which shifts every later value one column"
            ),
            "raw": {"fields": list(self.values)},
            "expected": f"{self.expected} fields",
            "actual": self.fields,
            "suggestion": (
                "Wrap the value that contains the delimiter in double quotes "
                '("Granite, coarse grained") and upload the file again.'
            ),
        }


@dataclass
class RaggedRows:
    """The lines of one CSV that are wider than its header.

    Polars' ``truncate_ragged_lines=True`` drops the surplus fields of such a
    line and keeps the rest, which reads as a harmless truncation and is a
    column shift: ``DH-1,0,10,GR,Granite, coarse grained,pink`` under a
    ``hole,from,to,lith,desc,color`` header gave ``desc='Granite'``,
    ``color='coarse grained'`` and lost ``pink``, with no sign anything was
    wrong. The parsers now skip these lines (:meth:`skip`) and report them
    (:meth:`warnings`), so a value that moved is never stored.

    A line with FEWER fields keeps its leading values where they were (the
    missing trailing ones are empty), and surplus fields that are all empty
    (a trailing delimiter) lose nothing - neither is ragged here.
    """

    rows: dict[int, RaggedRow] = field(default_factory=dict)
    #: Polars refused the file for ragged lines but they could not be located
    #: (a field over the csv module's size limit, or a row count that does
    #: not agree with Polars'). The lines were truncated as before; the
    #: warning says so instead of staying silent.
    unlocated: bool = False

    def __bool__(self) -> bool:
        return bool(self.rows) or self.unlocated

    def __len__(self) -> int:
        return len(self.rows)

    def skip(self, row: int, skipped: list) -> bool:
        """Record *row* in *skipped* when it is ragged; True if it was."""
        entry = self.rows.get(row)
        if entry is None:
            return False
        skipped.append(entry.skip_entry())
        return True

    def blank_rows(self, df: pl.DataFrame) -> pl.DataFrame:
        """*df* with every ragged row's cells emptied, row positions unchanged.

        The file-level passes that read the whole column before any row is
        judged (dip convention, coordinate system, the long-format pivot,
        decimal-comma detection) must not see the shifted values of a row the
        parser is about to skip. Emptying - not dropping - keeps every other
        row at the position its row number says.
        """
        if not self.rows or df.is_empty():
            return df
        ragged = pl.Series("_ragged", [(i + 2) in self.rows for i in range(len(df))])
        return df.with_columns(
            pl.when(ragged).then(None).otherwise(pl.col(c)).alias(c)
            for c in df.columns
        )

    def warnings(self) -> list[dict[str, Any]]:
        """The file-level warning for these lines (empty when none)."""
        if self.rows:
            numbers = sorted(self.rows)
            shown = numbers[:_RAGGED_ROWS_LISTED]
            first = self.rows[numbers[0]]
            examples = [
                list(self.rows[n].values) for n in numbers[:_RAGGED_EXAMPLES]
            ]
            listed = ", ".join(str(n) for n in shown) + (
                f" (+{len(numbers) - len(shown)} more)"
                if len(numbers) > len(shown) else ""
            )
            return [{
                "row": None,
                "code": "ragged_row",
                "message": (
                    f"{len(numbers)} row(s) have more fields than the header "
                    f"({first.expected}) and were skipped"
                ),
                "detail": (
                    f"Row(s) {listed} have more fields than the header's "
                    f"{first.expected}. That usually means a value contains "
                    f"an unquoted delimiter (for example 'Granite, coarse "
                    f"grained'), which would have moved every later value "
                    f"one column to the left and dropped the last. Those "
                    f"rows were not read; the rest of the file was. Wrap "
                    f"the value in double quotes and upload the file again. "
                    f"First row: {list(first.values)!r}"
                )[:900],
                "context": {
                    "count": len(numbers),
                    "expected_fields": first.expected,
                    "rows": shown,
                    "examples": examples,
                },
            }]
        if self.unlocated:
            return [{
                "row": None,
                "code": "ragged_row",
                "message": (
                    "some rows have more fields than the header; the surplus "
                    "fields were dropped and the rows could not be located"
                ),
                "detail": (
                    "At least one row of this file has more fields than the "
                    "header (usually an unquoted delimiter inside a value). "
                    "The surplus fields were dropped, so the values after the "
                    "stray delimiter on that row sit one column to the left "
                    "of where they belong, and the row could not be "
                    "identified. Quote values that contain the delimiter and "
                    "upload the file again."
                ),
                "context": {"located": False},
            }]
        return []


def locate_ragged_rows(
    content: str, *, separator: str, data_rows: int,
) -> RaggedRows:
    """Find the lines of *content* wider than its header.

    A streaming pass of the csv module over the text - no rows are kept - run
    only for a file Polars has already refused as ragged, so the common case
    pays nothing. Surplus fields that are all blank (a trailing delimiter) do
    not count. ``data_rows`` is how many rows Polars read: if the scan sees a
    different number the positions cannot be trusted and nothing is guessed.
    """
    found: dict[int, RaggedRow] = {}
    seen = 0
    try:
        reader = csv.reader(StringIO(content, newline=""), delimiter=separator)
        header = next(reader, None)
        if not header:
            return RaggedRows(unlocated=True)
        width = len(header)
        for seen, record in enumerate(reader, start=1):
            if len(record) > width and any(c.strip() for c in record[width:]):
                row = seen + 1
                found[row] = RaggedRow(
                    row=row, fields=len(record), expected=width,
                    values=tuple(record[:_RAGGED_VALUES_KEPT]),
                )
    except csv.Error:
        logger.warning("csv_io: could not scan for ragged lines", exc_info=True)
        return RaggedRows(unlocated=True)
    if seen != data_rows:
        logger.warning(
            "csv_io: ragged-line scan saw %d row(s), Polars read %d - "
            "positions not trusted", seen, data_rows,
        )
        return RaggedRows(unlocated=True)
    # An all-blank-surplus file (trailing delimiters) is not ragged at all.
    return RaggedRows(rows=found)


def read_csv_checked(
    content: str, *, separator: str, null_values: list[str],
) -> tuple[pl.DataFrame, RaggedRows]:
    """``pl.read_csv`` for the drill parsers, minus the silent column shift.

    Every column is read as text (``infer_schema=False``) exactly as the
    parsers always did. A file Polars accepts is returned untouched. One with
    lines wider than the header is re-read, the cells of the ragged rows are
    emptied (:meth:`RaggedRows.blank_rows`, so no whole-column pass sees their
    shifted values), and the :class:`RaggedRows` come back with it: the parser
    skips those rows and reports them rather than store shifted values.

    The text goes to Polars as bytes. A text ``StringIO`` is realised at four
    bytes per character before Polars reads it, which was a large part of the
    CSV path's memory use.
    """
    data = content.encode("utf-8")
    options: dict[str, Any] = {
        "separator": separator, "infer_schema": False, "null_values": null_values,
    }
    try:
        return pl.read_csv(data, **options), RaggedRows()
    except pl.exceptions.ComputeError as exc:
        if "truncate_ragged_lines" not in str(exc):
            raise
        logger.info("csv_io: lines wider than the header - locating them")
    df = pl.read_csv(data, truncate_ragged_lines=True, **options)
    ragged = locate_ragged_rows(content, separator=separator, data_rows=len(df))
    return ragged.blank_rows(df), ragged


# ---------------------------------------------------------------------------
# Decimal-comma transformation (added 2026-05-23 per CSV audit gap #2)
# ---------------------------------------------------------------------------

# Matches a number written with comma as decimal separator and NO period.
# Examples: "1,5", "-2,33", "1234,567"
# A value like "1,234.56" does NOT match (it contains a period), which is how
# a US thousands-separated column is told apart from a decimal-comma one. A
# column whose every comma group is exactly three digits ("1,250", "12,500")
# is the case that cannot be told apart by shape: see _THOUSANDS_SHAPED_RE.
_DECIMAL_COMMA_RE: re.Pattern = re.compile(r"^-?\d+,\d+$")
_PLAIN_INT_RE: re.Pattern = re.compile(r"^-?\d+$")

# A censored lab result: "<0,005", "> 10", "<5". The number inside is judged
# like any other cell of the column. One "<0,005" among a column of decimal
# commas used to disqualify the whole column, so every "0,52" in it stayed text
# and no value of the column was read (audit finding 10).
_CENSORED_RE: re.Pattern = re.compile(r"^[<>]\s*(-?\d+(?:,\d+)?)$")

# Non-numeric lab results that sit among numbers in an assay column and say
# nothing about its number format: below detection, not detected, not sampled,
# no result, insufficient sample.
_RESULT_TOKENS: frozenset[str] = frozenset({
    "bdl", "lod", "<lod", "<dl", "nd", "n.d.", "ns", "nr", "is",
})

# What a US thousands group looks like: one to three digits, no leading zero,
# then exactly three. "1,250" is 1250 there and 1.25 in a decimal-comma file;
# "0,750" and "12,5" cannot be thousands, so any such cell is decimal evidence.
_THOUSANDS_SHAPED_RE: re.Pattern = re.compile(r"^-?[1-9]\d{0,2},\d{3}$")

# The rewrite, applied per CELL: a decimal-comma number (optionally censored)
# gets its comma turned into a point and every other cell of the column - a
# later "n/a, see note" the sample never reached - is left exactly as written.
_REWRITE_RE: str = r"^(\s*[<>]?\s*-?\d+),(\d+)\s*$"

# Sample size — checking every cell is wasteful on big files. 500 rows is
# enough to be confident about whether the column uses decimal-comma
# consistently. False positives on a partial sample are mitigated by the
# all-must-match gate.
_DECIMAL_COMMA_SAMPLE_SIZE: int = 500


class DecimalCommaColumns(list):
    """The columns :func:`transform_decimal_comma` rewrote.

    A plain ``list`` of column names to every existing caller, which unpack
    ``df, transformed = transform_decimal_comma(df)``. ``ambiguous`` carries
    the columns it declined to guess about (``{column: example cells}``), and
    :meth:`ambiguity_warnings` turns them into the run's warning.
    """

    def __init__(self, columns=(), ambiguous: dict[str, list[str]] | None = None):
        super().__init__(columns)
        self.ambiguous: dict[str, list[str]] = dict(ambiguous or {})

    def ambiguity_warnings(self) -> list[dict[str, Any]]:
        """``decimal_comma_ambiguous`` for the columns left as written."""
        if not self.ambiguous:
            return []
        columns = list(self.ambiguous)
        shown = "; ".join(
            f"{col!r}: {', '.join(repr(c) for c in cells)}"
            for col, cells in list(self.ambiguous.items())[:6]
        )
        return [{
            "row": None,
            "code": "decimal_comma_ambiguous",
            "message": (
                f"{len(columns)} column(s) hold values like '1,250' that could "
                f"be decimals or thousands, and were left as written"
            ),
            "detail": (
                f"Every value with a comma in {', '.join(repr(c) for c in columns[:6])} "
                f"has exactly three digits after it, so it is 1.25 in a "
                f"decimal-comma file and 1250 in a thousands-separated one "
                f"(a thousand-fold difference). Nothing was converted; "
                f"numeric values in these columns will be rejected as "
                f"unreadable. Examples: {shown}. Re-export the column without "
                f"the comma grouping (or with a point decimal) and upload "
                f"the file again."
            )[:900],
            "context": {"columns": columns, "examples": self.ambiguous},
        }]


def transform_decimal_comma(
    df: pl.DataFrame,
    *,
    sample_size: int = _DECIMAL_COMMA_SAMPLE_SIZE,
) -> tuple[pl.DataFrame, DecimalCommaColumns]:
    """For each Utf8 column in ``df``, if every non-null value in the
    first ``sample_size`` rows is a decimal-comma number (or a plain integer,
    or a censored/non-result lab entry), replace the comma with a period in
    the cells that are decimal-comma numbers. Returns ``(transformed_df,
    columns_transformed)``.

    Rules:
      * Column must be string-typed (Utf8). Already-numeric columns are
        left alone.
      * Column must have at least one non-null value.
      * Every sampled value must match one of ``-?\\d+,\\d+`` (decimal
        comma), ``-?\\d+`` (plain integer - leaves room for mixed
        integer/decimal columns), a censored result ``<0,005`` / ``> 10``
        whose number is one of those two (audit finding 10: one "<0,005" used
        to disqualify the whole column), or a non-numeric lab result token
        (``BDL``, ``<DL``, ``NS``, ...).
      * At least one sampled value must contain a comma (else there's
        nothing to transform).
      * A period anywhere in the sample disqualifies the column. This
        is the rule that distinguishes EU decimal-comma from US
        thousand-separator (``1,234.56``).
      * AMBIGUOUS columns are not converted (audit finding 17): when every
        comma group is exactly three digits with no leading zero (``1,250``,
        ``12,500``) the column reads equally as 1.25 / 12.5 or 1250 / 12500.
        It is left as written and reported in ``.ambiguous``; one cell that
        cannot be a thousands group (``0,750``, ``12,5``) settles it as
        decimal.

    Side effects: only the matching columns are rewritten, and within them
    only the cells that are decimal-comma numbers. Other columns (text, IDs,
    dates) pass through untouched.
    """
    transformed = DecimalCommaColumns()
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
        decimal_evidence = False
        thousands_cells: list[str] = []
        all_match = True
        for v in sample:
            s = str(v).strip()
            if not s:
                # treat empty as match (will become null downstream)
                continue
            if "." in s:
                all_match = False
                break
            censored = _CENSORED_RE.match(s)
            body = censored.group(1) if censored else s
            if _DECIMAL_COMMA_RE.match(body):
                has_comma_decimal = True
                if _THOUSANDS_SHAPED_RE.match(body):
                    thousands_cells.append(s)
                else:
                    decimal_evidence = True
                continue
            if _PLAIN_INT_RE.match(body) or s.lower() in _RESULT_TOKENS:
                continue
            # Anything else (text, mixed punctuation, units): not a
            # decimal-comma numeric column.
            all_match = False
            break

        if not (all_match and has_comma_decimal):
            continue
        if not decimal_evidence:
            transformed.ambiguous[col] = list(dict.fromkeys(thousands_cells))[:3]
            continue
        df = df.with_columns(
            pl.col(col).str.replace(_REWRITE_RE, "${1}.${2}").alias(col)
        )
        transformed.append(col)

    return df, transformed
