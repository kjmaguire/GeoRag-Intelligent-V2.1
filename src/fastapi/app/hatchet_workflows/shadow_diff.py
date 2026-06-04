"""Phase 1 Step 5B — ``ai:shadow_diff`` workflow.

Pairs the v1.49 + Hatchet sides of a shadow_runs row, applies the locked
diff contract (``app.services.shadow_diff.classifier``), and writes the
classification + diff_details back to the row.

Two surfaces:

  1. ``shadow_diff`` — single-row classifier. Input: ``{shadow_runs_id}``.
     Looks up the row, classifies, UPDATEs the row, emits audit.
     Idempotent: re-running on a row already classified clean/minor/
     divergent/fatal is a no-op (early return).

  2. ``shadow_diff_scan`` — cron-driven sweeper. Every minute, finds
     ``classification='partial'`` rows where BOTH ``v149_result`` and
     ``hatchet_result`` are populated (i.e. both sides have landed) and
     enqueues the per-row classifier. Runs on the AI worker pool.

Pool: ``ai``. Action prefix: ``ai:shadow_diff``.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any
from uuid import UUID

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.db import bind_workspace_scope
from app.hatchet_workflows import hatchet
from app.services.shadow_diff import classify_shadow_run


log = logging.getLogger("georag.hatchet.shadow_diff")


# =============================================================================
# IO models
# =============================================================================
class ShadowDiffInput(BaseModel):
    shadow_runs_id: UUID = Field(..., description="silver.shadow_runs.id to classify.")


class ShadowDiffFinalOut(BaseModel):
    shadow_runs_id: str
    classification: str
    duration_ms: int
    skipped: bool = False
    reason: str | None = None


class ShadowDiffScanInput(BaseModel):
    """Cron input — no parameters, but Hatchet wants a typed model."""

    fired_at: str | None = None


class ShadowDiffScanOut(BaseModel):
    enqueued: int
    candidates: int


# =============================================================================
# Helpers
# =============================================================================
def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _action_types_for_run(
    conn: asyncpg.Connection,
    *,
    workspace_id: UUID,
    run_id: UUID | str | None,
) -> set[str]:
    """Collect distinct audit_ledger.action_type values for one run."""
    if run_id is None:
        return set()
    rows = await conn.fetch(
        """
        SELECT DISTINCT action_type
        FROM audit.audit_ledger
        WHERE workspace_id = $1
          AND trace_id = $2::text
        """,
        workspace_id,
        str(run_id),
    )
    return {r["action_type"] for r in rows if r["action_type"]}


# =============================================================================
# 1. Per-row classifier workflow
# =============================================================================
shadow_diff = hatchet.workflow(
    name="shadow_diff",
    input_validator=ShadowDiffInput,
)


@shadow_diff.task(execution_timeout="60s", retries=2)
async def classify(input: ShadowDiffInput, ctx: Context) -> ShadowDiffFinalOut:
    t_start = time.monotonic()
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2, statement_cache_size=0)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, workspace_id, classification, minio_key,
                       v149_result, hatchet_result,
                       v149_duration_ms, hatchet_duration_ms,
                       v149_audit_run_id, hatchet_audit_run_id,
                       error_v149, error_hatchet
                FROM silver.shadow_runs
                WHERE id = $1
                """,
                input.shadow_runs_id,
            )
            if row is None:
                log.warning("shadow_diff: row not found id=%s", input.shadow_runs_id)
                return ShadowDiffFinalOut(
                    shadow_runs_id=str(input.shadow_runs_id),
                    classification="partial",
                    duration_ms=int((time.monotonic() - t_start) * 1000),
                    skipped=True,
                    reason="row_not_found",
                )

            if row["classification"] != "partial":
                # Already classified — don't re-classify (idempotent).
                return ShadowDiffFinalOut(
                    shadow_runs_id=str(row["id"]),
                    classification=row["classification"],
                    duration_ms=int((time.monotonic() - t_start) * 1000),
                    skipped=True,
                    reason="already_classified",
                )

            v149 = row["v149_result"]
            hatchet_res = row["hatchet_result"]
            err_v = row["error_v149"]
            err_h = row["error_hatchet"]

            # asyncpg returns jsonb as already-parsed dict OR str depending
            # on codec setup; defend against both.
            def _coerce(j: Any) -> Any:
                if j is None or isinstance(j, (dict, list)):
                    return j
                if isinstance(j, str):
                    try:
                        return json.loads(j)
                    except ValueError:
                        return None
                return j

            v149 = _coerce(v149)
            hatchet_res = _coerce(hatchet_res)

            # Skip if neither side errored AND either side missing —
            # the scanner should not have queued us in that case.
            both_missing = v149 is None and hatchet_res is None
            one_missing = (v149 is None) ^ (hatchet_res is None)
            if both_missing or (one_missing and not (err_v or err_h)):
                return ShadowDiffFinalOut(
                    shadow_runs_id=str(row["id"]),
                    classification="partial",
                    duration_ms=int((time.monotonic() - t_start) * 1000),
                    skipped=True,
                    reason="incomplete_pair",
                )

            v_actions = await _action_types_for_run(
                conn,
                workspace_id=row["workspace_id"],
                run_id=row["v149_audit_run_id"],
            )
            h_actions = await _action_types_for_run(
                conn,
                workspace_id=row["workspace_id"],
                run_id=row["hatchet_audit_run_id"],
            )

            outcome = classify_shadow_run(
                v149=v149,
                hatchet=hatchet_res,
                v149_audit_action_types=v_actions,
                hatchet_audit_action_types=h_actions,
                v149_duration_ms=row["v149_duration_ms"],
                hatchet_duration_ms=row["hatchet_duration_ms"],
                v149_error=err_v,
                hatchet_error=err_h,
            )

            duration_ms = int((time.monotonic() - t_start) * 1000)

            async with conn.transaction():
                await bind_workspace_scope(
                    conn, workspace_id=str(row["workspace_id"]), site="hatchet.shadow_diff"
                )
                await conn.execute(
                    """
                    UPDATE silver.shadow_runs
                       SET classification = $2,
                           diff_details   = $3::jsonb,
                           completed_at   = COALESCE(completed_at, now())
                     WHERE id = $1
                    """,
                    row["id"],
                    outcome.classification,
                    json.dumps(outcome.details, default=str),
                )

                try:
                    await emit_audit(
                        conn,
                        action_type="ingest_pdf.shadow.classified",
                        workspace_id=row["workspace_id"],
                        actor_id=None,
                        actor_kind="workflow",
                        target_schema="silver",
                        target_table="shadow_runs",
                        target_id=str(row["id"]),
                        payload={
                            "classification": outcome.classification,
                            "minio_key": row["minio_key"],
                            "v149_audit_run_id": str(row["v149_audit_run_id"])
                                if row["v149_audit_run_id"] else None,
                            "hatchet_audit_run_id": str(row["hatchet_audit_run_id"])
                                if row["hatchet_audit_run_id"] else None,
                            "duration_ms": duration_ms,
                        },
                        trace_id=ctx.workflow_run_id,
                    )
                except Exception as e:
                    log.warning("shadow_diff audit emit failed: %s", e)

            log.info(
                "shadow_diff classified id=%s classification=%s duration_ms=%d",
                row["id"], outcome.classification, duration_ms,
            )
            return ShadowDiffFinalOut(
                shadow_runs_id=str(row["id"]),
                classification=outcome.classification,
                duration_ms=duration_ms,
            )
    finally:
        await pool.close()


# =============================================================================
# 2. Cron scanner — every minute
# =============================================================================
SCAN_BATCH = int(os.environ.get("SHADOW_DIFF_SCAN_BATCH", "50"))


shadow_diff_scan = hatchet.workflow(
    name="shadow_diff_scan",
    input_validator=ShadowDiffScanInput,
    on_crons=["* * * * *"],
)


@shadow_diff_scan.task(execution_timeout="2m", retries=1)
async def scan(input: ShadowDiffScanInput, ctx: Context) -> ShadowDiffScanOut:
    """Find partial shadow_runs with both sides landed; trigger ai:shadow_diff each.

    Also handles ``one-sided + error`` cases (e.g. the Hatchet path errored
    so error_hatchet is set; we can still classify as 'fatal'). Older than
    24h with one side still missing ⇒ classify as 'fatal' too (timeout).
    """
    pool = await asyncpg.create_pool(_dsn(), min_size=1, max_size=2, statement_cache_size=0)
    enqueued = 0
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"""
                SELECT id
                FROM silver.shadow_runs
                WHERE workflow_kind = 'ingest_pdf'
                  AND classification = 'partial'
                  AND (
                        (v149_result IS NOT NULL AND hatchet_result IS NOT NULL)
                     OR (error_v149   IS NOT NULL OR  error_hatchet IS NOT NULL)
                     OR  started_at < now() - interval '24 hours'
                  )
                ORDER BY started_at ASC
                LIMIT {SCAN_BATCH}
                """
            )
            candidates = len(rows)
            for r in rows:
                try:
                    await shadow_diff.aio_run_no_wait(
                        ShadowDiffInput(shadow_runs_id=r["id"])
                    )
                    enqueued += 1
                except Exception as e:
                    log.warning("shadow_diff_scan: failed to enqueue id=%s: %s",
                                r["id"], e)
        return ShadowDiffScanOut(enqueued=enqueued, candidates=candidates)
    finally:
        await pool.close()


__all__ = [
    "shadow_diff",
    "shadow_diff_scan",
    "ShadowDiffInput",
    "ShadowDiffFinalOut",
    "ShadowDiffScanInput",
    "ShadowDiffScanOut",
]
