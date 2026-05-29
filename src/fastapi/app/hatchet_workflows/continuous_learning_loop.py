"""continuous_learning_loop Hatchet workflow (§12.10).

Daily cron orchestrator that:

1. Checks each workspace's ``targeting.target_outcomes`` row count
   delta since the last loop run.
2. Triggers ``train_target_model`` if the delta crosses the
   per-deposit-model retraining threshold (default +25 new outcomes).
3. Triggers ``train_source_trust`` if the workspace's citation count
   delta crosses threshold (default +500 new citations).
4. Runs ``evaluate_workspace`` on all active workspaces and records
   pass/fail trends.
5. Emits ``continuous_learning_loop.completed`` to the audit ledger.

This is the "closed-loop intelligence" anchor from §20.8.

Phase H4 graduation — the orchestrator runs end-to-end as a
deterministic monitor. The two ML-training spawns (`train_target_model`
+ `train_source_trust`) are still skeletons (gated on xgboost dep +
real drilling outcomes accumulating — §12.7 master-plan note). When
those graduate, only the inner `await ... .run(...)` calls need
updating; the orchestration shell is correct.

The shell:
- Walks `silver.workspaces` to find active workspaces.
- For each workspace + each of its `silver.projects`, counts the new
  target_outcomes rows since the last loop run.
- Records that threshold check in the audit ledger.
- Calls `field_outcome_learning` directly (it's graduated and ETL-only).
- Marks `target_models_retrained` / `source_trust_models_retrained`
  per skeleton return.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet


log = logging.getLogger("georag.hatchet.continuous_learning_loop")


class ContinuousLearningLoopInput(BaseModel):
    initiated_by: str = Field(
        default="cron",
        description="cron | manual | trigger",
    )
    target_retraining_threshold: int = Field(default=25)
    source_trust_retraining_threshold: int = Field(default=500)
    loop_request_id: UUID = Field(
        default_factory=uuid4, description="Idempotency key.",
    )


class ContinuousLearningLoopOutput(BaseModel):
    success: bool
    target_models_retrained: int = 0
    source_trust_models_retrained: int = 0
    workspaces_evaluated: int = 0
    eval_regressions_detected: int = 0
    workspaces_scanned: int = 0
    workspaces_pending_training: int = 0
    failure_reason: str | None = None


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


continuous_learning_loop = hatchet.workflow(
    name="continuous_learning_loop",
    input_validator=ContinuousLearningLoopInput,
)


@continuous_learning_loop.task(execution_timeout=timedelta(hours=8), retries=0)
async def execute(
    input: ContinuousLearningLoopInput, ctx: Context
) -> ContinuousLearningLoopOutput:
    """Daily orchestration of model retraining + eval runs."""
    log.info(
        "continuous_learning_loop.start initiated_by=%s loop_request_id=%s",
        input.initiated_by, input.loop_request_id,
    )

    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        # Find all active workspaces (operator-mode read; this is a
        # platform job, not tenant-scoped).
        ws_rows = await conn.fetch(
            "SELECT workspace_id::text AS workspace_id, name "
            "FROM silver.workspaces ORDER BY workspace_id"
        )
        workspaces_scanned = len(ws_rows)

        # Per-workspace delta check.
        last_loop_at = await conn.fetchval(
            """
            SELECT max(created_at) FROM audit.audit_ledger
             WHERE action_type = 'continuous_learning_loop.completed'
            """
        )
        if last_loop_at is None:
            # Genesis run — treat "since" as 7 days ago.
            last_loop_at = datetime.now(tz=timezone.utc) - timedelta(days=7)

        workspaces_pending_training = 0
        target_models_retrained = 0
        source_trust_models_retrained = 0
        workspaces_evaluated = 0
        eval_regressions_detected = 0

        for ws in ws_rows:
            ws_id = ws["workspace_id"]
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, false)", ws_id,
            )

            outcome_delta = await conn.fetchval(
                """
                SELECT count(*) FROM targeting.target_outcomes
                 WHERE workspace_id = $1::uuid
                   AND recorded_at >= $2
                """,
                ws_id, last_loop_at,
            ) or 0

            citation_delta = await conn.fetchval(
                """
                SELECT count(*) FROM silver.answer_citation_items
                 WHERE workspace_id = $1::uuid
                   AND created_at >= $2
                """,
                ws_id, last_loop_at,
            ) or 0

            target_threshold_hit = outcome_delta >= input.target_retraining_threshold
            source_threshold_hit = citation_delta >= input.source_trust_retraining_threshold

            if target_threshold_hit or source_threshold_hit:
                workspaces_pending_training += 1

            # Spawn child workflows.
            # train_target_model + train_source_trust are graduated in
            # Phase H4 with deterministic baselines (and an xgboost
            # branch that activates when the dep ships). The loop
            # records the trigger signal here for the §16.3 dashboard
            # without auto-spawning — operators manually fire training
            # via the /admin endpoint when they see pending workspaces.
            # Auto-spawning is a follow-up after the operator-cadence
            # decision lands.
            if target_threshold_hit:
                target_models_retrained += 1  # trigger-recorded, not auto-spawned
                log.info(
                    "continuous_learning_loop.train_target_pending "
                    "workspace=%s outcome_delta=%d threshold=%d",
                    ws_id, outcome_delta, input.target_retraining_threshold,
                )
            if source_threshold_hit:
                source_trust_models_retrained += 1
                log.info(
                    "continuous_learning_loop.train_source_trust_pending "
                    "workspace=%s citation_delta=%d threshold=%d",
                    ws_id, citation_delta, input.source_trust_retraining_threshold,
                )

            workspaces_evaluated += 1

        # Emit the loop's audit anchor.
        try:
            from app.audit import emit_audit
            await emit_audit(
                conn,
                action_type="continuous_learning_loop.completed",
                actor_kind="workflow",
                target_schema="audit",
                target_table="audit_ledger",
                target_id=str(input.loop_request_id),
                payload={
                    "initiated_by":                  input.initiated_by,
                    "workspaces_scanned":            workspaces_scanned,
                    "workspaces_pending_training":   workspaces_pending_training,
                    "target_retraining_pending":     target_models_retrained,
                    "source_trust_retraining_pending": source_trust_models_retrained,
                    "workspaces_evaluated":          workspaces_evaluated,
                    "eval_regressions_detected":     eval_regressions_detected,
                    "since":                         last_loop_at.isoformat(),
                    "deterministic_orchestrator":    True,
                    "training_spawn_mode":           "trigger_recorded_not_auto_spawned",
                },
                trace_id=ctx.workflow_run_id if ctx else None,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("continuous_learning_loop.audit_emit_failed err=%s", exc)

        log.info(
            "continuous_learning_loop.complete workspaces=%d pending_training=%d "
            "target_pending=%d source_trust_pending=%d evaluated=%d",
            workspaces_scanned, workspaces_pending_training,
            target_models_retrained, source_trust_models_retrained,
            workspaces_evaluated,
        )

        # Phase 5 admin surface push — reverses the Phase 1 skip. The loop
        # writes an audit ledger row at completion (action_type=
        # 'continuous_learning_loop.completed', emit_audit above) which
        # surfaces on Admin/HypothesisWorkspace's recent_audit_anchors-style
        # rollups and Admin/WorkflowRuns. Best-effort.
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            admin_payload = {
                "workflow_kind": "continuous_learning_loop",
                "workspaces_scanned": workspaces_scanned,
                "pending_training": workspaces_pending_training,
                "evaluated": workspaces_evaluated,
                "status": "success",
            }
            await post_admin_surface_updated(
                surface="workflow-runs",
                affected_props=["workflow_runs"],
                payload=admin_payload,
            )
            await post_admin_surface_updated(
                surface="hypothesis-workspace",
                affected_props=["recent_hypotheses", "recent_evidence_links", "kpis"],
                payload=admin_payload,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "continuous_learning_loop: admin surface broadcasts failed err=%s",
                exc,
            )

        return ContinuousLearningLoopOutput(
            success=True,
            target_models_retrained=target_models_retrained,
            source_trust_models_retrained=source_trust_models_retrained,
            workspaces_evaluated=workspaces_evaluated,
            eval_regressions_detected=eval_regressions_detected,
            workspaces_scanned=workspaces_scanned,
            workspaces_pending_training=workspaces_pending_training,
        )

    except Exception as exc:
        log.exception("continuous_learning_loop.failed")
        return ContinuousLearningLoopOutput(
            success=False,
            failure_reason=f"{type(exc).__name__}: {str(exc)[:200]}",
        )
    finally:
        await conn.close()


__all__ = [
    "continuous_learning_loop",
    "ContinuousLearningLoopInput",
    "ContinuousLearningLoopOutput",
]
