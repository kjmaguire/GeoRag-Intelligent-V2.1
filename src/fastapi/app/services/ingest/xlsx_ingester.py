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

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path

import asyncpg

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


async def ingest_xlsx_file(
    conn: asyncpg.Connection,
    xlsx_path: str,
    *,
    workspace_id: str,
    project_id: str | None = None,
) -> XLSXIngestResult:
    """Ingest one XLSX into silver.reports + silver.document_passages."""
    from openpyxl import load_workbook

    p = Path(xlsx_path)
    if not p.is_file():
        return XLSXIngestResult(
            file_path=xlsx_path, document_id=None,
            sheets_processed=0, rows_total=0, passages_inserted=0,
            skipped=True, skipped_reason="file_not_found",
        )

    try:
        wb = load_workbook(p, read_only=True, data_only=True)
    except Exception as e:
        return XLSXIngestResult(
            file_path=xlsx_path, document_id=None,
            sheets_processed=0, rows_total=0, passages_inserted=0,
            skipped=True, skipped_reason=f"openpyxl_failed:{type(e).__name__}",
        )

    # Build a single combined text per sheet
    sheet_texts: list[tuple[str, str]] = []
    total_rows = 0
    for ws in wb.worksheets:
        text = _format_sheet_as_text(ws)
        if not text:
            continue
        row_count = text.count("\n") + 1
        total_rows += row_count
        sheet_texts.append((ws.title, text))

    if not sheet_texts:
        return XLSXIngestResult(
            file_path=xlsx_path, document_id=None,
            sheets_processed=0, rows_total=0, passages_inserted=0,
            skipped=True, skipped_reason="empty_workbook",
        )

    # SHA + dedupe
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    row = await conn.fetchrow(
        "SELECT report_id::text AS report_id FROM silver.reports "
        "WHERE source_file_sha256 = $1 LIMIT 1",
        sha,
    )
    if row:
        document_id = row["report_id"]
    else:
        row = await conn.fetchrow(
            """
            INSERT INTO silver.reports
                (report_id, project_id, workspace_id, title, commodity,
                 source_file_sha256, is_scanned, parser_used,
                 created_at, updated_at)
            VALUES (gen_random_uuid(), $1::uuid, $2::uuid, $3, 'uranium',
                    $4, false, 'openpyxl',
                    NOW(), NOW())
            RETURNING report_id::text AS report_id
            """,
            project_id, workspace_id, p.stem[:500], sha,
        )
        document_id = row["report_id"]

    # Insert one passage per sheet (kept whole; tabular content shouldn't
    # be paragraph-chunked).
    inserted = 0
    for ordinal, (sheet_name, text) in enumerate(sheet_texts):
        text_with_header = f"[Sheet: {sheet_name}]\n{text}"
        # Truncate to avoid massive single passages
        if len(text_with_header) > 8000:
            text_with_header = text_with_header[:8000] + "\n[...truncated]"
        h = hashlib.sha256(text_with_header.encode()).hexdigest()
        try:
            r = await conn.fetchrow(
                """
                INSERT INTO silver.document_passages
                    (passage_id, document_id, workspace_id, revision_number,
                     text, text_hash, ordinal, chunk_kind, created_at, updated_at)
                VALUES (gen_random_uuid(), $1::uuid, $2::uuid, 1, $3, $4, $5,
                        'table', NOW(), NOW())
                ON CONFLICT (document_id, revision_number, text_hash) DO NOTHING
                RETURNING passage_id
                """,
                document_id, workspace_id, text_with_header, h, ordinal,
            )
            if r:
                inserted += 1
        except Exception as e:
            log.warning("xlsx_ingester.passage_insert_failed err=%s", e)

    return XLSXIngestResult(
        file_path=xlsx_path,
        document_id=document_id,
        sheets_processed=len(sheet_texts),
        rows_total=total_rows,
        passages_inserted=inserted,
    )


__all__ = ["ingest_xlsx_file", "XLSXIngestResult"]
