"""§04p persistence layer — writes orchestrator output to 8 silver tables.

**Master-plan §3 Step 7 (part B — doc-phase 56).** Internal module
(leading underscore): only the Hatchet `ingest_pdf` workflow + tests
should import it.

Flow:
1. Caller acquires an asyncpg connection in a transaction with the
   workspace_id GUC set (use the `transactional_workspace_session`
   context manager).
2. Caller invokes `persist_orchestrator_result(conn, workspace_id,
   report_id, result)` where ``result`` is the dict returned by
   `app.ocr._orchestrator.orchestrate(pdf_path)`.
3. The function writes to all 8 silver tables + parser_run_artifacts
   rows, all inside the existing transaction.

The function returns a count dict (table_name → row count written)
for telemetry / handoff debugging.
"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import asyncpg


def _dsn() -> str:
    """Build the direct-Postgres DSN. Bypasses pgbouncer because
    `set_config('app.workspace_id', ..., true)` is transaction-local
    and needs a stable session.
    """
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@asynccontextmanager
async def transactional_workspace_session(
    pool: asyncpg.Pool,
    workspace_id: str,
) -> AsyncIterator[asyncpg.Connection]:
    """Acquire a connection, open a transaction, set the workspace_id GUC.

    Usage:
        async with transactional_workspace_session(pool, ws_id) as conn:
            await persist_orchestrator_result(conn, ws_id, report_id, result)
    """
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, true)",
                str(workspace_id),
            )
            yield conn


# ---------------------------------------------------------------------------
# Mapping: page profile → parser_used label for ocr_page_quality
# ---------------------------------------------------------------------------
_PARSER_FOR_PROFILE: dict[str, str] = {
    "native": "native",
    "scanned": "scanned_paddleocr",
    "mixed": "mixed_docling",
    "table_heavy": "table_heavy",
    "map_heavy": "map_heavy_unparsed",
}

# Mapping: parses[key] → parser_used label for parser_run_artifacts
_PARSE_KEY_TO_PARSER: dict[str, str] = {
    "native": "native",
    "scanned": "scanned_paddleocr",
    "mixed": "mixed_docling",
    "table_heavy": "table_heavy",
}

# Source method for ingest_ocr_results based on retry-level settings
_SCANNED_SOURCE_METHOD_BY_RETRY: dict[int, str] = {
    0: "paddleocr_pp_ocrv5",
    1: "paddleocr_pp_ocrv5_retry_binarized",
    2: "paddleocr_pp_ocrv5_retry_lang_hint",
}


async def persist_orchestrator_result(
    conn: asyncpg.Connection,
    workspace_id: str,
    report_id: str,
    result: dict[str, Any],
    bronze_s3_key: str | None = None,
) -> dict[str, int]:
    """Write orchestrator output to all relevant silver tables.

    Assumes `conn` is in a transaction with the workspace_id GUC set.

    Args:
        conn: live asyncpg.Connection inside a transaction.
        workspace_id: UUID string of the workspace.
        report_id: UUID string of the silver.reports row.
        result: dict returned by app.ocr._orchestrator.orchestrate().
        bronze_s3_key: optional bronze S3 key (e.g. "reports/{project}/{ts}_file.pdf").
            When provided, the preflight parser_run_artifacts row's
            ``raw_output_uri`` is populated so the Silver Review UI's
            page-render endpoint (doc-phase 59) can reconstruct the
            bronze URI without an extra schema column.

    Returns:
        Per-table row count dict.
    """
    counts: dict[str, int] = {
        "parser_run_artifacts": 0,
        "ocr_page_quality": 0,
        "document_ingestion_quality": 0,
        "table_extraction_quality": 0,
        "low_confidence_page_reviews": 0,
        "ingest_extractions": 0,
        "ingest_layouts": 0,
        "ingest_ocr_results": 0,
    }

    pf = result.get("preflight") or {}
    prof = result.get("profile")
    parses = result.get("parses") or {}
    route_decisions = result.get("route_decisions") or []
    doc_summary = result.get("document_summary") or {}

    # ---- 1. parser_run_artifacts ----
    await _insert_parser_artifact(
        conn, workspace_id, report_id, "preflight", "qpdf+pikepdf",
        errors=[] if pf.get("valid") else [pf.get("error") or "unknown"],
        warnings=[],
        raw_output_uri=bronze_s3_key,
    )
    counts["parser_run_artifacts"] += 1

    if prof is not None:
        await _insert_parser_artifact(
            conn, workspace_id, report_id, "profiler",
            "pdfplumber-heuristic",
            errors=[],
            warnings=[],
        )
        counts["parser_run_artifacts"] += 1

    for parse_key, parse_result in parses.items():
        if not parse_result:
            continue
        parser_used = _PARSE_KEY_TO_PARSER.get(parse_key, "p04p")
        await _insert_parser_artifact(
            conn, workspace_id, report_id, parser_used, "v1",
            errors=[],
            warnings=[],
        )
        counts["parser_run_artifacts"] += 1

    # ---- 2. Per-page quality + review rows ----
    per_page_profiles: list[str] = (prof or {}).get("per_page_profiles", [])
    for decision in route_decisions:
        page = int(decision["page"])
        scores = decision.get("confidence_scores") or {}
        page_profile = (
            per_page_profiles[page]
            if 0 <= page < len(per_page_profiles)
            else "native"
        )
        parser_used = _PARSER_FOR_PROFILE.get(page_profile, "native")
        needs_review = decision["route"] in {"re_ocr", "silver_review"}
        retry_count = int(decision.get("retry_count", 0))

        await _insert_ocr_page_quality(
            conn, workspace_id, report_id, page,
            ocr_confidence=_to_db_numeric(scores.get("ocr_confidence")),
            layout_confidence=_to_db_numeric(scores.get("layout_confidence")),
            table_confidence=_to_db_numeric(
                scores.get("min_table_structure_confidence")
            ),
            parser_used=parser_used,
            retry_count=retry_count,
            needs_review=needs_review,
        )
        counts["ocr_page_quality"] += 1

        if decision["route"] == "silver_review":
            reason = decision.get("reason") or "other"
            await _insert_review_row(
                conn, workspace_id, report_id, page, reason
            )
            counts["low_confidence_page_reviews"] += 1

    # ---- 3. document_ingestion_quality ----
    total_pages = pf.get("page_count") or len(route_decisions) or 0
    await _insert_document_quality(
        conn, workspace_id, report_id,
        total_pages=total_pages,
        low_confidence_pages=doc_summary.get("review_count", 0),
        table_pages=sum(1 for d in route_decisions if d.get("page_profile") == "table_heavy"),
        map_pages=sum(
            1 for p in per_page_profiles if p == "map_heavy"
        ),
        overall_quality_score=_compute_doc_quality_score(route_decisions),
        recommended_action=doc_summary.get("recommended_action", "accept"),
    )
    counts["document_ingestion_quality"] += 1

    # ---- 4. Per-region rows (extractions / layouts / ocr_results) ----
    # Per-page region counters; ensure (report_id, page, region) PK uniqueness
    # across parsers writing to the same per-region tables.
    region_counter_extractions: dict[int, int] = {}
    region_counter_layouts: dict[int, int] = {}
    region_counter_ocr: dict[int, int] = {}

    for parse_key, parse_result in parses.items():
        if not parse_result:
            continue

        if parse_key in ("native", "mixed", "table_heavy"):
            for passage in parse_result.get("passages", []):
                page = int(passage["page"])
                region = region_counter_extractions.setdefault(page, 0)
                region_counter_extractions[page] = region + 1
                await _insert_extraction(
                    conn, workspace_id, report_id, page, region, passage,
                )
                counts["ingest_extractions"] += 1

        if parse_key == "mixed":
            for layout in parse_result.get("layouts", []):
                page = int(layout["page"])
                region = region_counter_layouts.setdefault(page, 0)
                region_counter_layouts[page] = region + 1
                await _insert_layout(
                    conn, workspace_id, report_id, page, region, layout,
                )
                counts["ingest_layouts"] += 1

        if parse_key == "scanned":
            # Determine retry level for source_method label.
            retry_counts = parse_result.get("per_page_retry_counts", [])
            for passage in parse_result.get("passages", []):
                page = int(passage["page"])
                region = region_counter_ocr.setdefault(page, 0)
                region_counter_ocr[page] = region + 1
                page_retry = (
                    retry_counts[page]
                    if 0 <= page < len(retry_counts)
                    else 0
                )
                source_method = _SCANNED_SOURCE_METHOD_BY_RETRY.get(
                    page_retry, "paddleocr_pp_ocrv5"
                )
                await _insert_ocr_result(
                    conn, workspace_id, report_id, page, region,
                    passage, source_method,
                )
                counts["ingest_ocr_results"] += 1

        # Tables → table_extraction_quality (one row per table)
        for table_idx, table in enumerate(parse_result.get("tables", [])):
            await _insert_table_quality(
                conn, workspace_id, report_id, table,
            )
            counts["table_extraction_quality"] += 1

    return counts


# ---------------------------------------------------------------------------
# Per-table insert helpers
# ---------------------------------------------------------------------------

async def _insert_parser_artifact(
    conn: asyncpg.Connection,
    workspace_id: str,
    report_id: str,
    parser_used: str,
    parser_version: str,
    *,
    errors: list[Any],
    warnings: list[Any],
    raw_output_uri: str | None = None,
) -> None:
    await conn.execute(
        """
        INSERT INTO silver.parser_run_artifacts (
            report_id, workspace_id, parser_used, parser_version,
            raw_output_uri, errors, warnings, started_at, finished_at
        ) VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6::jsonb, $7::jsonb, NOW(), NOW())
        """,
        report_id, workspace_id, parser_used, parser_version,
        raw_output_uri,
        json.dumps(errors), json.dumps(warnings),
    )


async def _insert_ocr_page_quality(
    conn: asyncpg.Connection,
    workspace_id: str,
    report_id: str,
    page: int,
    *,
    ocr_confidence: float | None,
    layout_confidence: float | None,
    table_confidence: float | None,
    parser_used: str,
    retry_count: int,
    needs_review: bool,
) -> None:
    await conn.execute(
        """
        INSERT INTO silver.ocr_page_quality (
            report_id, page, workspace_id,
            ocr_confidence, layout_confidence, table_confidence,
            parser_used, retry_count, needs_review
        ) VALUES ($1::uuid, $2, $3::uuid, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (report_id, page) DO UPDATE SET
            ocr_confidence = EXCLUDED.ocr_confidence,
            layout_confidence = EXCLUDED.layout_confidence,
            table_confidence = EXCLUDED.table_confidence,
            parser_used = EXCLUDED.parser_used,
            retry_count = EXCLUDED.retry_count,
            needs_review = EXCLUDED.needs_review,
            last_evaluated_at = NOW()
        """,
        report_id, page, workspace_id,
        ocr_confidence, layout_confidence, table_confidence,
        parser_used, retry_count, needs_review,
    )


async def _insert_document_quality(
    conn: asyncpg.Connection,
    workspace_id: str,
    report_id: str,
    *,
    total_pages: int,
    low_confidence_pages: int,
    table_pages: int,
    map_pages: int,
    overall_quality_score: float | None,
    recommended_action: str,
) -> None:
    # Guard CHECK constraint: total_pages > 0
    if total_pages <= 0:
        total_pages = 1
    # Guard CHECK constraint: low_confidence_pages <= total_pages
    low_confidence_pages = min(low_confidence_pages, total_pages)
    table_pages = min(table_pages, total_pages)
    map_pages = min(map_pages, total_pages)

    await conn.execute(
        """
        INSERT INTO silver.document_ingestion_quality (
            report_id, workspace_id, total_pages,
            low_confidence_pages, table_pages, map_pages,
            overall_quality_score, recommended_action
        ) VALUES ($1::uuid, $2::uuid, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (report_id) DO UPDATE SET
            total_pages = EXCLUDED.total_pages,
            low_confidence_pages = EXCLUDED.low_confidence_pages,
            table_pages = EXCLUDED.table_pages,
            map_pages = EXCLUDED.map_pages,
            overall_quality_score = EXCLUDED.overall_quality_score,
            recommended_action = EXCLUDED.recommended_action
        """,
        report_id, workspace_id, total_pages,
        low_confidence_pages, table_pages, map_pages,
        overall_quality_score, recommended_action,
    )


async def _insert_review_row(
    conn: asyncpg.Connection,
    workspace_id: str,
    report_id: str,
    page: int,
    reason: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO silver.low_confidence_page_reviews (
            report_id, page, workspace_id, reason, status
        ) VALUES ($1::uuid, $2, $3::uuid, $4, 'pending')
        ON CONFLICT (report_id, page, reason) DO NOTHING
        """,
        report_id, page, workspace_id, reason,
    )


async def _insert_table_quality(
    conn: asyncpg.Connection,
    workspace_id: str,
    report_id: str,
    table: dict[str, Any],
) -> None:
    parser_used = table.get("parser_used", "pdfplumber")
    # Map orchestrator-side parser_used to the silver enum.
    parser_used_enum = {
        "pdfplumber": "pdfplumber",
        "docling_tableformer": "docling_tableformer",
        "docling_tableformer_v2": "docling_tableformer_v2",
        "paddleocr_pp_structure_v3": "paddleocr_pp_structure_v3",
    }.get(parser_used, "pdfplumber")

    await conn.execute(
        """
        INSERT INTO silver.table_extraction_quality (
            report_id, page, table_id, workspace_id,
            structure_confidence, cell_confidence,
            header_detected, parser_used, needs_review
        ) VALUES ($1::uuid, $2, $3, $4::uuid, $5, $6, $7, $8, $9)
        ON CONFLICT (report_id, page, table_id) DO UPDATE SET
            structure_confidence = EXCLUDED.structure_confidence,
            cell_confidence = EXCLUDED.cell_confidence,
            header_detected = EXCLUDED.header_detected,
            parser_used = EXCLUDED.parser_used,
            needs_review = EXCLUDED.needs_review
        """,
        report_id, int(table["page"]), int(table["table_id"]), workspace_id,
        _to_db_numeric(table.get("structure_confidence")),
        _to_db_numeric(table.get("cell_confidence")),
        bool(table.get("header_detected", False)),
        parser_used_enum,
        bool(table.get("needs_review", False)),
    )


async def _insert_extraction(
    conn: asyncpg.Connection,
    workspace_id: str,
    report_id: str,
    page: int,
    region: int,
    passage: dict[str, Any],
) -> None:
    await conn.execute(
        """
        INSERT INTO silver.ingest_extractions (
            report_id, page, region, workspace_id,
            bbox, source_method, extraction_confidence,
            text_content, payload
        ) VALUES ($1::uuid, $2, $3, $4::uuid,
                  $5::numeric[], $6, $7, $8, $9::jsonb)
        ON CONFLICT (report_id, page, region) DO UPDATE SET
            bbox = EXCLUDED.bbox,
            source_method = EXCLUDED.source_method,
            extraction_confidence = EXCLUDED.extraction_confidence,
            text_content = EXCLUDED.text_content,
            payload = EXCLUDED.payload
        """,
        report_id, page, region, workspace_id,
        passage.get("bbox", []),
        _coerce_source_method_extractions(passage.get("source_method", "pdfminer_six")),
        _to_db_numeric(passage.get("extraction_confidence")),
        passage.get("text_content", ""),
        json.dumps({
            "layout_label": passage.get("layout_label"),
            "coord_origin": "BOTTOMLEFT",  # pdfminer.six + Docling default
        }),
    )


async def _insert_layout(
    conn: asyncpg.Connection,
    workspace_id: str,
    report_id: str,
    page: int,
    region: int,
    layout: dict[str, Any],
) -> None:
    await conn.execute(
        """
        INSERT INTO silver.ingest_layouts (
            report_id, page, region, workspace_id,
            bbox, source_method, extraction_confidence,
            layout_label, payload
        ) VALUES ($1::uuid, $2, $3, $4::uuid,
                  $5::numeric[], $6, $7, $8, $9::jsonb)
        ON CONFLICT (report_id, page, region) DO UPDATE SET
            bbox = EXCLUDED.bbox,
            source_method = EXCLUDED.source_method,
            extraction_confidence = EXCLUDED.extraction_confidence,
            layout_label = EXCLUDED.layout_label,
            payload = EXCLUDED.payload
        """,
        report_id, page, region, workspace_id,
        layout.get("bbox", []),
        layout.get("source_method", "docling_layout_default"),
        _to_db_numeric(layout.get("extraction_confidence")),
        layout.get("layout_label", "other"),
        json.dumps({
            "has_text": bool(layout.get("has_text", False)),
            "coord_origin": "BOTTOMLEFT",
        }),
    )


async def _insert_ocr_result(
    conn: asyncpg.Connection,
    workspace_id: str,
    report_id: str,
    page: int,
    region: int,
    passage: dict[str, Any],
    source_method: str,
) -> None:
    await conn.execute(
        """
        INSERT INTO silver.ingest_ocr_results (
            report_id, page, region, workspace_id,
            bbox, source_method, extraction_confidence,
            ocr_text, char_confidences, payload
        ) VALUES ($1::uuid, $2, $3, $4::uuid,
                  $5::numeric[], $6, $7, $8, $9::jsonb, $10::jsonb)
        ON CONFLICT (report_id, page, region) DO UPDATE SET
            bbox = EXCLUDED.bbox,
            source_method = EXCLUDED.source_method,
            extraction_confidence = EXCLUDED.extraction_confidence,
            ocr_text = EXCLUDED.ocr_text,
            char_confidences = EXCLUDED.char_confidences,
            payload = EXCLUDED.payload
        """,
        report_id, page, region, workspace_id,
        passage.get("bbox", []),
        source_method,
        _to_db_numeric(passage.get("extraction_confidence")),
        passage.get("text_content", ""),
        json.dumps([]),  # char-level confidences not yet captured per char
        json.dumps({
            "coord_origin": "TOPLEFT_IMAGE",
            "render_scale": passage.get("render_scale", 2.0),
        }),
    )


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _to_db_numeric(value: Any) -> float | None:
    """Coerce orchestrator-side floats (or None) into asyncpg-friendly types
    for NUMERIC columns. asyncpg accepts float for NUMERIC.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_source_method_extractions(method: str) -> str:
    """Coerce orchestrator-side source_method to the silver.ingest_extractions
    CHECK enum values.
    """
    allowed = {
        "pdfminer_six",
        "pdfplumber_text",
        "pdfplumber_table_cell",
        "docling_text_region",
        "docling_table_cell",
    }
    return method if method in allowed else "pdfminer_six"


def _compute_doc_quality_score(route_decisions: list[dict[str, Any]]) -> float | None:
    """Simple aggregate: ratio of accept routes to total routes.

    A more sophisticated score (factor in OCR confidence, layout
    confidence, retry counts) can come in Step 9 corpus tuning.
    """
    if not route_decisions:
        return None
    accept = sum(1 for d in route_decisions if d.get("route") == "accept")
    return round(accept / len(route_decisions), 4)
