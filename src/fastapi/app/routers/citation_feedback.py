"""§12.8 citation feedback endpoint (Phase H4 UI work).

Backs the 👍/👎 buttons on chat citations.

Storage shape (database audit 2026-09-29 PG-7). This used to INSERT
``(feature_id, workspace_id, source_document_id, payload, recorded_at)``
into ``silver.source_trust_features`` — three of those columns do not
exist, and the table's real NOT NULL columns (``trust_score_id``,
``feature_name``, ``feature_value``) were never supplied, so every call
raised UndefinedColumn -> 500 -> Laravel 502. The writer now uses the
table as its migration (2026_05_13_150000) defines it:

* A features row must hang off a ``silver.source_trust_scores`` row, so
  each (workspace, source document) gets one *anchor* score row with
  ``model_version = 'citation_feedback'`` and the neutral
  ``trust_score = 0.5`` — the value ``boost_by_trust`` already falls back
  to for an unscored source. ``boost_by_trust`` skips anchor rows, so a
  trained score is never shadowed by one.
* ``(trust_score_id, feature_name)`` is UNIQUE, so feedback is an
  aggregate rather than one row per click: one ``citation_accuracy``
  feature per anchor, ``feature_value`` = (right + 0.5 x partial) /
  events, and ``payload`` = ``{"right", "wrong", "partial",
  "event_count", "last_event"}``. Both upserts are atomic under
  concurrent clicks.
* The per-event record (who, which answer run / citation item, verdict,
  reason) is the ``citation.feedback.recorded`` audit-ledger entry this
  handler already emitted.

``train_source_trust`` is where these features are meant to be consumed
once enough feedback accumulates; its deterministic baseline runs in the
meantime.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.db import scoped_connection
from app.services.auth import verify_service_key
from app.services.source_trust.boost import FEEDBACK_ANCHOR_MODEL_VERSION

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
# SQL
# ---------------------------------------------------------------------------


# One anchor score row per (workspace, source). The no-op DO UPDATE is what
# makes RETURNING yield the existing row's id on conflict.
UPSERT_ANCHOR_SCORE_SQL = """
    INSERT INTO silver.source_trust_scores (
        workspace_id, source_document_id, trust_score, model_version
    )
    VALUES ($1::uuid, $2::uuid, 0.5, $3)
    ON CONFLICT (workspace_id, source_document_id, model_version)
    DO UPDATE SET computed_at = silver.source_trust_scores.computed_at
    RETURNING trust_score_id
"""

# One citation_accuracy feature per anchor. Counts are incremented in SQL so
# two concurrent clicks cannot lose an update.
#   $1 trust_score_id  $2 workspace_id  $3 first feature_value
#   $4 first payload   $5/$6/$7 right/wrong/partial increment  $8 last_event
UPSERT_FEEDBACK_FEATURE_SQL = """
    INSERT INTO silver.source_trust_features AS f (
        trust_score_id, workspace_id, feature_name, feature_value, payload
    )
    VALUES ($1::uuid, $2::uuid, 'citation_accuracy', $3, $4::jsonb)
    ON CONFLICT (trust_score_id, feature_name) DO UPDATE SET
        payload = jsonb_build_object(
            'right',       COALESCE((f.payload->>'right')::int, 0) + $5::int,
            'wrong',       COALESCE((f.payload->>'wrong')::int, 0) + $6::int,
            'partial',     COALESCE((f.payload->>'partial')::int, 0) + $7::int,
            'event_count', COALESCE((f.payload->>'event_count')::int, 0) + 1,
            'last_event',  $8::jsonb
        ),
        feature_value = round(
            (COALESCE((f.payload->>'right')::int, 0) + $5::int
             + 0.5 * (COALESCE((f.payload->>'partial')::int, 0) + $7::int))::numeric
            / (COALESCE((f.payload->>'event_count')::int, 0) + 1),
            4
        )
    RETURNING feature_id, (payload->>'event_count')::int AS event_count
"""


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


@router.post("/feedback", response_model=FeedbackResponse, status_code=status.HTTP_201_CREATED)
async def post_feedback(req: FeedbackRequest) -> FeedbackResponse:
    """Fold one citation-feedback event into silver.source_trust_features."""
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )

    recorded_at = datetime.now(UTC)
    ws = str(req.workspace_id)
    source_id = str(req.source_document_id)

    payload = {
        "feedback":            req.verdict,
        "reason":              req.reason,
        "answer_run_id":       str(req.answer_run_id),
        "citation_item_id":    str(req.citation_item_id),
        "source_document_id":  source_id,
        "submitted_by_user_id": req.submitted_by_user_id,
        "recorded_at":         recorded_at.isoformat(),
    }
    increments = {
        "right": int(req.verdict == "right"),
        "wrong": int(req.verdict == "wrong"),
        "partial": int(req.verdict == "partial"),
    }
    first_value = increments["right"] + 0.5 * increments["partial"]
    last_event = {
        "verdict": req.verdict,
        "answer_run_id": str(req.answer_run_id),
        "citation_item_id": str(req.citation_item_id),
        "recorded_at": recorded_at.isoformat(),
    }

    # REC#2 Phase-2 migration (2026-06-03): the canonical helper binds the
    # workspace GUC; ONE transaction wraps both upserts + the audit anchor.
    async with scoped_connection(
        pool, workspace_id=ws, site="citation_feedback.record"
    ) as conn:
        trust_score_id = await conn.fetchval(
            UPSERT_ANCHOR_SCORE_SQL, ws, source_id, FEEDBACK_ANCHOR_MODEL_VERSION,
        )
        row = await conn.fetchrow(
            UPSERT_FEEDBACK_FEATURE_SQL,
            trust_score_id,
            ws,
            first_value,
            json.dumps({**increments, "event_count": 1, "last_event": last_event}),
            increments["right"],
            increments["wrong"],
            increments["partial"],
            json.dumps(last_event),
        )
        feature_id = str(row["feature_id"])
        cumulative = int(row["event_count"])

        # Audit anchor — the per-event record. Best-effort, but inside a
        # SAVEPOINT: a failed INSERT aborts the surrounding transaction, and
        # swallowing it bare would roll back the two upserts above while still
        # answering 201 (the feedback silently lost).
        try:
            from app.audit import emit_audit
            async with conn.transaction():
                await emit_audit(
                    conn,
                    action_type="citation.feedback.recorded",
                    workspace_id=ws,
                    actor_id=req.submitted_by_user_id,
                    actor_kind="user",
                    target_schema="silver",
                    target_table="source_trust_features",
                    target_id=feature_id,
                    payload=payload,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("citation feedback: audit emit failed err=%s", exc)

    logger.info(
        "citation.feedback: workspace=%s source=%s verdict=%s cumulative=%d",
        ws, source_id, req.verdict, cumulative,
    )

    return FeedbackResponse(
        feature_id=feature_id,
        workspace_id=ws,
        source_document_id=source_id,
        verdict=req.verdict,
        recorded_at=recorded_at,
        cumulative_feedback_for_source=cumulative,
    )


__all__ = ["router"]
