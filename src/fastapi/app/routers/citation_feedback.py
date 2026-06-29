"""§12.8 citation feedback endpoint (Phase H4 UI work).

Backs the 👍/👎 buttons on chat citations. The feedback writes a
row into ``silver.source_trust_features`` keyed by source_document_id;
once ≥500 feedback events accumulate per workspace, the
``train_source_trust`` workflow can produce a real ML-trained trust
model (the deterministic baseline runs in the meantime).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.db import scoped_connection
from app.services.auth import verify_service_key


logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/v1/citations",
    tags=["citation-feedback"],
    dependencies=[Depends(verify_service_key)],
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


FeedbackVerdict = Literal["wrong", "right", "partial"]


class FeedbackRequest(BaseModel):
    workspace_id: UUID
    answer_run_id: UUID
    citation_item_id: UUID
    source_document_id: UUID
    verdict: FeedbackVerdict
    reason: str | None = Field(default=None, max_length=2000)
    submitted_by_user_id: int | None = None


class FeedbackResponse(BaseModel):
    feature_id: str
    workspace_id: str
    source_document_id: str
    verdict: FeedbackVerdict
    recorded_at: datetime
    cumulative_feedback_for_source: int


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/feedback", response_model=FeedbackResponse, status_code=status.HTTP_201_CREATED)
async def post_feedback(req: FeedbackRequest) -> FeedbackResponse:
    """Record one citation-feedback event in silver.source_trust_features."""
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )

    feature_id = uuid4()
    recorded_at = datetime.now(timezone.utc)
    ws = str(req.workspace_id)

    payload = {
        "feedback":            req.verdict,
        "reason":              req.reason,
        "answer_run_id":       str(req.answer_run_id),
        "citation_item_id":    str(req.citation_item_id),
        "submitted_by_user_id": req.submitted_by_user_id,
        "recorded_at":         recorded_at.isoformat(),
    }

    # REC#2 Phase-2 migration (2026-06-03). Replaces the bespoke
    # acquire+set_config dance with the canonical helper. Same RLS
    # behaviour; explicit UUID validation; parameter-bound GUC; ONE
    # transaction wraps the INSERT + the count query + the audit anchor.
    async with scoped_connection(
        pool, workspace_id=ws, site="citation_feedback.record"
    ) as conn:
        # silver.source_trust_features schema: (feature_id, workspace_id,
        # source_document_id, payload jsonb, recorded_at). The trust_score_id
        # FK is populated later when train_source_trust runs and creates a
        # source_trust_scores row; until then it stays NULL.
        await conn.execute(
            """
            INSERT INTO silver.source_trust_features (
                feature_id, workspace_id, source_document_id,
                payload, recorded_at
            )
            VALUES ($1::uuid, $2::uuid, $3::uuid, $4::jsonb, $5)
            """,
            str(feature_id), ws, str(req.source_document_id),
            __import__("json").dumps(payload, default=str),
            recorded_at,
        )

        cumulative = await conn.fetchval(
            """
            SELECT count(*) FROM silver.source_trust_features
             WHERE workspace_id = $1::uuid
               AND source_document_id = $2::uuid
            """,
            ws, str(req.source_document_id),
        )

        # Audit anchor.
        try:
            from app.audit import emit_audit
            await emit_audit(
                conn,
                action_type="citation.feedback.recorded",
                workspace_id=ws,
                actor_id=req.submitted_by_user_id,
                actor_kind="user",
                target_schema="silver",
                target_table="source_trust_features",
                target_id=str(feature_id),
                payload=payload,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("citation feedback: audit emit failed err=%s", exc)

    logger.info(
        "citation.feedback: workspace=%s source=%s verdict=%s cumulative=%d",
        ws, req.source_document_id, req.verdict, cumulative or 0,
    )

    return FeedbackResponse(
        feature_id=str(feature_id),
        workspace_id=ws,
        source_document_id=str(req.source_document_id),
        verdict=req.verdict,
        recorded_at=recorded_at,
        cumulative_feedback_for_source=int(cumulative or 0),
    )


__all__ = ["router"]
