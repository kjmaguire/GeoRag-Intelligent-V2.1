"""XYZ Parser — Geosoft-style ASCII XYZ export files.

Geosoft Oasis montaj exports database channels to whitespace-delimited ASCII
known as XYZ format. The shape a real export takes::

    / ------------------------------------------------
    / XYZ EXPORT [03/15/2019]
    / DATABASE  [C:\\Jobs\\Sitka\\mag.gdb]
    / ------------------------------------------------
    /
    /        X            Y          FID     MAG_TMI     MAG_RESID
    /
    Line 1010
       495000.0    6220000.0         1     55432.1        -12.3
       495010.5    6220005.2         2     55431.9         -8.7
    Tie 9010
       ...

Rules
-----
* Lines starting with ``/`` are comments. The LAST comment line before the
  data that names a coordinate column (X / Y / EASTING / LAT / ...) is the
  column header. An export written without the ``/`` (a header row of plain
  words as the first non-comment line) is also accepted.
* ``Line <id>``, ``Tie <id>`` and ``Trend <id>`` rows are Geosoft's LINE
  MARKERS: every data row after one belongs to that line until the next
  marker. This is how Geosoft writes lines by default — there is usually no
  LINE column at all. The previous parser did not know about markers: a
  ``Line 1010`` row was read as a data row whose X was the word "Line", so
  every file in Geosoft's default layout lost its line structure and carried
  one bogus all-null row per line.
* When there are no markers, a LINE column (LINE / LINENO / LINE_NO ...)
  splits the rows into lines on each change of value. ``FID`` is NOT a line
  column: it is the fiducial (sample counter) and stays a channel — the old
  token list treated it as the line number, which made every row its own
  "line".
* When there is neither, the file is one block of ``points`` (a ground
  station grid, say).
* ``*`` is Geosoft's dummy, and so is any value at or below ``-1e31``
  (``-1.0E32`` is the GX dummy for real channels). Both become None.

Per-row validation: a row with the wrong number of fields, or whose X/Y is
not a number, is SKIPPED with a reason (``XyzRowIssue``) rather than padded,
truncated or allowed to fail the file. Padding a short row with None — what
this used to do — shifts nothing visibly and silently misaligns every
channel to the right of the missing field.

Coordinates are returned verbatim, in whatever CRS the file is in. Nothing
here decides that CRS: an XYZ file declares none, and choosing one is the
caller's job (declared / project / assumed, with warnings) — see
``app/services/ingest/geophysics_writer.py``.

Streaming: :func:`iter_xyz_lines` yields one line (or one segment of a very
long line) at a time, so a 500 MB airborne survey is never held in memory
whole. :func:`parse_xyz_file` is the all-at-once convenience built on it.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import hashlib
import logging
import math
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

PARSER_NAME = "xyz_parser"
PARSER_VERSION = "2.0.0"

#: Column-name tokens (upper-cased, punctuation removed) that name an axis.
#: A projected pair wins over a geographic one when a file carries both —
#: Geosoft exports routinely hold X/Y AND LAT/LONG, and the projected pair
#: is the one the survey was flown in.
X_TOKENS: frozenset = frozenset({
    "X", "EASTING", "EAST", "E", "UTME", "UTMEAST", "UTMX", "XUTM", "XCOORD",
})
Y_TOKENS: frozenset = frozenset({
    "Y", "NORTHING", "NORTH", "N", "UTMN", "UTMNORTH", "UTMY", "YUTM", "YCOORD",
})
LON_TOKENS: frozenset = frozenset({"LON", "LONG", "LONGITUDE", "LONGDD", "LONDD"})
LAT_TOKENS: frozenset = frozenset({"LAT", "LATITUDE", "LATDD"})

#: Datum-suffixed spellings ``X_NAD83`` / ``Y_WGS84`` / ``LAT_WGS84``.
_AXIS_WITH_SUFFIX = re.compile(r"^(X|Y|LAT|LON|LONG)_?(NAD|WGS|GDA|ED|SAD|UTM)", re.I)

#: The line column when a file has no Line/Tie markers. NOT ``FID``.
LINE_TOKENS: frozenset = frozenset({
    "LINE", "LINENO", "LINENUM", "LINENUMBER", "LINEID", "LINENAME",
})

#: A Geosoft line marker: ``Line 1010``, ``Tie 9010``, ``Trend 1``.
_MARKER = re.compile(r"^(line|tie|trend)\s+(\S+)", re.IGNORECASE)

#: Geosoft dummies. ``*`` in text, -1.0E32 for real channels.
_DUMMY_TEXT = frozenset({"*", "", "nan", "null", "-", "n/a", "na"})
_DUMMY_CUTOFF = -1.0e31

#: A single line longer than this is split into consecutive segments, so no
#: one database row or in-memory block grows without bound (an unlined
#: station grid can be millions of rows).
MAX_POINTS_PER_SEGMENT = 100_000

#: Row-issue codes.
CODE_FIELD_COUNT = "field_count_mismatch"
CODE_COORD_NOT_NUMERIC = "coordinate_not_numeric"


def _sha256_file(path: str) -> str:
    """Stream-hash the file at *path*, returning the hex digest."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _token_key(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", name or "").upper()


def _cast_float(value: Any) -> float | None:
    """Return a finite float or None (for dummies and non-numbers); never raises."""
    if value is None:
        return None
    s = str(value).strip()
    if s.lower() in _DUMMY_TEXT:
        return None
    try:
        f = float(s)
    except (ValueError, TypeError):
        return None
    if not math.isfinite(f) or f <= _DUMMY_CUTOFF:
        return None
    return f


def _marker(stripped: str) -> tuple | None:
    """``(kind, id)`` when *stripped* is a Line/Tie/Trend marker row.

    Exactly two tokens: an un-commented header row such as ``LINE X Y MAG``
    starts with the same word and must not be taken for line "X".
    """
    m = _MARKER.match(stripped)
    if m is None or len(stripped.split()) != 2:
        return None
    return m.group(1).lower(), m.group(2)


#: A plain decimal / scientific number, or Geosoft's ``*`` dummy.
_NUMBER_TOKEN = re.compile(r"^(\*|[+-]?(\d+\.?\d*|\.\d+)([eEdD][+-]?\d+)?)$")


def _is_number(token: str) -> bool:
    """Whether *token* reads as a data value (used to tell a header row from data)."""
    return bool(_NUMBER_TOKEN.match(token.strip()))


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class XyzHeader:
    """What the header told us: the columns and which of them are axes."""

    columns: list
    easting_column: str
    northing_column: str
    line_column: str | None
    #: 'projected' when X/Y-family columns were found, 'geographic' when only
    #: LON/LAT were. A hint for the CRS decision, never a CRS itself.
    axis_family: str
    #: 'comment' (a ``/`` line) or 'first_row' (an un-commented header row).
    header_source: str
    #: 0-based index of the first line after the header.
    data_start: int
    #: Every other axis-looking column, kept as a channel. Named so the
    #: caller can say which pair was used when a file carries several.
    other_axis_columns: list = field(default_factory=list)

    @property
    def channel_columns(self) -> list:
        skip = {self.easting_column, self.northing_column}
        if self.line_column:
            skip.add(self.line_column)
        return [c for c in self.columns if c not in skip]


@dataclass
class XyzRowIssue:
    """One skipped data row and why."""

    row: int         # 1-based line number in the file
    code: str
    reason: str


@dataclass
class XyzLine:
    """One line (or one segment of a long line) of an XYZ export."""

    line_id: str | None     # None for an unlined block of points
    line_type: str          # 'line' | 'tie' | 'trend' | 'points'
    segment: int            # 1-based ordinal among blocks sharing line_id
    source_rows: list       # 1-based file line numbers, one per point
    x: list                 # float
    y: list                 # float
    channels: dict          # channel name -> list[float | None]

    @property
    def point_count(self) -> int:
        return len(self.x)


@dataclass
class XyzChannel:
    """Statistics for a single geophysics channel (all non-coordinate columns)."""

    name: str
    values: list           # float values; may contain None for null/masked points
    min_value: float
    max_value: float
    unit: str              # None — an XYZ file carries no unit metadata


@dataclass
class XyzParseResult:
    """Container for a completed XYZ file parse run (all lines in memory)."""

    source_file: str
    channel_count: int
    channels: list          # list of XyzChannel
    point_count: int
    easting_column: str
    northing_column: str
    line_column: str
    x_values: list
    y_values: list
    line_values: list       # str line ids per point, or None when unlined
    lines: list = field(default_factory=list)        # list of XyzLine
    skipped_rows: list = field(default_factory=list)  # list of XyzRowIssue
    parse_errors: list = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

def _axis_of(name: str) -> str | None:
    key = _token_key(name)
    if key in X_TOKENS:
        return "x"
    if key in Y_TOKENS:
        return "y"
    if key in LON_TOKENS:
        return "lon"
    if key in LAT_TOKENS:
        return "lat"
    m = _AXIS_WITH_SUFFIX.match(name.strip())
    if m:
        head = m.group(1).upper()
        return {"X": "x", "Y": "y", "LAT": "lat", "LON": "lon", "LONG": "lon"}[head]
    return None


def detect_axes(columns: list) -> tuple:
    """``(easting, northing, line, family, other_axes)`` for a header row.

    Raises ValueError when no complete axis PAIR exists. The old parser fell
    back to "column 0 is X, column 1 is Y" whenever a name did not match,
    which files an unrecognised layout at whatever its first two numbers are
    — refused here instead, naming what was found.
    """
    by_axis: dict = {"x": [], "y": [], "lon": [], "lat": []}
    for col in columns:
        axis = _axis_of(col)
        if axis:
            by_axis[axis].append(col)

    if by_axis["x"] and by_axis["y"]:
        easting, northing, family = by_axis["x"][0], by_axis["y"][0], "projected"
    elif by_axis["lon"] and by_axis["lat"]:
        easting, northing, family = by_axis["lon"][0], by_axis["lat"][0], "geographic"
    else:
        found = [c for cols in by_axis.values() for c in cols]
        raise ValueError(
            "xyz_parser: the header names no complete coordinate pair "
            f"(found {found or 'none'} among {list(columns)}); expected X and Y, "
            "EASTING and NORTHING, or LONG and LAT."
        )

    line_col = next((c for c in columns if _token_key(c) in LINE_TOKENS), None)
    others = [
        c for cols in by_axis.values() for c in cols if c not in (easting, northing)
    ]
    return easting, northing, line_col, family, others


def scan_xyz_header(path: str) -> XyzHeader:
    """Find the column header and where the data begins.

    Raises ValueError when no header naming a coordinate pair can be found.
    """
    candidate: list | None = None
    source = "comment"
    data_start = 0

    with open(path, encoding="utf-8", errors="replace") as fh:
        for i, raw_line in enumerate(fh):
            stripped = raw_line.strip()
            if not stripped:
                continue
            if stripped.startswith("/"):
                tokens = stripped.lstrip("/").strip().split()
                if tokens and any(_axis_of(t) for t in tokens):
                    candidate = tokens
                continue
            if _marker(stripped) is not None:
                # A marker before any data: the header (if any) is above it.
                data_start = i
                break
            tokens = stripped.split()
            if candidate is None and not all(_is_number(t) for t in tokens):
                # An un-commented header row of words.
                if any(_axis_of(t) for t in tokens):
                    candidate = tokens
                    source = "first_row"
                    data_start = i + 1
                    break
            data_start = i
            break

    if not candidate:
        raise ValueError(
            f"xyz_parser: could not detect a column header in "
            f"'{os.path.basename(path)}'. Expected a comment line starting with "
            "'/' (or a first row of words) that names X/Y, EASTING/NORTHING or "
            "LONG/LAT columns."
        )

    easting, northing, line_col, family, others = detect_axes(candidate)
    return XyzHeader(
        columns=candidate,
        easting_column=easting,
        northing_column=northing,
        line_column=line_col,
        axis_family=family,
        header_source=source,
        data_start=data_start,
        other_axis_columns=others,
    )


# ---------------------------------------------------------------------------
# Streaming reader
# ---------------------------------------------------------------------------

def _line_value_text(raw: str) -> str:
    """A LINE column value as a stable id: ``1010.0`` and ``1010`` agree."""
    f = _cast_float(raw)
    if f is not None and float(f).is_integer():
        return str(int(f))
    return str(raw).strip()


def iter_xyz_lines(
    path: str,
    header: XyzHeader | None = None,
    *,
    issues: list | None = None,
    max_points: int = MAX_POINTS_PER_SEGMENT,
) -> Iterator[XyzLine]:
    """Yield the file's lines one block at a time.

    ``issues`` (if given) collects an :class:`XyzRowIssue` per skipped row.
    """
    header = header or scan_xyz_header(path)
    cols = header.columns
    n = len(cols)
    ix = cols.index(header.easting_column)
    iy = cols.index(header.northing_column)
    iline = cols.index(header.line_column) if header.line_column else None
    channel_idx = [(i, c) for i, c in enumerate(cols) if c in header.channel_columns]

    segments: dict = {}

    def _new(line_id: str | None, line_type: str) -> XyzLine:
        segments[line_id] = segments.get(line_id, 0) + 1
        return XyzLine(
            line_id=line_id, line_type=line_type, segment=segments[line_id],
            source_rows=[], x=[], y=[], channels={c: [] for _, c in channel_idx},
        )

    current: XyzLine | None = None
    saw_marker = False

    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, raw_line in enumerate(fh, start=1):
            if lineno - 1 < header.data_start:
                continue
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("/"):
                continue

            marker = _marker(stripped)
            if marker is not None:
                if current is not None and current.x:
                    yield current
                saw_marker = True
                current = _new(marker[1], marker[0])
                continue

            tokens = stripped.split()
            if len(tokens) != n:
                if issues is not None:
                    issues.append(XyzRowIssue(
                        lineno, CODE_FIELD_COUNT,
                        f"row {lineno}: {len(tokens)} field(s) where the header "
                        f"declares {n}; skipped rather than shifting every "
                        f"channel after the gap",
                    ))
                continue
            x = _cast_float(tokens[ix])
            y = _cast_float(tokens[iy])
            if x is None or y is None:
                if issues is not None:
                    issues.append(XyzRowIssue(
                        lineno, CODE_COORD_NOT_NUMERIC,
                        f"row {lineno}: {header.easting_column}/"
                        f"{header.northing_column} is not a number "
                        f"({tokens[ix]!r}, {tokens[iy]!r})",
                    ))
                continue

            if not saw_marker:
                if iline is not None:
                    line_id = _line_value_text(tokens[iline])
                    if current is None or current.line_id != line_id:
                        if current is not None and current.x:
                            yield current
                        current = _new(line_id, "line")
                elif current is None:
                    current = _new(None, "points")
            elif current is None:
                current = _new(None, "points")

            if len(current.x) >= max_points:
                yield current
                current = _new(current.line_id, current.line_type)

            current.source_rows.append(lineno)
            current.x.append(x)
            current.y.append(y)
            for i, c in channel_idx:
                current.channels[c].append(_cast_float(tokens[i]))

    if current is not None and current.x:
        yield current


# ---------------------------------------------------------------------------
# All-at-once entry point
# ---------------------------------------------------------------------------

def parse_xyz_file(path: str) -> XyzParseResult:
    """Parse a Geosoft-style XYZ export file entirely into memory.

    For large files prefer :func:`scan_xyz_header` + :func:`iter_xyz_lines`.

    Raises
    ------
    ValueError
        If no recognisable column header can be found in the file.
    FileNotFoundError
        If the file does not exist at the given path.
    """
    if not os.path.isfile(path):
        raise FileNotFoundError(f"xyz_parser: file not found at '{path}'")

    filename = os.path.basename(path)
    sha256_hex = _sha256_file(path)
    header = scan_xyz_header(path)
    issues: list = []
    lines = list(iter_xyz_lines(path, header, issues=issues))

    x_values: list = []
    y_values: list = []
    line_values: list = []
    merged: dict = {c: [] for c in header.channel_columns}
    for block in lines:
        x_values.extend(block.x)
        y_values.extend(block.y)
        line_values.extend([block.line_id] * block.point_count)
        for c in merged:
            merged[c].extend(block.channels.get(c, []))

    parse_errors: list = []
    channels: list = []
    for name, values in merged.items():
        clean = [v for v in values if v is not None]
        if not clean:
            parse_errors.append(f"Channel '{name}' has no numeric values.")
            continue
        channels.append(XyzChannel(
            name=name, values=values, min_value=min(clean), max_value=max(clean),
            unit=None,
        ))
    if not lines:
        parse_errors.append("No data rows found in file.")

    col_map: dict[str, str] = {
        "easting": header.easting_column,
        "northing": header.northing_column,
    }
    if header.line_column is not None:
        col_map["line"] = header.line_column

    result = XyzParseResult(
        source_file=filename,
        channel_count=len(channels),
        channels=channels,
        point_count=len(x_values),
        easting_column=header.easting_column,
        northing_column=header.northing_column,
        line_column=header.line_column,
        x_values=x_values,
        y_values=y_values,
        line_values=line_values if any(v is not None for v in line_values) else None,
        lines=lines,
        skipped_rows=issues,
        parse_errors=parse_errors,
        provenance={
            "source_file_sha256": sha256_hex,
            "parser_name": PARSER_NAME,
            "parser_version": PARSER_VERSION,
            "source_col_map": col_map,
        },
    )
    logger.info(
        "XYZ parse complete: file='%s' points=%d lines=%d channels=%d skipped=%d",
        filename, result.point_count, len(lines), len(channels), len(issues),
    )
    return result


__all__ = [
    "MAX_POINTS_PER_SEGMENT",
    "PARSER_NAME",
    "PARSER_VERSION",
    "XyzChannel",
    "XyzHeader",
    "XyzLine",
    "XyzParseResult",
    "XyzRowIssue",
    "detect_axes",
    "iter_xyz_lines",
    "parse_xyz_file",
    "scan_xyz_header",
]
