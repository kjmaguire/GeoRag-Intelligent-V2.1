"""§04p re-OCR page workflow (master-plan §3 Step 8e, doc-phase 63).

Triggered when an operator clicks "Re-OCR requested" in the Silver
Review queue (doc-phase 61 disposition controls). The workflow:

1. Looks up the bronze S3 key for the report from
   silver.parser_run_artifacts.raw_output_uri (doc-phase 59 tracking).
2. Looks up the current retry_count from silver.ocr_page_quality so
   we know which escalation settings to use.
3. Refuses if retry_count is already at MAX_OCR_RETRIES (caller can
   surface the error to the operator).
4. Downloads the bronze PDF from S3.
5. Calls parse_scanned with the next-attempt settings from
   quality_graph.RETRY_SETTINGS_BY_ATTEMPT.
6. Persists new rows: silver.ingest_ocr_results (one per OCR'd
   region) + updates silver.ocr_page_quality.retry_count +
   ocr_confidence + writes a silver.parser_run_artifacts row for the
   retry pass.
7. Emits two audit events: re_ocr_page.start + re_ocr_page.complete.

Scope deliberately narrow for doc-phase 63:
- The function re-OCRs the ONE page identified by (report_id, page).
- It does NOT create a follow-up review item if the new confidence
  is still low — doc-phase 64 may add that.
- It does NOT update the existing review_item's status — that's
  terminal (`resolved_reocr_requested`) at the schema level.
  Operators viewing the queue after re-OCR completes can see the
  new ocr_confidence in the per-page row.
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from uuid import UUID

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.db import bind_workspace_scope
from app.hatchet_workflows import hatchet
from app.ocr.parse_scanned import parse_scanned
from app.ocr.quality_graph import MAX_OCR_RETRIES, RETRY_SETTINGS_BY_ATTEMPT

log = logging.getLogger("georag.hatchet.re_ocr_page")


# =============================================================================
# Input + output models
# =============================================================================
class ReOcrPageInput(BaseModel):
    """Trigger payload from Laravel admin (doc-phase 63 disposition wiring)."""

    workspace_id: UUID
    report_id: UUID
    page: int = Field(..., ge=0, le=10000)
    review_item_id: UUID | None = Field(
        default=None,
        description="Optional review_item_id for audit-trail cross-referencing.",
    )
    actor_id: int | None = Field(
        default=None,
        description="Operator user id (public.users.id) who triggered the re-OCR.",
    )


class ReOcrPageOutput(BaseModel):
    success: bool
    new_ocr_confidence: float | None = None
    new_text_line_count: int | None = None
    retry_attempt: int = 0
    error: str | None = None


re_ocr_page = hatchet.workflow(
    name="re_ocr_page",
    input_validator=ReOcrPageInput,
)


# =============================================================================
# DSN + S3 helpers — mirror ingest_pdf conventions
# =============================================================================
def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


def _s3_endpoint() -> str:
    return os.environ.get(
        "S3_ENDPOINT_URL",
        os.environ.get("MINIO_ENDPOINT", "http://minio:8333"),
    )


def _s3_credentials() -> tuple[str, str]:
    return (
        os.environ.get("AWS_ACCESS_KEY_ID")
        or os.environ.get("MINIO_ROOT_USER", "georag-admin"),
        os.environ.get("AWS_SECRET_ACCESS_KEY")
        or os.environ.get("MINIO_ROOT_PASSWORD", ""),
    )


async def _download_from_s3(minio_key: str) -> bytes:
    import aioboto3
    sess = aioboto3.Session(
        aws_access_key_id=_s3_credentials()[0],
        aws_secret_access_key=_s3_credentials()[1],
        region_name="us-east-1",
    )
    bucket = os.environ.get("MINIO_BUCKET_BRONZE", "bronze")
    async with sess.client("s3", endpoint_url=_s3_endpoint()) as s3:
        resp = await s3.get_object(Bucket=bucket, Key=minio_key)
        return await resp["Body"].read()


# =============================================================================
# The workflow task
# =============================================================================
@re_ocr_page.task(execution_timeout="15m", retries=1)
async def execute(input: ReOcrPageInput, ctx: Context) -> ReOcrPageOutput:
    """Re-OCR one page of a Bronze-stored PDF with escalated settings."""
    workspace_id = str(input.workspace_id)
    report_id = str(input.report_id)
    page = input.page

    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        await bind_workspace_scope(
            conn, workspace_id=workspace_id, site="hatchet.re_ocr_page"
        )

        # ---- Stage 1: look up bronze key + current retry_count ----
        bronze_key = await conn.fetchval(
            """
            SELECT raw_output_uri
            FROM silver.parser_run_artifacts
            WHERE report_id = $1::uuid
              AND parser_used = 'preflight'
              AND raw_output_uri IS NOT NULL
            ORDER BY started_at DESC
            LIMIT 1
            """,
            report_id,
        )
        if not bronze_key:
            return ReOcrPageOutput(
                success=False,
                error="no_bronze_key_tracked",
                retry_attempt=0,
            )

        current_retry = await conn.fetchval(
            """
            SELECT retry_count
            FROM silver.ocr_page_quality
            WHERE report_id = $1::uuid AND page = $2
            """,
            report_id, page,
        )
        current_retry = int(current_retry) if current_retry is not None else 0

        if current_retry >= MAX_OCR_RETRIES:
            return ReOcrPageOutput(
                success=False,
                error=f"retry_max_exceeded ({current_retry}/{MAX_OCR_RETRIES})",
                retry_attempt=current_retry,
            )

        retry_attempt = current_retry  # 0-indexed; this is the NEXT attempt
        settings = RETRY_SETTINGS_BY_ATTEMPT[retry_attempt]

        # Emit start-of-run audit
        try:
            await emit_audit(
                conn,
                action_type="re_ocr_page.start",
                workspace_id=input.workspace_id,
                actor_id=input.actor_id,
                actor_kind="workflow",
                target_schema="silver",
                target_table="low_confidence_page_reviews",
                target_id=str(input.review_item_id) if input.review_item_id else None,
                payload={
                    "report_id": report_id,
                    "page": page,
                    "retry_attempt": retry_attempt,
                    "settings": settings,
                    "review_item_id": str(input.review_item_id) if input.review_item_id else None,
                },
                trace_id=ctx.workflow_run_id,
            )
        except Exception as e:
            log.warning("re_ocr_page audit emit (start) failed: %s", e)

    finally:
        await conn.close()

    # ---- Stage 2: download + re-OCR (outside the DB connection) ----
    try:
        pdf_body = await _download_from_s3(bronze_key)
    except Exception as exc:
        log.exception("re_ocr_page S3 fetch failed report=%s page=%s", report_id, page)
        return ReOcrPageOutput(
            success=False,
            error=f"s3_fetch_failed: {type(exc).__name__}",
            retry_attempt=retry_attempt,
        )

    fd, tmp_name = tempfile.mkstemp(suffix=".pdf", prefix="re_ocr_")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        tmp_path.write_bytes(pdf_body)
        try:
            parse_result = await parse_scanned(
                tmp_path,
                pages=[page],
                settings=settings,
            )
        except Exception as exc:
            log.exception("re_ocr_page parse_scanned failed")
            return ReOcrPageOutput(
                success=False,
                error=f"parse_scanned_failed: {type(exc).__name__}",
                retry_attempt=retry_attempt,
            )
    finally:
        try:
            tmp_path.unlink()
        except Exception:
            pass

    new_ocr_confidence = (
        parse_result["per_page_ocr_confidence"][0]
        if parse_result.get("per_page_ocr_confidence")
        else 0.0
    )
    new_text_line_count = (
        parse_result["per_page_text_line_counts"][0]
        if parse_result.get("per_page_text_line_counts")
        else 0
    )

    # ---- Stage 3: persist new rows ----
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        await bind_workspace_scope(
            conn, workspace_id=workspace_id, site="hatchet.re_ocr_page"
        )
        async with conn.transaction():
            # 3a. parser_run_artifacts row for this retry pass
            await conn.execute(
                """
                INSERT INTO silver.parser_run_artifacts (
                    report_id, workspace_id, parser_used, parser_version,
                    raw_output_uri, errors, warnings, started_at, finished_at
                ) VALUES ($1::uuid, $2::uuid, 'scanned_paddleocr', $3,
                          $4, '[]'::jsonb, '[]'::jsonb, NOW(), NOW())
                """,
                report_id, workspace_id,
                f"retry_attempt_{retry_attempt}",
                bronze_key,
            )

            # 3b. ingest_ocr_results rows — one per OCR region from the retry
            source_method = {
                0: "paddleocr_pp_ocrv5_retry_binarized",
                1: "paddleocr_pp_ocrv5_retry_lang_hint",
            }.get(retry_attempt, "paddleocr_pp_ocrv5")

            # Region IDs must not collide with existing rows. Get the
            # current max region for this (report_id, page) and offset.
            max_existing_region = await conn.fetchval(
                """
                SELECT COALESCE(MAX(region), -1)
                FROM silver.ingest_ocr_results
                WHERE report_id = $1::uuid AND page = $2
                """,
                report_id, page,
            )
            next_region = int(max_existing_region) + 1

            for offset, passage in enumerate(parse_result.get("passages", [])):
                region = next_region + offset
                await conn.execute(
                    """
                    INSERT INTO silver.ingest_ocr_results (
                        report_id, page, region, workspace_id,
                        bbox, source_method, extraction_confidence,
                        ocr_text, char_confidences, payload
                    ) VALUES ($1::uuid, $2, $3, $4::uuid,
                              $5::numeric[], $6, $7, $8, '[]'::jsonb, $9::jsonb)
                    """,
                    report_id, page, region, workspace_id,
                    passage.get("bbox", []),
                    source_method,
                    passage.get("extraction_confidence"),
                    passage.get("text_content", ""),
                    f'{{"coord_origin": "TOPLEFT_IMAGE", "retry_attempt": {retry_attempt}}}',
                )

            # 3c. Update ocr_page_quality: bump retry_count, update confidence
            new_needs_review = new_ocr_confidence < 0.85  # quality_graph ACCEPT_OCR_CONFIDENCE
            await conn.execute(
                """
                UPDATE silver.ocr_page_quality
                SET retry_count = $1,
                    ocr_confidence = $2,
                    needs_review = $3,
                    last_evaluated_at = NOW()
                WHERE report_id = $4::uuid AND page = $5
                """,
                retry_attempt + 1,
                new_ocr_confidence,
                new_needs_review,
                report_id, page,
            )

        # Emit complete-of-run audit
        try:
            await emit_audit(
                conn,
                action_type="re_ocr_page.complete",
                workspace_id=input.workspace_id,
                actor_id=input.actor_id,
                actor_kind="workflow",
                target_schema="silver",
                target_table="ocr_page_quality",
                target_id=f"{report_id}:{page}",
                payload={
                    "report_id": report_id,
                    "page": page,
                    "retry_attempt": retry_attempt,
                    "new_ocr_confidence": new_ocr_confidence,
                    "new_text_line_count": new_text_line_count,
                    "still_needs_review": new_needs_review,
                },
                trace_id=ctx.workflow_run_id,
            )
        except Exception as e:
            log.warning("re_ocr_page audit emit (complete) failed: %s", e)
    finally:
        await conn.close()

    log.info(
        "re_ocr_page ok report=%s page=%s attempt=%s conf=%.3f lines=%d needs_review=%s",
        report_id, page, retry_attempt, new_ocr_confidence,
        new_text_line_count, new_needs_review,
    )

    return ReOcrPageOutput(
        success=True,
        new_ocr_confidence=new_ocr_confidence,
        new_text_line_count=new_text_line_count,
        retry_attempt=retry_attempt,
    )


__all__ = ["re_ocr_page", "ReOcrPageInput", "ReOcrPageOutput"]
