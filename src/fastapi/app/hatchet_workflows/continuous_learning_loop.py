"""continuous_learning_loop Hatchet workflow (§12.10).

Daily cron at 22:30 UTC. Until 2026-09-29 this docstring called it a
"daily cron orchestrator" while the workflow declared no ``on_crons`` at
all, so it registered and never fired (HAT-13). It is scheduled now because
it passes the bar Kyle set for unattended runs:

* It is cheap: two ``count(*)`` queries per workspace and one audit row.
* It never spends on Cohere. There is no LLM, embed, rerank or Parse call
  anywhere in the body, and it spawns nothing (see below).
* It is safe to repeat. The delta window starts at the previous run's
  audit anchor, so an extra manual run just narrows tomorrow's window.
* It accepts the empty input an engine cron sends; every field defaults.
* It fits the window. 22:30 UTC plus the 30-minute budget ends at 23:00,
  before the earliest nightly stop (00:00 UTC in PDT, 01:00 in PST).
  ``tests/test_crons_avoid_the_shutdown_window.py`` checks both the start
  and the end.

What each run does:

1. Counts each workspace's new ``targeting.target_outcomes`` rows since
   the last loop run.
2. Flags ``train_target_model`` as pending if that delta crosses the
   retraining threshold (default +25 new outcomes).
3. Flags ``train_source_trust`` as pending if the workspace's citation
   delta crosses its threshold (default +500 new citations).
4. Emits ``continuous_learning_loop.completed`` to the audit ledger.

It flags; it does not spawn. Training is started by an operator through
``/api/v1/admin/ml/``. It does not call ``field_outcome_learning`` either,
whatever an earlier version of this docstring said: that workflow is manual.

It does NOT evaluate answer quality, whatever this docstring said
for the month after the code stopped doing it. ``evaluate_workspace`` was deleted in
09d1d35 (2026-07-27) and nothing replaced the call. Two audit fields
outlived it -- ``workspaces_evaluated`` (always identical to
``workspaces_scanned``) and ``eval_regressions_detected`` (a constant
0 that could never rise) -- and were removed on 2026-08-22. Answer
quality is measured by ``answer_quality_watch`` against
silver.answer_runs; look there, not here.

This is the "closed-loop intelligence" anchor from §20.8.

Phase H4 graduation — the orchestrator runs end-to-end as a
deterministic monitor. Auto-spawning the two trainers is a separate
decision, and it has not been made. If it ever is, re-check the cron bar
above: a trainer that calls a hosted model would make this workflow spend
unattended.

The shell:
- Lists workspaces with ``list_workspace_ids`` (``silver.workspaces`` is
  readable unscoped by design; HAT-1).
- Counts each workspace's deltas in its own short transaction with that
  workspace's scope bound, so the fail-closed ``targeting`` and ``silver``
  policies admit the rows under the AWS worker role (``georag_app``,
  NOBYPASSRLS).
- Records the threshold check in the audit ledger (a system row, NULL
  workspace).
- Reports ``target_models_retrained`` / ``source_trust_models_retrained``
  as "pending" counts. Nothing is retrained here.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.db import bind_workspace_scope, list_workspace_ids
from app.db.dsn import build_dsn
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
    workspaces_scanned: int = 0
    workspaces_pending_training: int = 0
    failure_reason: str | None = None


# One DSN builder for the whole service — see app/db/dsn.py for why
# sixty copies of this existed and what the drift cost.
_dsn = build_dsn


continuous_learning_loop = hatchet.workflow(
    name="continuous_learning_loop",
    # HAT-13 (2026-09-29): scheduled at last. 22:30 UTC sits in the open
    # window after model_cost_summary_run (22:00) and, with the 30 min budget
    # below, ends at 23:00, before the earliest nightly stop (00:00 UTC, PDT).
    # A cron tick sends NO input, and every input field defaults, so {} is
    # a complete input.
    on_crons=["30 22 * * *"],
    input_validator=ContinuousLearningLoopInput,
)


#: Was 8 h, which the end-time check in test_crons_avoid_the_shutdown_window
#: would reject from any open-window start. The body is two counts per
#: workspace and one audit insert; 30 minutes is two orders of magnitude of
#: headroom and still ends before the stop.
@continuous_learning_loop.task(execution_timeout=timedelta(minutes=30), retries=0)
async def execute(
    input: ContinuousLearningLoopInput, ctx: Context
) -> ContinuousLearningLoopOutput:
    """Daily retraining-readiness check. Flags, never trains, never calls a model."""
    log.info(
        "continuous_learning_loop.start initiated_by=%s loop_request_id=%s",
        input.initiated_by, input.loop_request_id,
    )

    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        # Every workspace, listed with the scope cleared (HAT-1 helper; logs
        # WORKSPACE_ENUMERATION_EMPTY if the policy ever hides them all).
        ws_ids = await list_workspace_ids(conn, site="continuous_learning_loop")
        workspaces_scanned = len(ws_ids)

        # Per-workspace delta check.
        last_loop_at = await conn.fetchval(
            """
            SELECT max(created_at) FROM audit.audit_ledger
             WHERE action_type = 'continuous_learning_loop.completed'
            """
        )
        if last_loop_at is None:
            # Genesis run — treat "since" as 7 days ago.
            last_loop_at = datetime.now(tz=UTC) - timedelta(days=7)

        workspaces_pending_training = 0
        target_models_retrained = 0
        source_trust_models_retrained = 0

        for ws_id in ws_ids:
            # One short transaction per workspace with the scope bound
            # SET LOCAL, so it ends with the transaction. It used to be a
            # session-level bind that stayed on the connection after the
            # loop, which left the last workspace's scope in place for the
            # audit insert below.
            async with conn.transaction():
                await bind_workspace_scope(
                    conn, workspace_id=ws_id, site="continuous_learning_loop",
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
            "target_pending=%d source_trust_pending=%d",
            workspaces_scanned, workspaces_pending_training,
            target_models_retrained, source_trust_models_retrained,
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
            workspaces_scanned=workspaces_scanned,
            workspaces_pending_training=workspaces_pending_training,
        )

    except Exception:
        # Fail the run. This used to return success=False, which Hatchet records
        # as a COMPLETED task: the 22:30 cron showed green on a night the
        # retraining check had not run, and nothing else reports it. (The
        # `success` / `failure_reason` fields stay on the output for callers that
        # read them; a failure no longer reaches them.)
        log.exception("continuous_learning_loop.failed")
        raise
    finally:
        await conn.close()


__all__ = [
    "continuous_learning_loop",
    "ContinuousLearningLoopInput",
    "ContinuousLearningLoopOutput",
]
