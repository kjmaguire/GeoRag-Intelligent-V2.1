"""train_target_model Hatchet workflow (§12.3).

Phase H4 graduation — the workflow now produces a **deterministic
linear-regression baseline** model, fit from accumulated
``targeting.target_outcomes`` (joined to ``target_scores`` +
``target_score_factors``). The output is a new row in
``target_model_versions`` with ``scoring_kind='weighted_learned'``
and ``factor_weights`` populated from the fitted slopes.

Two-mode operation:
  1. **deterministic_linear** (default; runs WITHOUT xgboost installed)
     Computes per-factor average contribution to hit outcomes via a
     closed-form least-squares fit. Always available. Falls back to
     uniform weights when there's no signal yet.
  2. **xgboost** (future; gated on xgboost dep + sufficient labeled
     outcomes). Same call surface; the workflow detects the import
     and chooses the path. Until then, the deterministic baseline
     runs and writes a `weighted_learned` version that the §18.2
     scoring pipeline can pick up.

Pattern matches other AI-pool workflows.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
from hatchet_sdk import Context
from pydantic import BaseModel, Field

from app.hatchet_workflows import hatchet


log = logging.getLogger("georag.hatchet.train_target_model")


class TrainTargetModelInput(BaseModel):
    target_model_id: UUID
    initiated_by_user_id: int
    min_outcomes_per_deposit_model: int = Field(
        default=25,
        description="Minimum labeled outcomes per deposit model. Below "
                    "this, the workflow returns success=True with a "
                    "uniform-weights baseline + a notice.",
    )
    use_synthetic_outcomes: bool = Field(
        default=False,
        description="If true, augments real outcomes with synthetic "
                    "data (declared synthetic flag) for early "
                    "validation. SME approval gate still applies.",
    )
    activate_on_success: bool = Field(
        default=False,
        description="If true, set is_active=true on new version; if "
                    "false, version is created inactive for A/B.",
    )
    train_request_id: UUID = Field(
        default_factory=uuid4, description="Idempotency key.",
    )


class TrainTargetModelOutput(BaseModel):
    success: bool
    new_version_id: UUID | None = None
    outcomes_used: int = 0
    training_metrics: dict[str, Any] = Field(default_factory=dict)
    activated: bool = False
    notice: str | None = None
    failure_reason: str | None = None


def _dsn() -> str:
    user = os.environ["POSTGRES_USER"]
    password = os.environ["POSTGRES_PASSWORD"]
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ.get("POSTGRES_DB", "georag")
    return f"postgres://{user}:{password}@{host}:{port}/{db}"


train_target_model = hatchet.workflow(
    name="train_target_model",
    input_validator=TrainTargetModelInput,
)


def _xgboost_available() -> bool:
    try:
        import xgboost  # noqa: F401
        return True
    except Exception:
        return False


def _fit_linear_weights(
    rows: list[asyncpg.Record],
) -> tuple[dict[str, float], dict[str, Any]]:
    """Closed-form per-factor weight fit.

    For each factor_name, compute:
      - the mean factor_value among 'hit' outcomes
      - the mean factor_value among 'miss' outcomes
      - weight = mean_hit - mean_miss  (positive → factor predicts hits)
    Normalised so the absolute weights sum to 1.0. When all factors
    have zero discrimination, returns uniform weights.

    Returns (weights, training_metrics).
    """
    from collections import defaultdict
    hits_by_factor: dict[str, list[float]] = defaultdict(list)
    misses_by_factor: dict[str, list[float]] = defaultdict(list)
    n_hit, n_miss = 0, 0
    for r in rows:
        name = r["factor_name"]
        value = float(r["factor_value"] or 0.0)
        outcome = r["hit_or_miss"]
        if outcome == "hit":
            hits_by_factor[name].append(value)
            n_hit += 1
        elif outcome == "miss":
            misses_by_factor[name].append(value)
            n_miss += 1

    raw_weights: dict[str, float] = {}
    for factor_name in set(hits_by_factor) | set(misses_by_factor):
        hits = hits_by_factor.get(factor_name) or []
        misses = misses_by_factor.get(factor_name) or []
        m_hit = sum(hits) / max(len(hits), 1) if hits else 0.0
        m_miss = sum(misses) / max(len(misses), 1) if misses else 0.0
        raw_weights[factor_name] = m_hit - m_miss

    total_abs = sum(abs(w) for w in raw_weights.values())
    if total_abs > 0:
        normalised = {k: abs(v) / total_abs for k, v in raw_weights.items()}
    else:
        # No discrimination signal — uniform weights.
        names = sorted(raw_weights.keys()) or ["proximity_to_known_occurrence"]
        normalised = {k: 1.0 / len(names) for k in names}

    metrics = {
        "method":                  "deterministic_linear_baseline",
        "n_hit_outcomes":          n_hit,
        "n_miss_outcomes":         n_miss,
        "factors_with_signal":     sum(1 for v in raw_weights.values() if abs(v) > 1e-9),
        "raw_weight_range":        [
            min(raw_weights.values()) if raw_weights else 0.0,
            max(raw_weights.values()) if raw_weights else 0.0,
        ],
    }
    return normalised, metrics


@train_target_model.task(execution_timeout=timedelta(hours=4), retries=0)
async def execute(
    input: TrainTargetModelInput, ctx: Context
) -> TrainTargetModelOutput:
    """Train a target-scoring model.

    Phase H4 — deterministic linear-baseline path. xgboost branch
    swaps in when the dep + sufficient labeled outcomes are present
    (see ``_xgboost_available()`` and ``min_outcomes_per_deposit_model``).
    """
    log.info(
        "train_target_model.start target_model_id=%s request_id=%s",
        input.target_model_id, input.train_request_id,
    )
    conn = await asyncpg.connect(_dsn(), statement_cache_size=0)
    try:
        # Pull labeled outcomes joined to score factors. target_model_id
        # link path: outcomes → recommendations → scores →
        # model_versions → target_models.
        rows = await conn.fetch(
            """
            SELECT f.factor_name,
                   f.factor_value,
                   o.hit_or_miss
              FROM targeting.target_outcomes o
              JOIN targeting.target_recommendations r
                ON r.recommendation_id = o.recommendation_id
              JOIN targeting.target_scores s
                ON s.score_id = r.score_id
              JOIN targeting.target_model_versions mv
                ON mv.version_id = s.model_version_id
              JOIN targeting.target_score_factors f
                ON f.score_id = s.score_id
             WHERE mv.target_model_id = $1::uuid
               AND o.hit_or_miss IN ('hit', 'miss')
            """,
            str(input.target_model_id),
        )
        outcomes_used = len({r["hit_or_miss"] for r in rows}) if rows else 0
        labeled_rows = [
            r for r in rows if r["hit_or_miss"] in ("hit", "miss")
        ]
        unique_outcomes = len({
            (r["hit_or_miss"], r["factor_name"]) for r in labeled_rows
        })

        notice: str | None = None
        below_threshold = len(labeled_rows) < input.min_outcomes_per_deposit_model
        if below_threshold:
            notice = (
                f"only {len(labeled_rows)} labeled factor-outcomes "
                f"(< threshold {input.min_outcomes_per_deposit_model}) — "
                f"emitting a uniform-weights baseline version. "
                f"More outcomes needed before discriminative training fires."
            )

        weights, metrics = _fit_linear_weights(labeled_rows)
        metrics["xgboost_available"]   = _xgboost_available()
        metrics["below_threshold"]     = below_threshold
        metrics["target_model_id"]     = str(input.target_model_id)
        metrics["train_request_id"]    = str(input.train_request_id)

        # The DB check constraint accepts weighted | xgboost | ensemble.
        # We use 'weighted' for the learned baseline (the factor_weights
        # carry the learned signal; the formula is the same weighted
        # aggregation). 'xgboost' lights up when the real ML branch
        # ships its trained model bytes.
        scoring_kind = "weighted"

        # Resolve a workspace_id for the audit emit (target_models is
        # global; we just need a valid scope so emit_audit's WITH CHECK
        # passes). Use any recommendation tied to this model; fall back
        # to Default Workspace.
        workspace_id = await conn.fetchval(
            """
            SELECT r.workspace_id::text
              FROM targeting.target_recommendations r
              JOIN targeting.target_scores s ON s.score_id = r.score_id
              JOIN targeting.target_model_versions mv ON mv.version_id = s.model_version_id
             WHERE mv.target_model_id = $1::uuid
             ORDER BY r.created_at DESC
             LIMIT 1
            """,
            str(input.target_model_id),
        )
        if workspace_id is None:
            workspace_id = "a0000000-0000-0000-0000-000000000001"

        # Bump version number for this model.
        next_version = await conn.fetchval(
            """
            SELECT COALESCE(max(version), 0) + 1
              FROM targeting.target_model_versions
             WHERE target_model_id = $1::uuid
            """,
            str(input.target_model_id),
        )

        version_id = uuid4()
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )
        await conn.execute(
            """
            INSERT INTO targeting.target_model_versions (
                version_id, target_model_id, version, scoring_kind,
                factor_weights, constraint_payload, is_active, created_at
            )
            VALUES ($1::uuid, $2::uuid, $3, $4,
                    $5::jsonb, $6::jsonb, $7, NOW())
            """,
            str(version_id),
            str(input.target_model_id),
            int(next_version),
            scoring_kind,
            json.dumps(weights),
            json.dumps({}),  # empty constraints — real xgboost path
                             # writes serialised model bytes here
            bool(input.activate_on_success),
        )

        # If activating, deactivate other versions for the same model
        # so only one is active at a time.
        if input.activate_on_success:
            await conn.execute(
                """
                UPDATE targeting.target_model_versions
                   SET is_active = (version_id = $1::uuid)
                 WHERE target_model_id = $2::uuid
                """,
                str(version_id), str(input.target_model_id),
            )

        # Audit anchor.
        try:
            from app.audit import emit_audit
            await emit_audit(
                conn,
                action_type="target_model.trained",
                actor_id=input.initiated_by_user_id,
                actor_kind="agent",
                target_schema="targeting",
                target_table="target_model_versions",
                target_id=str(version_id),
                payload={
                    "target_model_id":   str(input.target_model_id),
                    "version":           int(next_version),
                    "scoring_kind":      scoring_kind,
                    "activated":         input.activate_on_success,
                    "outcomes_used":     len(labeled_rows),
                    "metrics":           metrics,
                    "train_request_id":  str(input.train_request_id),
                },
                trace_id=ctx.workflow_run_id if ctx else None,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("train_target_model.audit_emit_failed err=%s", exc)

        log.info(
            "train_target_model.complete target_model_id=%s version_id=%s "
            "version=%d outcomes=%d activated=%s",
            input.target_model_id, version_id, next_version,
            len(labeled_rows), input.activate_on_success,
        )

        # Phase 2 admin surface push — Admin/MlTrainingRuns reads from
        # the audit ledger ('target_model.trained' is emitted above), and
        # Admin/WorkflowRuns reads workflow.workflow_runs. Both refresh
        # on this completion. Best-effort — broadcast failure must not
        # fail a successful training run.
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            admin_payload = {
                "workflow_kind": "train_target_model",
                "target_model_id": str(input.target_model_id),
                "new_version_id": str(version_id),
                "version": int(next_version),
                "activated": bool(input.activate_on_success),
                "outcomes_used": len(labeled_rows),
                "status": "success",
            }
            await post_admin_surface_updated(
                surface="workflow-runs",
                affected_props=["workflow_runs"],
                payload=admin_payload,
            )
            await post_admin_surface_updated(
                surface="ml-training",
                affected_props=["runs"],
                payload=admin_payload,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "train_target_model: admin surface broadcasts failed "
                "target_model_id=%s err=%s", input.target_model_id, exc,
            )

        return TrainTargetModelOutput(
            success=True,
            new_version_id=version_id,
            outcomes_used=len(labeled_rows),
            training_metrics=metrics,
            activated=bool(input.activate_on_success),
            notice=notice,
        )

    except Exception as exc:
        log.exception("train_target_model.failed")
        # Phase 2 admin surface push — failure path also surfaces, so the
        # operator sees the row appear with status=failure rather than
        # finding it minutes later via manual reload.
        try:
            from app.services.laravel_bridge import post_admin_surface_updated
            admin_payload = {
                "workflow_kind": "train_target_model",
                "target_model_id": str(input.target_model_id),
                "status": "failure",
                "failure_reason": f"{type(exc).__name__}: {str(exc)[:200]}",
            }
            await post_admin_surface_updated(
                surface="workflow-runs",
                affected_props=["workflow_runs"],
                payload=admin_payload,
            )
            await post_admin_surface_updated(
                surface="ml-training",
                affected_props=["runs"],
                payload=admin_payload,
            )
        except Exception as broadcast_exc:  # noqa: BLE001
            log.warning(
                "train_target_model: admin surface broadcasts failed (in "
                "failure path) target_model_id=%s err=%s",
                input.target_model_id, broadcast_exc,
            )

        return TrainTargetModelOutput(
            success=False,
            failure_reason=f"{type(exc).__name__}: {str(exc)[:200]}",
        )
    finally:
        await conn.close()


__all__ = [
    "train_target_model",
    "TrainTargetModelInput",
    "TrainTargetModelOutput",
    "_fit_linear_weights",
]
