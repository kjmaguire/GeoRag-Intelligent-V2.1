"""what_changed_detector Hatchet workflow (§9.13 / §21.7).

Doc-phase 94 skeleton → doc-phase 147 graduation.

Delta-detection workflow that surfaces what changed in a workspace
within a time window by scanning the audit ledger + silver tables.

Counts produced:
  - new_ingestion_count        — ingest_pdf.* audit anchors in window
  - new_public_record_count    — public_geo.pull.complete audits
  - updated_public_record_count — public_geo.pull.updated audits
  - new_claim_count            — silver.claim_ledger inserts in window
                                  (synthetic stub — table may not exist yet)
  - target_score_shift_count   — synthetic stub (deposits when the
                                  §18 scoring workflow graduates with
                                  delta detection)

Output feeds into the §7.2 `what_changed` report template.

What's live:
  - Audit-anchor counts from audit.audit_ledger (real data)
  - Hypothesis / decision / support counts in window (bonus visibility)
  - Audit anchor emission (`workspace.what_changed.detected`)
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from uuid import UUID

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel

from app.audit import emit_audit
from app.db import bind_workspace_scope
from app.hatchet_workflows import hatchet

log = logging.getLogger("georag.hatchet.what_changed_detector")


# =============================================================================
# IO models
# =============================================================================
class WhatChangedInput(BaseModel):
    workspace_id: UUID
    project_id: UUID | None = None
    window_start: datetime
    window_end: datetime
    detect_request_id: UUID  # idempotency key


class WhatChangedOutput(BaseModel):
    success: bool
    new_ingestion_count: int = 0
    new_public_record_count: int = 0
    updated_public_record_count: int = 0
    new_claim_count: int = 0
    target_score_shift_count: int = 0
    report_request_id: UUID | None = None
    error: str | None = None
    # Doc-phase 147 — bonus delta signals for observability.
    new_decision_count: int = 0
    new_hypothesis_count: int = 0
    new_support_ticket_count: int = 0
    total_audit_anchors_in_window: int = 0


# =============================================================================
# Workflow registration
# =============================================================================
what_changed_detector = hatchet.workflow(
    name="what_changed_detector",
    input_validator=WhatChangedInput,
)


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _count_audits(
    conn: asyncpg.Connection,
    workspace_id: str,
    action_prefixes: list[str],
    window_start: datetime,
    window_end: datetime,
) -> int:
    """Count audit_ledger entries matching action_type LIKE any prefix."""
    if not action_prefixes:
        return 0
    patterns = [p + "%" for p in action_prefixes]
    return (
        await conn.fetchval(
            """
            SELECT count(*) FROM audit.audit_ledger
             WHERE workspace_id = $1::uuid
               AND created_at >= $2 AND created_at < $3
               AND action_type LIKE ANY($4::text[])
            """,
            workspace_id, window_start, window_end, patterns,
        )
        or 0
    )


@what_changed_detector.task(execution_timeout="30m", retries=1)
async def execute(input: WhatChangedInput, ctx: Context) -> WhatChangedOutput:
    """Detect deltas in the window. Graduated doc-phase 147.

    Reads audit.audit_ledger + silver.* counts in the window and
    emits a `workspace.what_changed.detected` audit anchor with the
    full delta summary.
    """
    workspace_str = str(input.workspace_id)
    log.info(
        "what_changed_detector.task_started workspace=%s window=%s..%s",
        workspace_str, input.window_start, input.window_end,
    )

    pool = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=2, statement_cache_size=0
    )
    try:
        async with pool.acquire() as conn:
            # Block-3 RLS: scope to the workspace we're detecting for.
            await bind_workspace_scope(
                conn, workspace_id=workspace_str, site="hatchet.what_changed_detector"
            )
            ingest = await _count_audits(
                conn, workspace_str, ["ingest_pdf.", "ingest.", "ocr."],
                input.window_start, input.window_end,
            )
            new_public = await _count_audits(
                conn, workspace_str, ["public_geo.pull.complete"],
                input.window_start, input.window_end,
            )
            updated_public = await _count_audits(
                conn, workspace_str, ["public_geo.pull.updated"],
                input.window_start, input.window_end,
            )

            # Bonus delta signals from silver/ops tables in window.
            decisions = (
                await conn.fetchval(
                    """
                    SELECT count(*) FROM silver.decision_records
                     WHERE workspace_id = $1::uuid
                       AND decided_at >= $2 AND decided_at < $3
                    """,
                    workspace_str, input.window_start, input.window_end,
                )
                or 0
            )
            hypotheses = (
                await conn.fetchval(
                    """
                    SELECT count(*) FROM silver.hypotheses
                     WHERE workspace_id = $1::uuid
                       AND created_at >= $2 AND created_at < $3
                    """,
                    workspace_str, input.window_start, input.window_end,
                )
                or 0
            )
            support_tickets = (
                await conn.fetchval(
                    """
                    SELECT count(*) FROM ops.support_tickets
                     WHERE workspace_id = $1::uuid
                       AND reported_at >= $2 AND reported_at < $3
                    """,
                    workspace_str, input.window_start, input.window_end,
                )
                or 0
            )
            total_audits = (
                await conn.fetchval(
                    """
                    SELECT count(*) FROM audit.audit_ledger
                     WHERE workspace_id = $1::uuid
                       AND created_at >= $2 AND created_at < $3
                    """,
                    workspace_str, input.window_start, input.window_end,
                )
                or 0
            )

            # Emit the rollup audit anchor.
            await emit_audit(
                conn,
                action_type="workspace.what_changed.detected",
                workspace_id=workspace_str,
                actor_kind="workflow",
                target_schema="audit",
                target_table="audit_ledger",
                target_id=str(input.detect_request_id),
                payload={
                    "evaluator": "what_changed_v1",
                    "doc_phase": 147,
                    "window_start": input.window_start.isoformat(),
                    "window_end": input.window_end.isoformat(),
                    "new_ingestion_count": ingest,
                    "new_public_record_count": new_public,
                    "updated_public_record_count": updated_public,
                    "new_decision_count": decisions,
                    "new_hypothesis_count": hypotheses,
                    "new_support_ticket_count": support_tickets,
                    "total_audit_anchors_in_window": total_audits,
                },
            )

        log.info(
            "what_changed_detector.task_completed workspace=%s "
            "ingest=%d public_new=%d public_updated=%d decisions=%d "
            "hypotheses=%d support=%d total_audits=%d",
            workspace_str, ingest, new_public, updated_public,
            decisions, hypotheses, support_tickets, total_audits,
        )

        # Phase 5 admin surface push — drives Admin/WhatChanged. The Foundry
        # per-project surface (Foundry/WhatChangedFeed) picks up the change
        # via WorkspaceDataUpdated's `what_changed` affected_type the next
        # time an ingest completes — what_changed runs are typically tied
        # to ingest events so the page sees the new digest within the same
        # debounce window.
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            admin_payload = {
                "workflow_kind": "what_changed_detector",
                "workspace_id": workspace_str,
                "ingest_count": ingest,
                "decisions": decisions,
                "hypotheses": hypotheses,
                "support_tickets": support_tickets,
                "status": "success",
            }
            await post_admin_surface_updated(
                surface="workflow-runs",
                affected_props=["workflow_runs"],
                payload=admin_payload,
            )
            await post_admin_surface_updated(
                surface="what-changed",
                affected_props=["runs"],
                payload=admin_payload,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "what_changed_detector: admin surface broadcasts failed "
                "workspace=%s err=%s", workspace_str, exc,
            )

        return WhatChangedOutput(
            success=True,
            new_ingestion_count=ingest,
            new_public_record_count=new_public,
            updated_public_record_count=updated_public,
            new_claim_count=0,  # silver.claim_ledger table not present yet
            target_score_shift_count=0,  # awaits §18 scoring delta detection
            new_decision_count=decisions,
            new_hypothesis_count=hypotheses,
            new_support_ticket_count=support_tickets,
            total_audit_anchors_in_window=total_audits,
        )
    finally:
        await pool.close()
