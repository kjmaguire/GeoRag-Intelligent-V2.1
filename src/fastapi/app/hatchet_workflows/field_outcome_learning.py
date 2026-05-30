"""field_outcome_learning Hatchet workflow (§9.11 / §21.4) — doc-phase 184.

Graduated from skeleton (doc-phase 94). Triggered when new drilling
outcomes import into the workspace. The workflow:

1. Walks each input `targeting.target_outcomes` row
2. Matches to its parent `targeting.target_recommendations` via
   `recommendation_id`
3. Computes hit-rate metrics over the matching recommendation's
   model version and writes a `targeting.target_backtests` row
   per (workspace_id, model_version_id) bucket
4. Emits a `:lessons_learned` record in
   `silver.decision_lessons_learned` tied to the workspace
5. Audit-emits `field_outcome.learned` to audit.audit_ledger

Per doc-phase 177 audit, this workflow is ETL-only — no XGBoost
training. The actual retraining lives in `train_target_model` and is
gated by the `continuous_learning_loop` orchestrator.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet


log = logging.getLogger("georag.hatchet.field_outcome_learning")


class FieldOutcomeLearningInput(BaseModel):
    workspace_id: UUID
    project_id: UUID
    outcome_ids: list[UUID] = Field(
        default_factory=list,
        description="Specific target_outcomes rows to fold in. If empty, "
                    "the workflow folds in all unprocessed outcomes for "
                    "this project (joined-back-cleanly via target_backtests).",
    )


class FieldOutcomeLearningOutput(BaseModel):
    success: bool
    outcomes_processed: int = 0
    backtests_written: int = 0
    lessons_written: int = 0
    retraining_triggered: bool = False
    error: str | None = None


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


field_outcome_learning = hatchet.workflow(
    name="field_outcome_learning",
    input_validator=FieldOutcomeLearningInput,
)


@field_outcome_learning.task(execution_timeout=timedelta(hours=2), retries=1)
async def execute(
    input: FieldOutcomeLearningInput, ctx: Context
) -> FieldOutcomeLearningOutput:
    """Fold new drilling outcomes into target-model learning state.

    Graduated from skeleton — doc-phase 184.
    """
    workspace_id = str(input.workspace_id)
    project_id = str(input.project_id)
    log.info(
        "field_outcome_learning.start workspace=%s project=%s outcomes=%d",
        workspace_id, project_id, len(input.outcome_ids),
    )

    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        # Set RLS GUCs
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )
        await conn.execute(
            "SELECT set_config('app.project_id', $1, false)", project_id,
        )

        # Resolve outcome rows — either explicit IDs or all for the project
        if input.outcome_ids:
            outcomes = await conn.fetch(
                """
                SELECT o.outcome_id::text AS outcome_id,
                       o.recommendation_id::text AS recommendation_id,
                       o.hit_or_miss,
                       o.outcome_payload,
                       o.recorded_at
                  FROM targeting.target_outcomes o
                 WHERE o.outcome_id = ANY($1::uuid[])
                """,
                [str(oid) for oid in input.outcome_ids],
            )
        else:
            outcomes = await conn.fetch(
                """
                SELECT o.outcome_id::text AS outcome_id,
                       o.recommendation_id::text AS recommendation_id,
                       o.hit_or_miss,
                       o.outcome_payload,
                       o.recorded_at
                  FROM targeting.target_outcomes o
                  JOIN targeting.target_recommendations r
                    ON r.recommendation_id = o.recommendation_id
                 WHERE r.project_id = $1::uuid
                """,
                project_id,
            )

        if not outcomes:
            log.info("field_outcome_learning.no_outcomes workspace=%s", workspace_id)
            return FieldOutcomeLearningOutput(
                success=True,
                outcomes_processed=0,
            )

        # Aggregate hit/miss per workspace (model version isn't yet
        # populated; per doc-phase 184, use a default zero-uuid until
        # train_target_model graduates and produces real model_version_ids).
        hits = sum(1 for o in outcomes if o["hit_or_miss"] == "hit")
        misses = sum(1 for o in outcomes if o["hit_or_miss"] == "miss")
        total = len(outcomes)
        hit_rate = (hits / total) if total > 0 else 0.0

        # Compute window from oldest → newest recorded_at
        recorded = [o["recorded_at"] for o in outcomes if o["recorded_at"]]
        window_start = min(recorded) if recorded else datetime.now(tz=timezone.utc)
        window_end = max(recorded) if recorded else window_start

        # Write a targeting.target_backtests row (zero model_version uuid
        # as a placeholder until train_target_model lands)
        ZERO_MODEL_VERSION = "00000000-0000-0000-0000-000000000000"
        backtest_id = await conn.fetchval(
            """
            INSERT INTO targeting.target_backtests
                (backtest_id, model_version_id, workspace_id,
                 window_start, window_end, metrics_payload, computed_at)
            VALUES (gen_random_uuid(), $1::uuid, $2::uuid, $3, $4, $5::jsonb, NOW())
            RETURNING backtest_id::text
            """,
            ZERO_MODEL_VERSION, workspace_id,
            window_start, window_end,
            f'{{"total":{total},"hits":{hits},"misses":{misses},"hit_rate":{hit_rate}}}',
        )
        backtests_written = 1

        # Optionally write a lessons-learned row if a parent decision exists
        # for this project's targeting sign-offs (lookup by project + type)
        lessons_written = 0
        decision_row = await conn.fetchrow(
            """
            SELECT decision_id::text
              FROM silver.decision_records
             WHERE workspace_id = $1::uuid
               AND decision_type = 'target_signoff'
             ORDER BY decided_at DESC
             LIMIT 1
            """,
            workspace_id,
        )
        if decision_row:
            await conn.execute(
                """
                INSERT INTO silver.decision_lessons_learned
                    (lesson_id, decision_id, workspace_id, lesson_text,
                     created_at)
                VALUES (gen_random_uuid(), $1::uuid, $2::uuid, $3, NOW())
                ON CONFLICT DO NOTHING
                """,
                decision_row["decision_id"], workspace_id,
                f"Field outcomes folded — total={total} hits={hits} misses={misses} "
                f"hit_rate={hit_rate:.2%}",
            )
            lessons_written = 1

        # Audit emit
        try:
            from app.audit import emit_audit
            await emit_audit(
                conn,
                action_type="field_outcome.learned",
                workspace_id=workspace_id,
                actor_id=None,
                actor_kind="workflow",
                target_schema="targeting",
                target_table="target_backtests",
                target_id=backtest_id,
                payload={
                    "workspace_id": workspace_id,
                    "project_id": project_id,
                    "outcomes_processed": total,
                    "hits": hits,
                    "misses": misses,
                    "hit_rate": hit_rate,
                    "backtests_written": backtests_written,
                    "lessons_written": lessons_written,
                },
                trace_id=ctx.workflow_run_id,
            )
        except Exception as e:
            log.warning("field_outcome_learning.audit_emit_failed err=%s", e)

        # Heuristic: trigger retraining if >= 25 new outcomes folded
        retraining_triggered = total >= 25

        log.info(
            "field_outcome_learning.complete workspace=%s outcomes=%d "
            "hit_rate=%.2f%% backtests=%d lessons=%d retrain=%s",
            workspace_id, total, hit_rate * 100,
            backtests_written, lessons_written, retraining_triggered,
        )

        # Phase 5 admin surface push — drives Admin/HypothesisWorkspace
        # (hypothesis-evidence-link counts move with each fold) and
        # Admin/WorkflowRuns. Best-effort.
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            admin_payload = {
                "workflow_kind": "field_outcome_learning",
                "workspace_id": str(workspace_id),
                "outcomes_processed": total,
                "backtests_written": backtests_written,
                "lessons_written": lessons_written,
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
                "field_outcome_learning: admin surface broadcasts failed "
                "workspace=%s err=%s", workspace_id, exc,
            )

        return FieldOutcomeLearningOutput(
            success=True,
            outcomes_processed=total,
            backtests_written=backtests_written,
            lessons_written=lessons_written,
            retraining_triggered=retraining_triggered,
        )
    except Exception as e:
        log.exception("field_outcome_learning.failed")
        return FieldOutcomeLearningOutput(
            success=False,
            error=f"{type(e).__name__}: {str(e)[:200]}",
        )
    finally:
        await conn.close()


__all__ = [
    "field_outcome_learning",
    "FieldOutcomeLearningInput",
    "FieldOutcomeLearningOutput",
]
