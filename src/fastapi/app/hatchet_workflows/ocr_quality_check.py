"""Phase 6 (2026-05-22) — OCR Quality Agent workflow.

Dispatched from ingest_pdf.persist after passage rows commit. For each
passage on the parsed report:
  1. Skip if ocr_confidence is NULL (text-layer extraction; no OCR
     quality to evaluate).
  2. Below OCR_REOCR_THRESHOLD (default 0.60) OR matches a known OCR
     artifact pattern → mark ocr_status='pending_reocr' and dispatch
     re_ocr_page for the passage's page_first.
  3. Below OCR_QUALITY_THRESHOLD (default 0.75) but doesn't qualify
     for re-OCR (no artifact + above re-OCR floor) → mark
     ocr_status='low_confidence' (informational).
  4. Cap: if > OCR_MAX_REOCR_PAGES_PER_DOC (default 20) pages flagged
     for re-OCR, dispatch the first N and emit a single
     silver.review_queue row asking a human to look at the rest.

Lightweight — no LLM calls. Pattern matching only. Designed to run
inline in the worker pool right after persist (a few ms of DB +
regex work per passage; re-OCR + review-queue inserts are the actual
expensive work, both async-fire-and-forget).

Gated on OCR_QUALITY_AGENT_ENABLED env (default false). Phase 6
ships disabled so the wiring lands without changing default behavior.
Flip after smoke-testing the heuristics on a real low-quality PDF.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any
from uuid import UUID

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet


log = logging.getLogger("georag.hatchet.ocr_quality_check")


# =============================================================================
# Input + output models
# =============================================================================
class OcrQualityCheckInput(BaseModel):
    """Dispatched from ingest_pdf.persist once a report's passages commit."""

    workspace_id: UUID
    project_id: UUID
    report_id: UUID
    actor_id: int | None = Field(
        default=None,
        description="Optional uploader id for audit-trail cross-reference.",
    )


class OcrQualityCheckOutput(BaseModel):
    ok: bool = True
    skipped: bool = False
    reason: str | None = None
    passages_evaluated: int = 0
    flagged_low_confidence: int = 0
    flagged_pending_reocr: int = 0
    reocr_dispatched: int = 0
    review_queue_created: bool = False


ocr_quality_check_wf = hatchet.workflow(
    name="ocr_quality_check",
    input_validator=OcrQualityCheckInput,
)


# =============================================================================
# Heuristic helpers — pure functions, isolated for test coverage
# =============================================================================

# Letter/digit confusion: a stray 'l' or 'I' adjacent to digits, or 'O' for '0'
# in a numeric context. Common Tesseract failure mode on degraded scans.
_LETTER_DIGIT_CONFUSION_RE = re.compile(
    r"(?:\b[lI]\d+|\d+[lI]\d*|\bO\d+|\d+O\d+)"
)

# Broken decimals: a digit, whitespace, more digits, then a known unit. Real
# values are "1.23 g/t", broken OCR reads "1 23 g/t".
_BROKEN_DECIMAL_RE = re.compile(
    # \b at the trailing edge bites on '%' (non-word char) — drop the
    # trailing boundary and rely on optional whitespace + the unit set
    # being specific enough to not produce false positives.
    r"\b\d+\s+\d+\s*(?:g/t|%|ppm|ppb|oz/t|kg/t)",
    re.IGNORECASE,
)

# Missing unit: a numeric pattern that looks like grade/depth/interval but
# has no unit within 8 chars. Loose enough to catch real cases without
# over-firing on plain numbers.
_NUMERIC_NEAR_GEOLOGICAL_RE = re.compile(
    r"\b\d+(?:\.\d+)?\s*(?:g/t|%|ppm|ppb|oz/t|kg/t|m\b|cm\b|ft\b)",
    re.IGNORECASE,
)


def _has_letter_digit_confusion(text: str) -> bool:
    """True when text contains a letter/digit confusion pattern."""
    if not text:
        return False
    return bool(_LETTER_DIGIT_CONFUSION_RE.search(text))


def _has_broken_decimal(text: str) -> bool:
    """True when text has a numeric-space-numeric-unit pattern (broken decimal)."""
    if not text:
        return False
    return bool(_BROKEN_DECIMAL_RE.search(text))


def _is_garbage(text: str, confidence: float | None) -> bool:
    """True for sub-threshold OCR confidence on very short text — likely
    garbage glyphs that the model couldn't read."""
    if confidence is None:
        return False
    try:
        thr = float(os.environ.get("OCR_REOCR_THRESHOLD", "0.60"))
    except ValueError:
        thr = 0.60
    return confidence < thr and len(text.strip()) < 50


def _has_artifact(text: str, confidence: float | None) -> bool:
    """Aggregate artifact check — any of the heuristics triggering."""
    return (
        _has_letter_digit_confusion(text)
        or _has_broken_decimal(text)
        or _is_garbage(text, confidence)
    )


# =============================================================================
# Workflow body
# =============================================================================
def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


@ocr_quality_check_wf.task(execution_timeout="3m", retries=1)
async def run(
    input: OcrQualityCheckInput, ctx: Context,
) -> OcrQualityCheckOutput:
    """Evaluate every passage of the report for OCR quality issues.

    Returns an OcrQualityCheckOutput summarising what was flagged and
    what dispatches fired. Does not block on the dispatched workflows
    (they're aio_run_no_wait fire-and-forget).
    """
    enabled = os.environ.get("OCR_QUALITY_AGENT_ENABLED", "false").lower() == "true"
    if not enabled:
        return OcrQualityCheckOutput(
            ok=True, skipped=True, reason="OCR_QUALITY_AGENT_ENABLED=false",
        )

    try:
        quality_thr = float(os.environ.get("OCR_QUALITY_THRESHOLD", "0.75"))
    except ValueError:
        quality_thr = 0.75
    try:
        reocr_thr = float(os.environ.get("OCR_REOCR_THRESHOLD", "0.60"))
    except ValueError:
        reocr_thr = 0.60
    try:
        max_reocr = int(os.environ.get("OCR_MAX_REOCR_PAGES_PER_DOC", "20"))
    except ValueError:
        max_reocr = 20

    pool = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=2, statement_cache_size=0,
    )
    try:
        async with pool.acquire() as conn:
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, true)",
                str(input.workspace_id),
            )
            rows = await conn.fetch(
                """
                SELECT passage_id::text AS passage_id,
                       text,
                       ocr_confidence,
                       ocr_method,
                       page_first,
                       ocr_status
                  FROM silver.document_passages
                 WHERE document_id = $1::uuid
                   AND ocr_confidence IS NOT NULL
                """,
                str(input.report_id),
            )

            flagged_low_conf: list[str] = []         # ocr_status = 'low_confidence'
            flagged_pending_reocr: list[str] = []    # ocr_status = 'pending_reocr'
            page_set_for_reocr: dict[int, str] = {}  # page → first passage_id for that page

            for row in rows:
                text = row["text"] or ""
                conf = float(row["ocr_confidence"])
                page = row["page_first"]

                if conf < reocr_thr or _has_artifact(text, conf):
                    flagged_pending_reocr.append(row["passage_id"])
                    if page is not None and page not in page_set_for_reocr:
                        page_set_for_reocr[page] = row["passage_id"]
                elif conf < quality_thr:
                    flagged_low_conf.append(row["passage_id"])

            # Apply ocr_status updates in two batches
            if flagged_low_conf:
                await conn.execute(
                    """
                    UPDATE silver.document_passages
                       SET ocr_status = 'low_confidence', updated_at = NOW()
                     WHERE passage_id = ANY($1::uuid[])
                       AND ocr_status = 'accepted'
                    """,
                    flagged_low_conf,
                )
            if flagged_pending_reocr:
                await conn.execute(
                    """
                    UPDATE silver.document_passages
                       SET ocr_status = 'pending_reocr', updated_at = NOW()
                     WHERE passage_id = ANY($1::uuid[])
                       AND ocr_status = 'accepted'
                    """,
                    flagged_pending_reocr,
                )

            # Cap re-OCR dispatch to keep cost bounded
            pages_to_reocr = list(page_set_for_reocr.keys())
            dispatched = 0
            over_cap = len(pages_to_reocr) > max_reocr
            if over_cap:
                pages_to_reocr = pages_to_reocr[:max_reocr]

            # Fire re-OCR for each unique page (de-duped above)
            try:
                from app.hatchet_workflows.re_ocr_page import (
                    re_ocr_page,
                    ReOcrPageInput,
                )
                for page in pages_to_reocr:
                    try:
                        await re_ocr_page.aio_run_no_wait(
                            ReOcrPageInput(
                                workspace_id=input.workspace_id,
                                report_id=input.report_id,
                                page=page,
                                actor_id=input.actor_id,
                            )
                        )
                        dispatched += 1
                    except Exception as exc:
                        log.warning(
                            "ocr_quality_check: re_ocr_page dispatch failed "
                            "page=%d (%s) — passage stays pending_reocr",
                            page, exc,
                        )
            except ImportError as exc:
                log.warning(
                    "ocr_quality_check: re_ocr_page workflow not importable "
                    "(%s) — pending_reocr flag set, manual reset required",
                    exc,
                )

            # Review-queue insert when over cap (informational; one row per doc)
            review_queue_created = False
            if over_cap:
                try:
                    bronze_uri = await conn.fetchval(
                        "SELECT source_file_sha256 FROM silver.reports WHERE report_id = $1::uuid",
                        str(input.report_id),
                    )
                    payload = {
                        "report_id": str(input.report_id),
                        "flagged_pages_total": len(page_set_for_reocr),
                        "reocr_dispatched": dispatched,
                        "flagged_pages_over_cap": list(page_set_for_reocr.keys())[max_reocr:],
                        "quality_threshold": quality_thr,
                        "reocr_threshold": reocr_thr,
                        "max_reocr_pages_per_doc": max_reocr,
                    }
                    await conn.execute(
                        """
                        INSERT INTO silver.review_queue (
                            queue_id, workspace_id, project_id,
                            target_table, target_record_kind,
                            bronze_uri, payload, confidence_record,
                            parser_version, routing_decision, routing_reason,
                            lifecycle, created_at, updated_at
                        ) VALUES (
                            $1::uuid, $2::uuid, $3::uuid,
                            'silver.document_passages', 'ocr_quality_review',
                            $4, $5::jsonb, $6::numeric,
                            $7, 'review_required', $8,
                            'pending', NOW(), NOW()
                        )
                        """,
                        str(uuid.uuid4()),
                        str(input.workspace_id),
                        str(input.project_id),
                        f"bronze:{bronze_uri}" if bronze_uri else None,
                        json.dumps(payload),
                        # confidence_record at the doc level — use the
                        # threshold as a stable scalar so the queue can
                        # sort/filter sensibly.
                        reocr_thr,
                        "ocr_quality_agent_phase6",
                        f"{len(page_set_for_reocr)} pages flagged > cap {max_reocr}",
                    )
                    review_queue_created = True
                    log.info(
                        "ocr_quality_check: review_queue row created for report=%s "
                        "(flagged=%d > cap=%d)",
                        input.report_id, len(page_set_for_reocr), max_reocr,
                    )
                except Exception as exc:
                    log.warning(
                        "ocr_quality_check: review_queue insert failed: %s — "
                        "re-OCR dispatches still fired", exc,
                    )

            return OcrQualityCheckOutput(
                ok=True,
                passages_evaluated=len(rows),
                flagged_low_confidence=len(flagged_low_conf),
                flagged_pending_reocr=len(flagged_pending_reocr),
                reocr_dispatched=dispatched,
                review_queue_created=review_queue_created,
            )
    finally:
        await pool.close()
