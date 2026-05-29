"""§12 — weekly what-changed digest fan-out (cron wrapper).

Wraps ``what_changed_detector`` with a "Monday-morning digest" cron:
once a week, iterate every active workspace, fire the detector for
the previous 7-day window, emit a roll-up audit anchor with the
per-workspace counts.

This is the "polish" graduation of the §9.13 / §21.7 surface: the
detector itself was graduated doc-phase 147, but it could only be
triggered by hand or by an API call. With this wrapper, operators
see weekly delta surfaces in the audit ledger without any manual
intervention.

Cron: ``0 6 * * 1`` UTC — Mondays at 06:00 (15min after the eval
nightly's 05:45 final slot, keeping the AI pool spread out).

Triggering manually:
  ``what_changed_weekly.aio_mock_run(WeeklyDigestInput())``
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.audit import emit_audit
from app.hatchet_workflows import hatchet
from app.hatchet_workflows.what_changed_detector import (
    WhatChangedInput, execute as what_changed_execute,
)

log = logging.getLogger("georag.hatchet.what_changed_weekly")


class WeeklyDigestInput(BaseModel):
    window_days: int = Field(
        default=7,
        ge=1, le=90,
        description="Look-back window. Defaults to 7 (Monday-to-Monday).",
    )
    # If None (cron path), use now() - window_days as the start. Operator
    # override lets you back-date a digest for a custom window.
    explicit_window_end: datetime | None = Field(
        default=None,
        description="Override window end (UTC). Cron uses now().",
    )


class PerWorkspaceDigest(BaseModel):
    workspace_id: str
    workspace_name: str | None = None
    detect_request_id: str
    new_ingestion_count: int = 0
    new_public_record_count: int = 0
    updated_public_record_count: int = 0
    new_decision_count: int = 0
    new_hypothesis_count: int = 0
    new_support_ticket_count: int = 0
    total_audit_anchors_in_window: int = 0
    error: str | None = None


class WeeklyDigestOutput(BaseModel):
    run_id: str
    window_start: datetime
    window_end: datetime
    workspace_count: int
    per_workspace: list[PerWorkspaceDigest]
    total_ingestion: int = 0
    total_audit_anchors: int = 0


what_changed_weekly = hatchet.workflow(
    name="what_changed_weekly",
    input_validator=WeeklyDigestInput,
    on_crons=["0 6 * * 1"],  # Mondays at 06:00 UTC
)


def _dsn() -> str:
    user = os.environ.get("POSTGRES_USER", "georag")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


async def _list_active_workspaces(
    conn: asyncpg.Connection,
) -> list[dict[str, Any]]:
    """Workspaces with at least one ingestion or audit row in the last 90 days
    are considered 'active' — we skip dormant workspaces to keep the digest
    focused. (Operator can still run the detector manually for any workspace.)
    """
    rows = await conn.fetch(
        """
        SELECT w.workspace_id::text AS id, w.name
          FROM silver.workspaces w
         WHERE EXISTS (
             SELECT 1 FROM audit.audit_ledger a
              WHERE a.workspace_id = w.workspace_id
                AND a.created_at >= now() - interval '90 days'
             LIMIT 1
         )
         ORDER BY w.name
        """,
    )
    return [{"id": r["id"], "name": r["name"]} for r in rows]


@what_changed_weekly.task(execution_timeout="60m", retries=1)
async def run_weekly(
    input: WeeklyDigestInput, ctx: Context,
) -> WeeklyDigestOutput:
    """Fan out what_changed_detector across every active workspace.

    On cron-fire (`input.explicit_window_end = None`), the window is
    `[now - window_days, now]`. Operators can pass an explicit window end
    to back-date the digest.
    """
    run_id = str(uuid4())
    window_end = input.explicit_window_end or datetime.now(tz=timezone.utc)
    window_start = window_end - timedelta(days=input.window_days)

    log.info(
        "what_changed_weekly.start run_id=%s window=%s..%s",
        run_id, window_start, window_end,
    )

    pool = await asyncpg.create_pool(
        _dsn(), min_size=1, max_size=4, statement_cache_size=0,
    )
    try:
        async with pool.acquire() as conn:
            workspaces = await _list_active_workspaces(conn)

        per_workspace: list[PerWorkspaceDigest] = []
        total_ingestion = 0
        total_audits = 0

        for ws in workspaces:
            detect_request_id = str(uuid4())
            try:
                # Invoke the detector inline — keeps the call shape
                # identical to the API path. Could also be an aio_run
                # against the workflow engine, but inline is cheaper
                # for a weekly fan-out (no per-call queueing overhead).
                detector_input = WhatChangedInput(
                    workspace_id=UUID(ws["id"]),
                    project_id=None,
                    window_start=window_start,
                    window_end=window_end,
                    detect_request_id=UUID(detect_request_id),
                )
                # Call the underlying coroutine directly — the detector
                # opens its own pool, so we don't share connections.
                detector_out = await what_changed_execute.aio_mock_run(detector_input)

                per_workspace.append(PerWorkspaceDigest(
                    workspace_id=ws["id"],
                    workspace_name=ws["name"],
                    detect_request_id=detect_request_id,
                    new_ingestion_count=detector_out.new_ingestion_count,
                    new_public_record_count=detector_out.new_public_record_count,
                    updated_public_record_count=detector_out.updated_public_record_count,
                    new_decision_count=detector_out.new_decision_count,
                    new_hypothesis_count=detector_out.new_hypothesis_count,
                    new_support_ticket_count=detector_out.new_support_ticket_count,
                    total_audit_anchors_in_window=detector_out.total_audit_anchors_in_window,
                ))
                total_ingestion += detector_out.new_ingestion_count
                total_audits += detector_out.total_audit_anchors_in_window
            except Exception as exc:  # noqa: BLE001
                log.exception(
                    "what_changed_weekly: detector failed for ws=%s: %s",
                    ws["id"], exc,
                )
                per_workspace.append(PerWorkspaceDigest(
                    workspace_id=ws["id"],
                    workspace_name=ws["name"],
                    detect_request_id=detect_request_id,
                    error=f"{type(exc).__name__}: {exc}",
                ))

        # Emit the rollup audit anchor (NULL workspace_id — system-wide).
        try:
            async with pool.acquire() as conn:
                await emit_audit(
                    conn,
                    action_type="workspace.what_changed.weekly_digest",
                    workspace_id=None,
                    actor_kind="workflow",
                    target_schema="audit",
                    target_table="audit_ledger",
                    target_id=run_id,
                    payload={
                        "evaluator":           "what_changed_weekly_v1",
                        "doc_phase":           182,
                        "run_id":              run_id,
                        "window_start":        window_start.isoformat(),
                        "window_end":          window_end.isoformat(),
                        "workspace_count":     len(workspaces),
                        "total_ingestion":     total_ingestion,
                        "total_audit_anchors": total_audits,
                        "errors":              sum(
                            1 for d in per_workspace if d.error
                        ),
                    },
                )
        except Exception:
            log.exception("what_changed_weekly: rollup audit emission failed")

        log.info(
            "what_changed_weekly.completed run_id=%s ws=%d ingest=%d audits=%d",
            run_id, len(workspaces), total_ingestion, total_audits,
        )

        # Phase 5 admin surface push — drives Admin/WhatChanged on the
        # weekly rollup cadence. Same shape as what_changed_detector.
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            admin_payload = {
                "workflow_kind": "what_changed_weekly",
                "run_id": str(run_id),
                "workspace_count": len(workspaces),
                "total_ingestion": total_ingestion,
                "total_audits": total_audits,
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
                "what_changed_weekly: admin surface broadcasts failed run_id=%s err=%s",
                run_id, exc,
            )

        return WeeklyDigestOutput(
            run_id=run_id,
            window_start=window_start,
            window_end=window_end,
            workspace_count=len(workspaces),
            per_workspace=per_workspace,
            total_ingestion=total_ingestion,
            total_audit_anchors=total_audits,
        )
    finally:
        await pool.close()


__all__ = [
    "what_changed_weekly",
    "WeeklyDigestInput",
    "WeeklyDigestOutput",
    "PerWorkspaceDigest",
]
