"""§12 ML Training admin endpoints (Phase H4 UI work).

Backs the `/admin/ml/training-runs` Inertia surface. Provides:

  GET  /api/v1/admin/ml/training-runs
       Lists recent training runs from audit.audit_ledger
       (both target-model + source-trust).

  POST /api/v1/admin/ml/train-target-model
       Synchronously runs the train_target_model workflow against
       the live DB. Body: target_model_id + activate_on_success.

  POST /api/v1/admin/ml/train-source-trust
       Synchronously runs the train_source_trust workflow.
       Body: workspace_id + min_citations_per_source +
       model_version.

The workflows themselves are Hatchet-decorated; this router invokes
the underlying task body via `aio_mock_run` so it runs inline. For
long-running ML jobs the operator can switch to Hatchet's
client-side `.aio_run()` to enqueue a real workflow.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.hatchet_workflows.train_source_trust import (
    TrainSourceTrustInput,
)
from app.hatchet_workflows.train_source_trust import (
    execute as train_source_trust_execute,
)
from app.hatchet_workflows.train_target_model import (
    TrainTargetModelInput,
)
from app.hatchet_workflows.train_target_model import (
    execute as train_target_model_execute,
)
from app.services.auth import verify_service_key

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/v1/admin/ml",
    tags=["ml-training-cockpit"],
    dependencies=[Depends(verify_service_key)],
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TrainingRunSummary(BaseModel):
    run_id: str
    workspace_id: str | None
    kind: str          # "target_model" | "source_trust"
    actor_id: int | None
    completed_at: datetime
    success: bool
    metrics: dict[str, Any] = Field(default_factory=dict)


class TrainingRunList(BaseModel):
    runs: list[TrainingRunSummary]
    total: int


class TrainTargetModelRequest(BaseModel):
    target_model_id: UUID
    initiated_by_user_id: int
    activate_on_success: bool = False
    min_outcomes_per_deposit_model: int = 25


class TrainSourceTrustRequest(BaseModel):
    workspace_id: UUID
    initiated_by_user_id: int
    min_citations_per_source: int = 3
    model_version: str = Field(
        default="weighted_learned_v1", min_length=1, max_length=40,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/training-runs", response_model=TrainingRunList)
async def list_training_runs(limit: int = 50) -> TrainingRunList:
    """List recent ML training runs from the audit ledger."""
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )
    limit = max(1, min(limit, 500))

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text             AS run_id,
                   workspace_id::text   AS workspace_id,
                   action_type          AS action_type,
                   actor_id             AS actor_id,
                   created_at           AS completed_at,
                   payload              AS payload
              FROM audit.audit_ledger
             WHERE action_type IN ('target_model.trained', 'source_trust.trained')
             ORDER BY created_at DESC
             LIMIT $1
            """,
            limit,
        )

    runs: list[TrainingRunSummary] = []
    for r in rows:
        payload = r["payload"] or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}
        kind = (
            "target_model" if r["action_type"] == "target_model.trained"
            else "source_trust"
        )
        runs.append(TrainingRunSummary(
            run_id=r["run_id"] or "",
            workspace_id=r["workspace_id"],
            kind=kind,
            actor_id=r["actor_id"],
            completed_at=r["completed_at"],
            success=True,
            metrics=payload if isinstance(payload, dict) else {},
        ))
    return TrainingRunList(runs=runs, total=len(runs))


@router.post("/train-target-model", status_code=status.HTTP_201_CREATED)
async def post_train_target_model(req: TrainTargetModelRequest) -> dict[str, Any]:
    inp = TrainTargetModelInput(
        target_model_id=req.target_model_id,
        initiated_by_user_id=req.initiated_by_user_id,
        min_outcomes_per_deposit_model=req.min_outcomes_per_deposit_model,
        activate_on_success=req.activate_on_success,
        train_request_id=uuid4(),
    )
    out = await train_target_model_execute.aio_mock_run(inp)
    return out.model_dump(mode="json")


@router.post("/train-source-trust", status_code=status.HTTP_201_CREATED)
async def post_train_source_trust(req: TrainSourceTrustRequest) -> dict[str, Any]:
    inp = TrainSourceTrustInput(
        workspace_id=req.workspace_id,
        initiated_by_user_id=req.initiated_by_user_id,
        min_citations_per_source=req.min_citations_per_source,
        model_version=req.model_version,
        train_request_id=uuid4(),
    )
    out = await train_source_trust_execute.aio_mock_run(inp)
    return out.model_dump(mode="json")


__all__ = ["router"]
