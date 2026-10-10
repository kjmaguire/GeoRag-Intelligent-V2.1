"""Excel Parser — handles both .xlsx/.xlsm (via Polars/openpyxl) and .xls (via xlrd).

Accepts an Excel file path (.xlsx, .xlsm, or .xls), a sheet name, and a sheet
type.  The file is loaded via the appropriate backend:

  .xlsx / .xlsm — polars.read_excel (openpyxl / fastexcel backend)
  .xls          — xlrd 1.x (the only free library that reads the legacy BIFF OLE
                  format; xlrd >= 2.0 explicitly dropped .xls support)

After loading, both paths export to an in-memory CSV buffer and delegate to the
same CSV parser for the given sheet_type, reusing all alias-matching, validation,
and quality-metric logic without duplication.

Legacy-format behaviours:
  - Merged cells are detected (xlrd exposes sheet.merged_cells).  Sprint 4 does
    NOT auto-unmerge; a structured warning is emitted instead.
  - Multi-row headers: if row 0 is mostly empty but row 1 has full coverage, a
    warning is emitted.
  - Title / preamble rows (ING-13, 2026-09-29): when row 0 classifies as no
    drill layout but a row within the first 15 does, THAT row is the header
    and the rows above it are skipped, with a ``header_row_detected`` warning.
    A branded export ("Acme Gold Corp - Drill Collar Table" in A1, headers in
    row 3) used to classify ``unknown`` and reach only the text fallback.
  - .xlsx is read through openpyxl explicitly. polars' default Excel engine is
    calamine, which needs ``fastexcel`` - a package no lockfile in this repo
    installs - so the default read raised ModuleNotFoundError for every
    .xlsx sheet.
  - Formula cells: xlrd returns cached values only (live formulas unavailable).
    This is logged at debug level and is not an error.
  - xls_legacy_format_detected info warning is emitted for all .xls files.

Parse quality metrics are emitted as structured log output so the caller can
record them in Dagster materialisation metadata.

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import hashlib
import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import polars as pl

logger = logging.getLogger(__name__)

PARSER_VERSION = "1.2.0"  # 2026-05-23 — added enumerate_sheets for multi-sheet auto-dispatch

# Supported sheet types map directly to the existing CSV parsers.
SheetType = Literal[
    "collar", "survey", "lithology", "sample", "structure",
    "alteration", "mineralization",
]

#: Codes of parser warnings that describe the data (not the transport) and
#: are forwarded from the CSV parser to the workbook result.
_FORWARDED_PARSER_WARNINGS = frozenset({
    "optional_values_blanked",
    "sample_type_column_missing",
    "assay_unit_assumed",
    "assay_unit_converted",
    "assay_columns_merged",
    "structure_strike_not_converted",
    "structure_type_unmapped",
    "structure_interval_collapsed",
    "structure_no_orientation",
    "structure_strike_converted",
    "lithology_values_too_long",
    "mineralization_abundance_not_numeric",
    "mineralization_abundance_out_of_range",
    "mineralization_text_unsplit",
    "mineralization_text_in_notes",
    "mineralization_intensity_in_notes",
    "mineralization_value_unassigned",
    "alteration_style_in_notes",
    "alteration_value_unassigned",
    # Collar sheets (audit findings 6 and 15): a repeated hole, and a drill
    # date left empty because it was unreadable or day/month-ambiguous.
    "duplicate_hole_id",
    "date_unparseable",
    "date_ambiguous",
    "date_convention_inferred",
})

# Extension sets for routing to the correct read backend.
_XLSX_EXTS = frozenset({".xlsx", ".xlsm"})
_XLS_EXTS = frozenset({".xls"})


# ---------------------------------------------------------------------------
# Sheet enumeration (added 2026-05-23 per XLSX audit gap #1)
# ---------------------------------------------------------------------------

@dataclass
class SheetMeta:
    """Per-sheet metadata returned by :func:`enumerate_sheets`.

    Used by ``silver_xlsx`` to fan out a multi-sheet workbook to the
    matching CSV parser per sheet, instead of silently dropping
    everything past Sheet 1.
    """

    name: str                  # sheet name as it appears in the workbook
    headers: list[str]         # first-row column names (may be empty)
    row_count: int             # data rows below the header (0 = empty sheet)
    sheet_type: str            # collar | survey | lithology | sample | structure | alteration | mineralization | unknown
    classify_confidence: float # 0.0-1.0 from the header classifier
    hidden: bool               # True if the sheet is hidden / very_hidden
    header_row: int = 0        # 0-based row the headers were found on (ING-13)


def enumerate_sheets(path: str, *, column_map=None) -> list[SheetMeta]:
    """Return one :class:`SheetMeta` per sheet in the workbook.

    Hidden sheets are reported with ``hidden=True`` so the caller can
    choose to skip them (the typical correct behaviour — hidden sheets
    in an industry template are usually scratchpads / lookup tables
    that aren't data).

    Both modern (.xlsx/.xlsm) and legacy (.xls) workbooks are handled
    via their respective backends. Sheet classification reuses the
    project's :func:`_sheet_classifier.classify_sheet_type` so the same
    header → type rules apply everywhere.

    ``column_map`` is a mapping the user confirmed, passed straight to the
    classifier. A sheet whose headers we do not recognise classifies as
    ``unknown`` and is never dispatched to a parser, so without this a
    mapping written FOR that sheet could never take effect on it.
    """
    # Deferred import — keeps the parser module lightweight at load.
    from georag_geoparsers._sheet_classifier import (
        HEADER_SCAN_ROWS,
        classify_sheet_type,
        detect_header_row,
    )

    ext = Path(path).suffix.lower()
    out: list[SheetMeta] = []

    if ext in _XLSX_EXTS:
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover — openpyxl is in the image
            raise RuntimeError(
                "openpyxl unavailable — required for XLSX sheet enumeration"
            ) from exc

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                # openpyxl sheet_state: 'visible' | 'hidden' | 'veryHidden'
                hidden = (getattr(ws, "sheet_state", "visible") or "visible") != "visible"
                # The header is row 0 unless a title/preamble sits above it
                # (detect_header_row); the data rows are those below it.
                head: list[tuple] = []
                nonempty: list[bool] = []
                total_nonempty = 0
                for row in ws.iter_rows(values_only=True):
                    filled = any(c is not None and str(c).strip() for c in row)
                    if len(head) < HEADER_SCAN_ROWS:
                        head.append(tuple(row))
                        nonempty.append(filled)
                    total_nonempty += int(filled)
                header_row = detect_header_row(head, column_map=column_map)
                headers = (
                    [(str(c) if c is not None else "") for c in head[header_row]]
                    if head else []
                )
                row_count = (
                    total_nonempty - sum(nonempty[: header_row + 1]) if head else 0
                )
                sheet_type, confidence = classify_sheet_type(
                    headers, column_map=column_map,
                )
                out.append(SheetMeta(
                    name=sheet_name,
                    headers=headers,
                    row_count=row_count,
                    sheet_type=sheet_type,
                    classify_confidence=confidence,
                    hidden=hidden,
                    header_row=header_row,
                ))
        finally:
            wb.close()
        return out

    if ext in _XLS_EXTS:
        try:
            import xlrd
        except ImportError as exc:
            raise RuntimeError(
                "xlrd unavailable — required for legacy .xls enumeration"
            ) from exc

        wb = xlrd.open_workbook(path, formatting_info=True)
        for sheet_name in wb.sheet_names():
            sheet = wb.sheet_by_name(sheet_name)
            # xlrd visibility: 0 = visible, 1 = hidden, 2 = very_hidden
            hidden = getattr(sheet, "visibility", 0) != 0
            headers: list[str] = []
            row_count = 0
            header_row = 0
            if sheet.nrows > 0:
                header_row = detect_header_row(
                    [sheet.row_values(i) for i in range(min(sheet.nrows, HEADER_SCAN_ROWS))],
                    column_map=column_map,
                )
                headers = [
                    (str(v) if v is not None else "")
                    for v in sheet.row_values(header_row)
                ]
                row_count = max(0, sheet.nrows - 1 - header_row)
            sheet_type, confidence = classify_sheet_type(
                headers, column_map=column_map,
            )
            out.append(SheetMeta(
                name=sheet_name,
                headers=headers,
                row_count=row_count,
                sheet_type=sheet_type,
                classify_confidence=confidence,
                hidden=hidden,
                header_row=header_row,
            ))
        return out

    raise ValueError(
        f"xlsx_parser.enumerate_sheets: unsupported extension {ext!r} for {path!r}"
    )


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class ExcelParseResult:
    """Container for a completed Excel sheet parse run.

    Mirrors the shape of the underlying CSV parse result so that the Silver
    asset can handle all four sheet types uniformly.
    """

    source_file: str
    sheet_name: str
    sheet_type: SheetType
    format: Literal["xlsx", "xls", "xlsm"]  # which backend was used
    total_rows: int
    valid_rows: int
    skipped_rows: int
    parse_quality_pct: float
    unmapped_columns: list
    records: list            # validated row dicts — same structure as CSV parse result
    skipped_details: list    # each entry has 'reason' key (and 'raw', 'row' keys)
    column_map: dict         # canonical → original column name from the CSV parser
    assay_columns: list      # only populated for sample sheets
    warnings: list[dict] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)


# Backward-compatible alias — existing code that imports XlsxParseResult still works.
XlsxParseResult = ExcelParseResult


# ---------------------------------------------------------------------------
# SHA-256 provenance helper
# ---------------------------------------------------------------------------

def read_sheet_rows(path: str, sheet_name: str = "") -> list[dict[str, Any]]:
    """One sheet's rows as dicts keyed by its header row.

    The four typed drill parsers reject a sheet whose columns none of their
    aliases match, and until now the only thing left to do with it was
    render it to prose. A geochemical certificate, an IP station list or a
    radiometric-age table is not prose: it is a table whose columns simply
    are not collar/survey/lithology/sample. This is the shape that lets the
    workflow keep those values AS values.

    Deliberately the same two loaders :func:`parse_xlsx_sheet` uses, so a
    sheet that parses there reads here -- including the xlrd legacy path,
    which is the only reader for .xls and lives in this package because
    check_pyproject_covers_imports will not let the FastAPI side import it.
    """
    ext = Path(path).suffix.lower()
    if ext == ".xls":
        df, _resolved, _warnings = _xls_to_polars_df(path, sheet_name, header_row=0)
    elif ext in _XLSX_EXTS:
        df = (
            pl.read_excel(path, sheet_name=sheet_name, engine="openpyxl")
            if sheet_name else pl.read_excel(path, engine="openpyxl")
        )
    else:
        raise ValueError(
            f"read_sheet_rows: unsupported extension '{ext}' for '{path}'. "
            f"Expected one of: .xlsx, .xlsm, .xls"
        )
    return df.to_dicts()


def read_xls_sheets(path: str) -> list[tuple[str, str]]:
    """Every non-empty sheet of a legacy .xls as (name, tab-separated text).

    Exists so the FastAPI side does not have to import xlrd itself. It cannot:
    check_pyproject_covers_imports gates every import under src/fastapi/app
    against src/fastapi/pyproject.toml, and xlrd is declared HERE, in the
    package that owns spreadsheet reading. Adding it to both would have meant
    two readers for one format and a lockfile regeneration for a library that
    is already installed.

    Why it is needed at all: app/services/ingest/xlsx_ingester.py -- the text
    fallback that catches every sheet the drill classifier did not claim --
    called openpyxl unconditionally, and openpyxl reads the OOXML zip, not the
    OLE2 binary that .xls actually is. A real customer .xls came back as
    "produced no searchable text" while this module had been reading .xls
    perfectly well on the typed-drill route the whole time.

    Shape matches what the caller already builds from openpyxl worksheets:
    first row is the header, rows are tab-separated, blank rows dropped, and a
    sheet with nothing in it is omitted rather than yielding an empty string.
    """
    import xlrd  # noqa: PLC0415

    book = xlrd.open_workbook(path, on_demand=True)
    try:
        out: list[tuple[str, str]] = []
        for name in book.sheet_names():
            sheet = book.sheet_by_name(name)
            lines = []
            for r in range(sheet.nrows):
                cells = [
                    "" if v is None else str(v).strip()
                    for v in sheet.row_values(r)
                ]
                if any(cells):
                    lines.append("\t".join(cells))
            if lines:
                out.append((name, "\n".join(lines)))
        return out
    finally:
        try:
            book.release_resources()
        except Exception:
            logger.debug(
                "xlsx_parser: xlrd release_resources failed for %s",
                path, exc_info=True,
            )


def _sha256_file(path: str) -> str:
    """Stream-hash the file at *path*, returning the hex digest."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# XLS reader — xlrd 1.x backend
# ---------------------------------------------------------------------------

def _detect_merged_cells(sheet) -> list[dict]:
    """Return a list of merged-cell range dicts from an xlrd Sheet object.

    xlrd exposes sheet.merged_cells as a list of (r_low, r_high, c_low, c_high)
    tuples where the ranges are [r_low, r_high) × [c_low, c_high).
    """
    ranges = []
    for r_low, r_high, c_low, c_high in (sheet.merged_cells or []):
        ranges.append({
            "row_start": r_low, "row_end": r_high,
            "col_start": c_low, "col_end": c_high,
        })
    return ranges


def _detect_multi_row_header(sheet, ncols: int) -> bool:
    """Return True if row 0 is mostly empty but row 1 has full coverage.

    'Mostly empty' means > 50 % of cells in row 0 are blank/None.
    'Full coverage' means > 80 % of cells in row 1 are non-blank.
    """
    if sheet.nrows < 2 or ncols == 0:
        return False

    row0_values = sheet.row_values(0)
    row1_values = sheet.row_values(1)

    row0_empty = sum(1 for v in row0_values if v == "" or v is None)
    row1_filled = sum(1 for v in row1_values if v != "" and v is not None)

    row0_empty_pct = row0_empty / ncols
    row1_fill_pct = row1_filled / ncols

    return row0_empty_pct > 0.5 and row1_fill_pct > 0.8


def _xls_to_polars_df(
    path: str, sheet_name: str, *, header_row: int | None = None, column_map=None,
) -> tuple[pl.DataFrame, str, list[dict]]:
    """Load an .xls workbook via xlrd and return a Polars DataFrame plus metadata.

    ``header_row`` is the 0-based row holding the column names; None detects
    it (``detect_header_row``: row 0 unless a title row sits above a
    recognisable drill header).

    Returns (df, resolved_sheet_name, xls_warnings).
    All cell values are converted to strings for downstream CSV parser compatibility
    (matching the behaviour of polars.write_csv / read_csv round-trip used for xlsx).
    """
    import xlrd

    xls_warnings: list[dict] = []

    # formatting_info=True is required for sheet.merged_cells to be populated.
    wb = xlrd.open_workbook(path, formatting_info=True)

    # Resolve sheet name
    if sheet_name:
        try:
            sheet = wb.sheet_by_name(sheet_name)
            resolved_name = sheet_name
        except xlrd.XLRDError:
            # Fall back to first sheet
            logger.warning(
                "xlsx_parser: sheet '%s' not found in '%s' — using first sheet",
                sheet_name, path,
            )
            sheet = wb.sheet_by_index(0)
            resolved_name = sheet.name
    else:
        sheet = wb.sheet_by_index(0)
        resolved_name = sheet.name

    ncols = sheet.ncols
    nrows = sheet.nrows

    # Detect merged cells — warn but do NOT auto-unmerge (Sprint 4 scope)
    merged = _detect_merged_cells(sheet)
    if merged:
        xls_warnings.append({
            "code": "merged_cells_detected",
            "message": (
                f"Sheet '{resolved_name}' has {len(merged)} merged cell range(s). "
                "Auto-unmerge is deferred to a future sprint."
            ),
            "context": {"count": len(merged), "ranges": merged},
        })
        logger.warning(
            "xlsx_parser: '%s' sheet '%s' has %d merged cell range(s)",
            path, resolved_name, len(merged),
        )

    # Detect multi-row header
    if _detect_multi_row_header(sheet, ncols):
        xls_warnings.append({
            "code": "multi_row_header_suspected",
            "message": (
                "Row 0 appears mostly empty; row 1 may be the actual header. "
                "Row 0 is used as the header — verify the source file."
            ),
            "context": {"header_candidate_rows": [0, 1]},
        })
        logger.warning(
            "xlsx_parser: '%s' sheet '%s' may have a multi-row header",
            path, resolved_name,
        )

    if nrows == 0 or ncols == 0:
        return pl.DataFrame(), resolved_name, xls_warnings

    if header_row is None:
        from georag_geoparsers._sheet_classifier import (
            HEADER_SCAN_ROWS,
            detect_header_row,
        )

        header_row = detect_header_row(
            [sheet.row_values(i) for i in range(min(nrows, HEADER_SCAN_ROWS))],
            column_map=column_map,
        )
    if header_row:
        xls_warnings.append(_header_row_warning(resolved_name, header_row))

    # Extract header row
    header = [
        str(v) if v != "" else f"col_{i}"
        for i, v in enumerate(sheet.row_values(header_row))
    ]

    # Extract data rows — xlrd returns cached cell values; formulas show computed result
    logger.debug(
        "xlsx_parser: xlrd reads cached values for formula cells in '%s'", path
    )

    rows_data: list[list[str]] = []
    for ridx in range(header_row + 1, nrows):
        row_vals = sheet.row_values(ridx)
        rows_data.append([
            "" if (v is None or (isinstance(v, float) and str(v) == "nan")) else str(v)
            for v in row_vals
        ])

    # Build Polars DataFrame from string columns
    if not rows_data:
        df = pl.DataFrame({col: pl.Series(col, [], dtype=pl.Utf8) for col in header})
    else:
        col_data = {header[i]: [row[i] if i < len(row) else "" for row in rows_data]
                    for i in range(ncols)}
        df = pl.DataFrame(col_data, schema={k: pl.Utf8 for k in col_data})

    return df, resolved_name, xls_warnings


def _header_row_warning(sheet_name: str, header_row: int) -> dict:
    """Say that rows above the header were skipped (ING-13)."""
    return {
        "code": "header_row_detected",
        "message": (
            f"sheet '{sheet_name}': headers found on row {header_row + 1}; "
            f"the {header_row} row(s) above were read as a title and skipped"
        ),
        "detail": (
            f"The first row of sheet '{sheet_name}' is not a column header, so "
            f"the header was taken from row {header_row + 1}, the first row "
            f"whose columns match a drill-table layout. The {header_row} "
            f"row(s) above it (a title or notes) were not read as data."
        ),
        "context": {"sheet": sheet_name, "header_row": header_row + 1},
    }


def _xlsx_sheet_head(path: str, sheet_name: str) -> list[tuple]:
    """The first rows of an .xlsx sheet, for header detection."""
    import openpyxl

    from georag_geoparsers._sheet_classifier import HEADER_SCAN_ROWS

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet_name] if sheet_name else wb[wb.sheetnames[0]]
        head: list[tuple] = []
        for row in ws.iter_rows(values_only=True):
            head.append(tuple(row))
            if len(head) >= HEADER_SCAN_ROWS:
                break
        return head
    finally:
        wb.close()


def _promote_header(raw: pl.DataFrame, header_row: int) -> pl.DataFrame:
    """A header-less frame with row *header_row* made the column names."""
    rows = raw.rows()
    names: list[str] = []
    for i, value in enumerate(rows[header_row] if header_row < len(rows) else []):
        name = str(value).strip() if value is not None else ""
        name = name or f"col_{i}"
        while name in names:
            name = f"{name}_{i}"
        names.append(name)
    data = [
        ["" if v is None else str(v) for v in row]
        for row in rows[header_row + 1:]
        if any(v is not None and str(v).strip() for v in row)
    ]
    return pl.DataFrame(
        {name: [row[i] if i < len(row) else "" for row in data] for i, name in enumerate(names)},
        schema={name: pl.Utf8 for name in names},
    )


def _classifier_map(sheet_type: str, vendor_aliases: dict | None) -> dict | None:
    """The user's confirmed mapping, in the classifier's shape."""
    if not vendor_aliases:
        return None
    return {sheet_type: {
        field_name: aliases[0]
        for field_name, aliases in vendor_aliases.items()
        if aliases
    }}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def parse_xlsx_sheet(
    path: str,
    sheet_name: str,
    sheet_type: SheetType,
    *,
    vendor_aliases: dict[str, list[str]] | None = None,
    companion: bool = False,
) -> ExcelParseResult:
    """Parse a single sheet of an Excel file (.xlsx, .xlsm, or .xls) as the given sheet_type.

    Routing:
      .xlsx / .xlsm → polars.read_excel (openpyxl / fastexcel backend)
      .xls          → xlrd 1.x (legacy BIFF OLE; xlrd >= 2 does not support .xls)

    After loading, both paths serialise to an in-memory CSV buffer and delegate
    to the matching CSV parser.  All alias resolution, row-level validation, and
    quality metrics come from the CSV parser — no duplication of that logic here.

    Parameters
    ----------
    path:
        Absolute path to the Excel file (.xlsx, .xlsm, or .xls).
    sheet_name:
        Name of the sheet to load.  Pass an empty string to use the first sheet.
    sheet_type:
        One of "collar", "survey", "lithology", "sample", "structure",
        "alteration", "mineralization".  Controls which CSV parser is invoked.
    vendor_aliases:
        Extra column spellings, passed straight through to that CSV parser.
        A workbook sheet resolves its columns in the CSV parser, so a user's
        confirmed mapping has to travel the same way or it would apply to a
        loose .csv and be ignored for the identical table inside an .xlsx.
    companion:
        Only for "alteration" / "mineralization": read those columns of a sheet
        that is primarily a lithology log (rows without any are not-applicable,
        not rejected). See ``_geology_interval``.

    Returns
    -------
    ExcelParseResult (aliased as XlsxParseResult for backward compatibility).
        Contains validated records plus quality metrics.
    """
    path_str = str(path)
    ext = Path(path_str).suffix.lower()

    # Provenance — hash before opening
    sha256_hex = _sha256_file(path_str)
    provenance: dict[str, Any] = {
        "source_file_sha256": sha256_hex,
        "parser_name": "xlsx_parser",
        "parser_version": PARSER_VERSION,
        # source_col_map populated below once the CSV parser resolves column aliases
        "source_col_map": None,
    }

    extra_warnings: list[dict] = []

    if ext in _XLS_EXTS:
        # --- Legacy .xls path (xlrd 1.x) ---
        extra_warnings.append({
            "code": "xls_legacy_format_detected",
            "message": (
                "File is in legacy .xls binary OLE format.  "
                "Parsed via xlrd 1.x (cached cell values only, no live formulas)."
            ),
            "context": {"path": path_str},
        })
        logger.info("xlsx_parser: '%s' is .xls — using xlrd legacy path", path_str)

        try:
            df, resolved_sheet_name, xls_warnings = _xls_to_polars_df(
                path_str, sheet_name,
                column_map=_classifier_map(sheet_type, vendor_aliases),
            )
        except Exception as exc:
            logger.error(
                "xlsx_parser: failed to load .xls from '%s' (sheet=%r): %s",
                path_str, sheet_name, exc,
            )
            raise

        extra_warnings.extend(xls_warnings)
        file_format: Literal["xlsx", "xls", "xlsm"] = "xls"

    elif ext in _XLSX_EXTS:
        # --- Modern .xlsx / .xlsm path (Polars / openpyxl) ---
        header_row = 0
        try:
            from georag_geoparsers._sheet_classifier import detect_header_row

            header_row = detect_header_row(
                _xlsx_sheet_head(path_str, sheet_name),
                column_map=_classifier_map(sheet_type, vendor_aliases),
            )
        except Exception:
            # Detection is an improvement, never a new way to fail: an
            # unreadable head leaves the header on row 0, as before.
            logger.debug(
                "xlsx_parser: header-row detection failed for %s", path_str,
                exc_info=True,
            )
        try:
            if header_row:
                raw_df = pl.read_excel(
                    path_str, sheet_name=sheet_name or None, engine="openpyxl",
                    has_header=False, drop_empty_rows=False,
                    infer_schema_length=0,
                )
                # Re-detected on the frame itself: its row numbering is what
                # the header is promoted from.
                from georag_geoparsers._sheet_classifier import detect_header_row

                header_row = detect_header_row(
                    raw_df.rows()[:15],
                    column_map=_classifier_map(sheet_type, vendor_aliases),
                )
                df = _promote_header(raw_df, header_row)
                resolved_sheet_name = sheet_name or "Sheet1"
                if header_row:
                    extra_warnings.append(
                        _header_row_warning(resolved_sheet_name, header_row),
                    )
            elif sheet_name:
                df = pl.read_excel(path_str, sheet_name=sheet_name, engine="openpyxl")
                resolved_sheet_name = sheet_name
            else:
                df = pl.read_excel(path_str, engine="openpyxl")
                resolved_sheet_name = "Sheet1"
                try:
                    import openpyxl
                    wb = openpyxl.load_workbook(path_str, read_only=True, data_only=True)
                    resolved_sheet_name = wb.sheetnames[0]
                    wb.close()
                except Exception:
                    pass  # openpyxl unavailable or unreadable — placeholder is fine
        except Exception as exc:
            logger.error(
                "xlsx_parser: failed to load Excel from '%s' (sheet=%r): %s",
                path_str, sheet_name, exc,
            )
            raise

        file_format = "xlsm" if ext == ".xlsm" else "xlsx"

    else:
        raise ValueError(
            f"xlsx_parser: unsupported file extension '{ext}' for '{path_str}'. "
            f"Expected one of: .xlsx, .xlsm, .xls"
        )

    filename = Path(path_str).name
    total_rows_raw = len(df)
    logger.info(
        "Excel loaded: file='%s' sheet='%s' rows=%d columns=%d format=%s",
        filename, resolved_sheet_name, total_rows_raw, len(df.columns), file_format,
    )

    # --- Export to in-memory CSV buffer ---
    # Polars writes all dtypes as strings in CSV, which is exactly what the CSV
    # parsers expect (they use infer_schema=False and cast themselves).
    csv_buffer = io.StringIO()
    df.write_csv(csv_buffer)
    csv_buffer.seek(0)

    # --- Delegate to the matching CSV parser ---
    if sheet_type == "collar":
        from georag_geoparsers.csv_collar import parse_csv_collars
        result = parse_csv_collars(csv_buffer, vendor_aliases=vendor_aliases)
        assay_columns: list = []
    elif sheet_type == "survey":
        from georag_geoparsers.csv_survey import parse_csv_surveys
        result = parse_csv_surveys(csv_buffer, vendor_aliases=vendor_aliases)
        assay_columns = []
    elif sheet_type == "lithology":
        from georag_geoparsers.csv_lithology import parse_csv_lithology
        result = parse_csv_lithology(csv_buffer, vendor_aliases=vendor_aliases)
        assay_columns = []
    elif sheet_type == "sample":
        from georag_geoparsers.csv_sample import parse_csv_samples
        result = parse_csv_samples(csv_buffer, vendor_aliases=vendor_aliases)
        assay_columns = getattr(result, "assay_columns", [])
    elif sheet_type == "structure":
        from georag_geoparsers.csv_structure import parse_csv_structures
        result = parse_csv_structures(csv_buffer, vendor_aliases=vendor_aliases)
        assay_columns = []
    elif sheet_type == "alteration":
        from georag_geoparsers.csv_alteration import parse_csv_alteration
        result = parse_csv_alteration(
            csv_buffer, vendor_aliases=vendor_aliases, companion=companion,
        )
        assay_columns = []
    elif sheet_type == "mineralization":
        from georag_geoparsers.csv_mineralization import parse_csv_mineralization
        result = parse_csv_mineralization(
            csv_buffer, vendor_aliases=vendor_aliases, companion=companion,
        )
        assay_columns = []
    else:
        raise ValueError(f"xlsx_parser: unknown sheet_type '{sheet_type}'")

    # The CSV parsers' own warnings are NOT all forwarded: their encoding and
    # delimiter notes describe the in-memory buffer built above, not the
    # workbook. The ones about the DATA are, because the CSV path surfaces
    # them and a workbook must not be the quieter way to lose a value.
    extra_warnings.extend(
        w for w in (getattr(result, "warnings", None) or [])
        if isinstance(w, dict) and w.get("code") in _FORWARDED_PARSER_WARNINGS
    )

    # Populate source_col_map now that the CSV parser has resolved column aliases.
    provenance["source_col_map"] = result.column_map or {}

    excel_result = ExcelParseResult(
        source_file=filename,
        sheet_name=resolved_sheet_name,
        sheet_type=sheet_type,
        format=file_format,
        total_rows=result.total_rows,
        valid_rows=result.valid_rows,
        skipped_rows=result.skipped_rows,
        parse_quality_pct=result.parse_quality_pct,
        unmapped_columns=result.unmapped_columns,
        records=result.records,
        skipped_details=getattr(result, "skipped_details", []),
        column_map=result.column_map,
        assay_columns=assay_columns,
        warnings=extra_warnings,
        provenance=provenance,
    )

    logger.info(
        "Excel parse complete — sheet='%s' type=%s format=%s total=%d valid=%d "
        "skipped=%d quality=%.1f%%",
        resolved_sheet_name, sheet_type, file_format,
        excel_result.total_rows, excel_result.valid_rows,
        excel_result.skipped_rows, excel_result.parse_quality_pct,
    )

    return excel_result
