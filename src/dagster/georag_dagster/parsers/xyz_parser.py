"""XYZ Parser — Geosoft-style ASCII XYZ export files.

Geosoft Oasis montaj can export database channels to whitespace-delimited ASCII
files known as XYZ format.  A typical file looks like:

    / X            Y            LINE    MAG_TMI     MAG_RESID
      495000.0     6220000.0    1010    55432.1     -12.3
      495010.5     6220005.2    1010    55431.9      -8.7

Rules:
  - Lines starting with '/' are comments.
  - The last comment line before data that contains recognisable column-header
    tokens (X, Y, LINE, EASTING, NORTHING, etc.) is treated as the column
    header.
  - Data rows are whitespace-delimited (one or more spaces or tabs).
  - All remaining numeric columns after X, Y, and LINE are "channels"
    (geophysics measurements).

Parse quality metrics and structured error reporting are returned in
XyzParseResult so the caller (Dagster Silver asset) can record them in
materialisation metadata.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

import polars as pl

logger = logging.getLogger(__name__)

PARSER_NAME = "xyz_parser"
PARSER_VERSION = "1.0.0"


def _sha256_file(path: str) -> str:
    """Stream-hash the file at *path*, returning the hex digest."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# Column name tokens that identify X / Y / LINE columns (all matched uppercase)
X_TOKENS: frozenset = frozenset({"X", "EASTING", "E", "UTME", "EAST"})
Y_TOKENS: frozenset = frozenset({"Y", "NORTHING", "N", "UTMN", "NORTH"})
LINE_TOKENS: frozenset = frozenset({"LINE", "LINENO", "LINE_NO", "FID", "LINENUM"})

# Minimum number of known column tokens required to accept a comment line as the
# column header.  At least one of X or Y must appear.
_MIN_KNOWN_TOKENS = 1


@dataclass
class XyzChannel:
    """Statistics for a single geophysics channel (all non-coordinate columns)."""

    name: str
    values: list           # float values; may contain None for null/masked points
    min_value: float
    max_value: float
    unit: str              # None — unit detection is not implemented yet


@dataclass
class XyzParseResult:
    """Container for a completed XYZ file parse run."""

    source_file: str
    channel_count: int
    channels: list          # list of XyzChannel
    point_count: int
    easting_column: str     # detected X column name as it appears in the file
    northing_column: str    # detected Y column name as it appears in the file
    line_column: str        # detected LINE column name, or None
    x_values: list          # float easting values
    y_values: list          # float northing values
    line_values: list       # int line-number values, or None if no LINE column
    parse_errors: list = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _scan_header(path: str) -> tuple:
    """Scan the file for the column-header comment line and the data start row.

    Returns
    -------
    (column_names, data_start_index)
        column_names — list of column name strings extracted from the last
                       qualifying comment line.
        data_start_index — zero-based line index of the first data row.
    """
    column_names = None
    candidate_line = None
    data_start = 0

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for i, raw_line in enumerate(fh):
            stripped = raw_line.strip()
            if not stripped:
                continue
            if stripped.startswith("/"):
                # Strip the leading '/' (and any subsequent slashes / spaces)
                content = stripped.lstrip("/").strip()
                tokens = content.split()
                if not tokens:
                    continue
                tokens_upper = {t.upper() for t in tokens}
                # Accept as a header candidate if it contains at least one known
                # coordinate token (X or Y family).
                if tokens_upper & (X_TOKENS | Y_TOKENS):
                    candidate_line = tokens
            else:
                # First non-comment, non-empty line is the start of data
                data_start = i
                break

    if candidate_line:
        column_names = candidate_line

    return column_names, data_start


def _cast_float(value) -> float:
    """Return float or None; never raises."""
    if value is None:
        return None
    try:
        s = str(value).strip()
        if s in ("", "*", "NaN", "nan", "NULL", "null", "-", "N/A", "n/a"):
            return None
        return float(s)
    except (ValueError, TypeError):
        return None


def _detect_coordinate_columns(column_names: list) -> tuple:
    """Detect easting, northing, and line columns from a list of column names.

    Returns (easting_col, northing_col, line_col) where line_col may be None.
    Falls back to positional defaults (col[0], col[1]) when no token matches.
    """
    upper_map = {c: c.upper() for c in column_names}

    easting_col = next(
        (c for c, u in upper_map.items() if u in X_TOKENS),
        column_names[0] if column_names else None,
    )
    northing_col = next(
        (c for c, u in upper_map.items() if u in Y_TOKENS),
        column_names[1] if len(column_names) > 1 else None,
    )
    line_col = next(
        (c for c, u in upper_map.items() if u in LINE_TOKENS),
        None,
    )

    return easting_col, northing_col, line_col


# ---------------------------------------------------------------------------
# Public parser entry point
# ---------------------------------------------------------------------------

def parse_xyz_file(path: str) -> XyzParseResult:
    """Parse a Geosoft-style XYZ export file.

    Parameters
    ----------
    path:
        Absolute path to the .xyz ASCII file.

    Returns
    -------
    XyzParseResult
        Contains channel data, coordinate lists, and quality metrics.

    Raises
    ------
    ValueError
        If no recognisable column header can be found in the file.
    FileNotFoundError
        If the file does not exist at the given path.
    """
    import os  # noqa: PLC0415

    if not os.path.isfile(path):
        raise FileNotFoundError(f"xyz_parser: file not found at '{path}'")

    filename = path.split("/")[-1]
    parse_errors: list = []
    sha256_hex = _sha256_file(path)

    logger.info("XYZ parse start: file='%s'", filename)

    # --- Step 1: Locate column header and data start ---
    column_names, data_start = _scan_header(path)

    if not column_names:
        raise ValueError(
            f"xyz_parser: could not detect a column header in '{filename}'. "
            "Expected a comment line starting with '/' that contains X/Y/EASTING/NORTHING tokens."
        )

    logger.info(
        "XYZ header detected: columns=%s data_start_row=%d",
        column_names,
        data_start,
    )

    # --- Step 2: Read data rows with Polars ---
    # Polars read_csv with a whitespace separator does not support multiple
    # consecutive whitespace characters natively; use separator=" " and rely on
    # truncate_ragged_lines.  A better approach is to read raw lines and split
    # with Python's str.split() which handles any run of whitespace.
    data_rows: list[list] = []
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line_idx, raw_line in enumerate(fh):
            if line_idx < data_start:
                continue
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("/"):
                continue
            tokens = stripped.split()
            if len(tokens) < len(column_names):
                # Pad with None if row is short
                tokens.extend([None] * (len(column_names) - len(tokens)))
            elif len(tokens) > len(column_names):
                # Truncate extra tokens (shouldn't happen in well-formed files)
                tokens = tokens[: len(column_names)]
            data_rows.append(tokens)

    if not data_rows:
        logger.warning("XYZ: no data rows found in '%s'", filename)
        return XyzParseResult(
            source_file=filename,
            channel_count=0,
            channels=[],
            point_count=0,
            easting_column=column_names[0] if column_names else "X",
            northing_column=column_names[1] if len(column_names) > 1 else "Y",
            line_column=None,
            x_values=[],
            y_values=[],
            line_values=None,
            parse_errors=["No data rows found in file."],
            provenance={
                "source_file_sha256": sha256_hex,
                "parser_name": PARSER_NAME,
                "parser_version": PARSER_VERSION,
                "source_col_map": None,
            },
        )

    # Build a Polars DataFrame for convenience
    # All values are strings at this point; cast explicitly below.
    try:
        df = pl.DataFrame(
            {col: [row[i] if i < len(row) else None for row in data_rows]
             for i, col in enumerate(column_names)},
            schema={col: pl.Utf8 for col in column_names},
        )
    except Exception as exc:
        logger.error("XYZ: failed to build DataFrame for '%s': %s", filename, exc)
        raise

    point_count = len(df)
    logger.info("XYZ: loaded %d data rows, %d columns", point_count, len(column_names))

    # --- Step 3: Detect coordinate / line columns ---
    easting_col, northing_col, line_col = _detect_coordinate_columns(column_names)

    logger.info(
        "XYZ column mapping: easting='%s' northing='%s' line=%r",
        easting_col,
        northing_col,
        line_col,
    )

    # --- Step 4: Extract coordinate arrays ---
    x_values: list[float] = [_cast_float(v) for v in df[easting_col].to_list()]
    y_values: list[float] = [_cast_float(v) for v in df[northing_col].to_list()]

    line_values = None
    if line_col and line_col in df.columns:
        raw_line_vals = df[line_col].to_list()
        parsed_line_vals = []
        for v in raw_line_vals:
            fv = _cast_float(v)
            parsed_line_vals.append(int(fv) if fv is not None else None)
        line_values = parsed_line_vals

    # --- Step 5: Extract channels (every non-coordinate column) ---
    skip_cols: set = {easting_col, northing_col}
    if line_col:
        skip_cols.add(line_col)

    channels: list[XyzChannel] = []
    for col in column_names:
        if col in skip_cols:
            continue
        if col not in df.columns:
            continue
        try:
            raw_vals = df[col].to_list()
            float_vals = [_cast_float(v) for v in raw_vals]
            clean = [v for v in float_vals if v is not None]
            if not clean:
                logger.warning(
                    "XYZ: channel '%s' has no numeric values — skipping", col
                )
                parse_errors.append(f"Channel '{col}' has no numeric values.")
                continue
            channels.append(
                XyzChannel(
                    name=col,
                    values=float_vals,
                    min_value=min(clean),
                    max_value=max(clean),
                    unit=None,
                )
            )
        except Exception as exc:
            logger.warning(
                "XYZ: failed to parse channel '%s': %s — skipping", col, exc
            )
            parse_errors.append(f"Channel '{col}' parse failed: {exc}")
            continue

    # Build coordinate column map: output_field → source_field_name as it appears in the file.
    _col_map: dict[str, str] = {
        "easting": easting_col,
        "northing": northing_col,
    }
    if line_col is not None:
        _col_map["line"] = line_col

    result = XyzParseResult(
        source_file=filename,
        channel_count=len(channels),
        channels=channels,
        point_count=point_count,
        easting_column=easting_col,
        northing_column=northing_col,
        line_column=line_col,
        x_values=x_values,
        y_values=y_values,
        line_values=line_values,
        parse_errors=parse_errors,
        provenance={
            "source_file_sha256": sha256_hex,
            "parser_name": PARSER_NAME,
            "parser_version": PARSER_VERSION,
            "source_col_map": _col_map,
        },
    )

    logger.info(
        "XYZ parse complete: file='%s' points=%d channels=%d errors=%d",
        filename,
        point_count,
        len(channels),
        len(parse_errors),
    )

    return result
