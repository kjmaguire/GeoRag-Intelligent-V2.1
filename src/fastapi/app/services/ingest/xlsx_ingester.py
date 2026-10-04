"""XLSX ingester for Wyoming uranium drillhole archive.

Doc-phase 179 — Phase B Tier 1.

Reads spreadsheet content via `openpyxl`, lands sheet data as text
chunks in `silver.document_passages` (each sheet → N rows → N chunks).

For the 11 XLSX files in the WSGS archive, content is likely:
  - Collar tables (hole_id, easting, northing, depth)
  - Assay tables (hole_id, depth_from, depth_to, U_pct)
  - Lithology tables (hole_id, depth_from, depth_to, lithology)

Phase B Tier 1 just captures the data as searchable text. Phase B
Tier 2 will route XLSX content through the column-mapping wizard to
normalize into typed silver tables.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from app.services.ingest.file_hash import sha256_file

log = logging.getLogger("georag.ingest.xlsx")


@dataclass
class XLSXIngestResult:
    file_path: str
    document_id: str | None
    sheets_processed: int
    rows_total: int
    passages_inserted: int
    skipped: bool = False
    skipped_reason: str | None = None


#: Legacy Excel. openpyxl reads the OOXML .xlsx zip and nothing else, so it
#: raises InvalidFileException on the OLE2 binary that .xls actually is.
_XLS_SUFFIXES = frozenset({".xls", ".xlt"})


def _xls_sheet_texts(path: str) -> list[tuple[str, str]]:
    """Read a legacy .xls into (title, tab-separated text) pairs.

    Delegates to georag_geoparsers, which owns spreadsheet reading and is where
    xlrd is declared. This module cannot import xlrd directly:
    check_pyproject_covers_imports gates every import under app/ against
    src/fastapi/pyproject.toml, and adding the dist there would mean two
    readers for one format plus a uv.lock + requirements.lock.txt
    regeneration for a library that is already installed.

    xlsx_parser has had an xlrd path since it was written; this module -- the
    text fallback for sheets the drill classifier did not claim -- called
    openpyxl unconditionally, and openpyxl reads OOXML zips, not the OLE2
    binary that .xls is. A real customer file therefore reported
    "produced no searchable text" for data that was readable all along.
    """
    from georag_geoparsers.xlsx_parser import read_xls_sheets  # noqa: PLC0415

    # georag_geoparsers ships no py.typed, so mypy sees Any coming back.
    # Bind it to the declared shape rather than widening this function's
    # signature -- the contract is ours to state, not the untyped import's.
    sheets: list[tuple[str, str]] = read_xls_sheets(path)
    return sheets


def _format_sheet_as_text(sheet) -> str:
    """Format an openpyxl worksheet as tab-separated text.

    First row treated as header. Subsequent rows joined with newlines.
    """
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return ""
    lines = []
    for row in rows:
        cells = ["" if v is None else str(v).strip() for v in row]
        if any(c for c in cells):
            lines.append("\t".join(cells))
    return "\n".join(lines)


#: Characters per stored passage. Matches the PDF path's window so a
#: spreadsheet row and a report paragraph are comparable units of retrieval.
_SHEET_PASSAGE_CHARS = 5000


@dataclass(frozen=True)
class SheetPassage:
    """One stored passage of a sheet, with where in the workbook it came from.

    ``page`` is the 1-based index of the sheet in the workbook (a workbook has
    no pages; the sheet is the nearest unit a citation can point at, and
    ``silver.document_passages.page_first`` / ``page_last`` must be >= 1).
    ``row_first`` / ``row_last`` count NON-EMPTY rows of the sheet, header row
    = 1, because blank rows are dropped when the sheet is rendered to text.
    """

    ordinal: int
    text: str
    page: int
    row_first: int
    row_last: int


def _sheet_passages(
    sheet_texts: list[tuple[str, str]],
) -> list[SheetPassage]:
    """Split each sheet into as many passages as it needs.

    A sheet used to be rendered to one blob and hard-cut at 8,000 characters
    with a "[...truncated]" marker. On a 12,000-row assay workbook that is
    roughly 1.5 MB of text reduced to about 80 rows — 99.3% of the assays
    discarded, while the passage was still stored, embedded and retrievable,
    so chat answered assay questions from the first 80 rows and looked like
    it had the data. `rows_total` went on reporting the full count, so
    nothing in the result said otherwise.

    Splitting on line boundaries keeps rows whole; a row cut in half is a
    row that answers nothing. Every passage repeats the sheet header so a
    chunk retrieved on its own still says which sheet it came from and,
    when there is more than one, which part and which rows.

    "Sheet header" used to mean only the ``[Sheet: name]`` line. Parts 2..N
    of a 12,000-row assay sheet were then bare numbers: a retriever and an
    LLM could see ``0.42  1.13  0.07`` and not know which column was Au and
    which was Cu. The sheet's first row (the column headings, as the
    docstring of ``_format_sheet_as_text`` says) is therefore repeated at the
    top of every part after the first. A first row longer than half the
    window is not a column row and is not repeated.
    """
    passages: list[SheetPassage] = []
    ordinal = 0

    for sheet_index, (sheet_name, text) in enumerate(sheet_texts, start=1):
        header = f"[Sheet: {sheet_name}]"
        lines = (text or "").split("\n")
        column_row = lines[0] if lines else ""
        repeat_row = (
            column_row
            if column_row and len(column_row) <= _SHEET_PASSAGE_CHARS // 2
            else ""
        )

        # (first_row, last_row, lines) with 1-based row numbers, header = 1.
        chunks: list[tuple[int, int, list[str]]] = []
        current: list[str] = []
        first_row = 1
        size = 0
        has_data = False  # a part holding only the repeated column row is empty
        for row_no, line in enumerate(lines, start=1):
            # +1 for the newline this line will be rejoined with.
            if has_data and size + len(line) + 1 > _SHEET_PASSAGE_CHARS:
                chunks.append((first_row, row_no - 1, current))
                first_row = row_no
                # Parts 2..N open with the column row; its size counts
                # against the window so a part stays within it.
                current = [repeat_row] if repeat_row else []
                size = (len(repeat_row) + 1) if repeat_row else 0
                has_data = False
            current.append(line)
            size += len(line) + 1
            has_data = True
        if current:
            chunks.append((first_row, len(lines), current))
        if not chunks:
            chunks = [(1, 1, [""])]

        total = len(chunks)
        for index, (row_first, row_last, chunk_lines) in enumerate(chunks, start=1):
            if total == 1:
                label = header
            else:
                label = (
                    f"{header} (part {index} of {total}, "
                    f"rows {row_first}-{row_last} of {len(lines)})"
                )
            passages.append(
                SheetPassage(
                    ordinal=ordinal,
                    text=f"{label}\n" + "\n".join(chunk_lines),
                    page=sheet_index,
                    row_first=row_first,
                    row_last=row_last,
                )
            )
            ordinal += 1

    return passages


def _stable_report_id(
    *,
    workspace_id: str,
    project_id: str | None,
    source_identity: str,
) -> str:
    """Retry-stable report UUID for one project-scoped source.

    Same derivation as ``ingest_pdf._stable_report_id`` (not imported: this
    module is a service and ingest_pdf is a workflow that imports services).
    """
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            f"georag:report:{workspace_id}:{project_id}:{source_identity}",
        )
    )


async def ingest_xlsx_file(
    conn: asyncpg.Connection,
    xlsx_path: str,
    *,
    workspace_id: str,
    project_id: str | None = None,
    only_sheets: frozenset[str] | None = None,
) -> XLSXIngestResult:
    """Ingest one XLSX into silver.reports + silver.document_passages.

    ``only_sheets`` restricts the work to named worksheets. That is
    what ingest_tabular passes when a workbook classified partly: the
    Collars / Survey / Lithology tabs became typed rows, and the two
    tabs that matched nothing come here so they are at least
    answerable in chat. Passing the whole workbook instead would
    duplicate every drill row as a second, text-shaped copy competing
    with the typed one in the recall set.
    """
    from openpyxl import load_workbook

    p = Path(xlsx_path)
    if not p.is_file():
        return XLSXIngestResult(
            file_path=xlsx_path, document_id=None,
            sheets_processed=0, rows_total=0, passages_inserted=0,
            skipped=True, skipped_reason="file_not_found",
        )

    if only_sheets is not None and not only_sheets:
        return XLSXIngestResult(
            file_path=xlsx_path, document_id=None,
            sheets_processed=0, rows_total=0, passages_inserted=0,
            skipped=True, skipped_reason="no_sheets_requested",
        )

    # Legacy .xls never reaches openpyxl: it is an OLE2 binary, not an OOXML
    # zip, and load_workbook raises InvalidFileException on it. Handled here
    # rather than left to the except below so the geologist gets their data
    # instead of "produced no searchable text".
    if p.suffix.lower() in _XLS_SUFFIXES:
        try:
            xls_texts = await asyncio.to_thread(_xls_sheet_texts, str(p))
        except Exception as e:
            log.warning(
                "xlsx_ingester: xlrd could not read '%s': %s",
                xlsx_path, e, exc_info=True,
            )
            return XLSXIngestResult(
                file_path=xlsx_path, document_id=None,
                sheets_processed=0, rows_total=0, passages_inserted=0,
                skipped=True, skipped_reason=f"xlrd_failed:{type(e).__name__}",
            )
        if only_sheets is not None:
            xls_texts = [t for t in xls_texts if t[0] in only_sheets]
        if not xls_texts:
            return XLSXIngestResult(
                file_path=xlsx_path, document_id=None,
                sheets_processed=0, rows_total=0, passages_inserted=0,
                skipped=True, skipped_reason="empty_workbook",
            )
        return await land_sheets_as_text(
            conn,
            path=p,
            sheet_texts=xls_texts,
            total_rows=sum(t[1].count(chr(10)) + 1 for t in xls_texts),
            workspace_id=workspace_id,
            project_id=project_id,
            parser_used="xlrd",
        )

    try:
        # Hard rule 2 — openpyxl is sync and a large workbook is seconds of
        # CPU on the caller's event loop, which is a Hatchet worker's
        # heartbeat thread.
        wb = await asyncio.to_thread(load_workbook, p, read_only=True, data_only=True)
    except Exception as e:
        return XLSXIngestResult(
            file_path=xlsx_path, document_id=None,
            sheets_processed=0, rows_total=0, passages_inserted=0,
            skipped=True, skipped_reason=f"openpyxl_failed:{type(e).__name__}",
        )

    # Build a single combined text per sheet.
    #
    # `wb.close()` is not optional under read_only=True: openpyxl
    # streams from the still-open .xlsx zip rather than reading it into
    # memory, so the handle lives until the workbook is collected. On a
    # long-lived Hatchet worker that is one leaked descriptor per
    # ingest; on Windows it is worse than a leak, because the enclosing
    # TemporaryDirectory then cannot delete the file and the cleanup
    # raises PermissionError over the whole run. Found by probing this
    # path, not by reading it.
    sheet_texts: list[tuple[str, str]] = []
    total_rows = 0
    try:
        for ws in wb.worksheets:
            if only_sheets is not None and ws.title not in only_sheets:
                continue
            text = _format_sheet_as_text(ws)
            if not text:
                continue
            row_count = text.count("\n") + 1
            total_rows += row_count
            sheet_texts.append((ws.title, text))
    finally:
        with contextlib.suppress(Exception):
            wb.close()

    if not sheet_texts:
        return XLSXIngestResult(
            file_path=xlsx_path, document_id=None,
            sheets_processed=0, rows_total=0, passages_inserted=0,
            skipped=True, skipped_reason="empty_workbook",
        )

    return await land_sheets_as_text(
        conn,
        path=p,
        sheet_texts=sheet_texts,
        total_rows=total_rows,
        workspace_id=workspace_id,
        project_id=project_id,
        parser_used="openpyxl",
    )


async def land_sheets_as_text(
    conn: asyncpg.Connection,
    *,
    path: Path,
    sheet_texts: list[tuple[str, str]],
    total_rows: int,
    workspace_id: str,
    project_id: str | None,
    parser_used: str,
) -> XLSXIngestResult:
    """Land already-rendered sheet text as a report plus its passages.

    Format-agnostic on purpose: a workbook and a delimited file differ only
    in how their rows are read, and the second caller was going to copy
    seventy lines of report-dedupe and passage-insert to avoid saying so.
    """
    p = path
    # Off the event loop, streamed (ING-18).
    sha = await asyncio.to_thread(sha256_file, p)
    # The report row is keyed by a DETERMINISTIC id derived from
    # workspace / project / file sha, and written with ON CONFLICT. It used
    # to be SELECT-then-INSERT with gen_random_uuid() and no unique key, so
    # two concurrent ingests of one workbook (or a Hatchet retry racing its
    # own first attempt) both found nothing and inserted two report rows,
    # each with a full set of passages.
    #
    # Project scope is part of the key, as it was part of the old lookup:
    # the same workbook uploaded into a SECOND project must not find the
    # FIRST project's report (two teams sharing a standard assay template
    # is not an exotic way for a sha to collide).
    #
    # Rows written before this change carry random ids and are not matched;
    # re-ingesting such a file creates one new report under the stable id.
    report_id = _stable_report_id(
        workspace_id=workspace_id, project_id=project_id, source_identity=sha,
    )
    row = await conn.fetchrow(
        """
        INSERT INTO silver.reports
            (report_id, project_id, workspace_id, title, commodity,
             source_file_sha256, is_scanned, parser_used,
             created_at, updated_at)
        VALUES ($1::uuid, $2::uuid, $3::uuid, $4, NULL,
                $5, false, $6,
                NOW(), NOW())
        ON CONFLICT (report_id) DO UPDATE SET
            title      = EXCLUDED.title,
            parser_used = EXCLUDED.parser_used,
            updated_at = NOW()
        RETURNING report_id::text AS report_id
        """,
        report_id, project_id, workspace_id, p.stem[:500], sha, parser_used,
    )
    document_id = row["report_id"]

    # One or more passages per sheet. Tabular content is not
    # paragraph-chunked — _sheet_passages splits on row boundaries only, so
    # a row is never cut in half. page_first/page_last carry the sheet's
    # 1-based index so a citation can name the sheet; the row range is in the
    # passage text header (there is no row column on document_passages).
    inserted = 0
    for sp in _sheet_passages(sheet_texts):
        h = hashlib.sha256(sp.text.encode()).hexdigest()
        try:
            r = await conn.fetchrow(
                """
                INSERT INTO silver.document_passages
                    (passage_id, document_id, workspace_id, revision_number,
                     text, text_hash, ordinal, chunk_kind,
                     page_first, page_last, created_at, updated_at)
                VALUES (gen_random_uuid(), $1::uuid, $2::uuid, 1, $3, $4, $5,
                        'table', $6::int, $7::int, NOW(), NOW())
                ON CONFLICT (document_id, revision_number, text_hash) DO NOTHING
                RETURNING passage_id
                """,
                document_id, workspace_id, sp.text, h, sp.ordinal,
                sp.page, sp.page,
            )
            if r:
                inserted += 1
        except Exception as e:
            log.warning("xlsx_ingester.passage_insert_failed err=%s", e)

    return XLSXIngestResult(
        file_path=str(p),
        document_id=document_id,
        sheets_processed=len(sheet_texts),
        rows_total=total_rows,
        passages_inserted=inserted,
    )


async def ingest_delimited_as_text(
    conn: asyncpg.Connection,
    csv_path: str,
    *,
    workspace_id: str,
    project_id: str | None = None,
) -> XLSXIngestResult:
    """Land one CSV/TSV as searchable text.

    The fallback for a delimited file whose headers match no drill sheet
    type. Before this the answer was a `nothing_classified` warning telling
    the user to "pass sheet_type explicitly if the headers are unusual" —
    advice they cannot act on for a file that arrived inside a ZIP, since
    the archive branch deliberately passes no hint. The file was simply not
    in the system.

    Reads through the same ``_csv_io`` helpers the parsers use: these
    arrive as Latin-1 from Windows survey software and semicolon-delimited
    from European labs, and splitting on the wrong delimiter would store
    one column per row.
    """
    import csv  # noqa: PLC0415

    from georag_geoparsers._csv_io import (  # noqa: PLC0415
        detect_delimiter,
        open_csv_with_encoding,
    )

    p = Path(csv_path)
    if not p.is_file():
        return XLSXIngestResult(
            file_path=csv_path, document_id=None,
            sheets_processed=0, rows_total=0, passages_inserted=0,
            skipped=True, skipped_reason="file_not_found",
        )

    try:
        stream, _encoding, _sha, _size = await asyncio.to_thread(
            open_csv_with_encoding, csv_path,
        )
        content = await asyncio.to_thread(stream.read)
        delimiter = detect_delimiter(content)
    except Exception as e:
        return XLSXIngestResult(
            file_path=csv_path, document_id=None,
            sheets_processed=0, rows_total=0, passages_inserted=0,
            skipped=True, skipped_reason=f"csv_read_failed:{type(e).__name__}",
        )

    lines: list[str] = []
    for row in csv.reader(content.splitlines(), delimiter=delimiter):
        cells = ["" if v is None else str(v).strip() for v in row]
        if any(cells):
            # Tab-separated to match the workbook path, so a retrieved
            # passage reads the same whichever file it came from.
            lines.append("\t".join(cells))

    if not lines:
        return XLSXIngestResult(
            file_path=csv_path, document_id=None,
            sheets_processed=0, rows_total=0, passages_inserted=0,
            skipped=True, skipped_reason="empty_file",
        )

    return await land_sheets_as_text(
        conn,
        path=p,
        sheet_texts=[(p.stem, "\n".join(lines))],
        total_rows=len(lines),
        workspace_id=workspace_id,
        project_id=project_id,
        parser_used="csv-text",
    )


__all__ = [
    "ingest_xlsx_file",
    "ingest_delimited_as_text",
    "land_sheets_as_text",
    "SheetPassage",
    "XLSXIngestResult",
]
