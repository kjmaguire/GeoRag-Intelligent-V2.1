"""Answer-run supplementary endpoints — Module 7 Phase B Chunk 1.

Two endpoints:

  GET  /v1/answer_runs/{answer_run_id}/events?since_event_seq=N
       ─ Replay SSE events from the Redis ring buffer for reconnect dedup.

  POST /v1/answer_runs/{answer_run_id}/feedback
       ─ Submit thumbs-up / thumbs-down feedback with optional 6-taxonomy
         category and free-form note.

Auth (both endpoints)
─────────────────────
X-Service-Key header (shared secret) — same as queries.py and evidence.py.
Optional JWT for user_id extraction (graceful rollout; X-Service-Key alone
is sufficient while Laravel doesn't always mint a JWT on every call).

RBAC (both endpoints)
──────────────────────
The answer_run must belong to the caller's workspace_id.  Cross-tenant
requests return 403 — same status as "not found" would be, to prevent
existence enumeration.  The workspace is resolved from the X-Workspace-Id
header (service-to-service) or falls back to the default single-tenant UUID
(same pattern as evidence.py).

Architecture references
───────────────────────
  Module spec §6 B1 (replay), B6 (feedback), §10p taxonomy, §07f addendum
  georag-architecture.html Section 06 (Redis timeout: 500ms)
  georag-architecture.html Section 07d (Laravel↔FastAPI contract surface)
"""

from __future__ import annotations

import json
import logging
from typing import Any, Annotated
from uuid import UUID

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, status

from app.models.feedback import FeedbackCreate, FeedbackRead
from app.services.auth import UserContext, extract_user_context, verify_service_key

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/v1/answer_runs",
    tags=["answer_runs"],
    dependencies=[Depends(verify_service_key)],
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPLAY_MAX_EVENTS = 1000
_REDIS_KEY_PREFIX = "georag:answer_run_events"
_REDIS_TIMEOUT_S = 0.5  # Section 06 — Redis ≤500ms


# ---------------------------------------------------------------------------
# Workspace resolver (Module 9 Chunk 9.4 — uses shared helper, no fallback)
# ---------------------------------------------------------------------------

# Module 9 Chunk 9.4 (A2-04) — _resolve_workspace_id replaced by the shared
# helper in app.services.workspace_resolution. The default-UUID fallback is
# gone; missing workspace context now returns HTTP 403.
from app.services.workspace_resolution import resolve_workspace_id  # noqa: E402


# ---------------------------------------------------------------------------
# RBAC helper — verify answer_run belongs to workspace
# ---------------------------------------------------------------------------


async def _check_answer_run_workspace(
    pg_pool: Any,
    answer_run_id: UUID,
    workspace_id: UUID,
) -> None:
    """Assert that answer_run_id exists in workspace_id.

    Raises HTTP 403 on mismatch OR missing row (prevents existence enumeration).
    Raises HTTP 403 (not 404) so callers cannot probe which answer_run_ids exist.

    Args:
        pg_pool:        asyncpg Pool from app.state.pg_pool.
        answer_run_id:  UUID of the target answer run.
        workspace_id:   Resolved workspace UUID from caller context.
    """
    if pg_pool is None:
        # Dev / unit-test path — no pool attached; skip RBAC.
        logger.warning(
            "_check_answer_run_workspace: pg_pool is None, skipping RBAC check "
            "answer_run_id=%s",
            answer_run_id,
        )
        return

    try:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT workspace_id FROM silver.answer_runs "
                "WHERE answer_run_id = $1",
                answer_run_id,
            )
    except asyncpg.PostgresError as exc:
        logger.error(
            "_check_answer_run_workspace: DB error answer_run_id=%s: %s",
            answer_run_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="answer_run_lookup_failed",
        )

    # 403 on both "not found" and "wrong workspace" — prevent enumeration.
    if row is None or UUID(str(row["workspace_id"])) != workspace_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="answer_run not accessible",
        )


# ---------------------------------------------------------------------------
# GET /v1/answer_runs/{answer_run_id}/events
# ---------------------------------------------------------------------------


@router.get(
    "/{answer_run_id}/events",
    summary="Replay SSE events for an answer run",
    description=(
        "Returns events from the Redis ring buffer for a completed or "
        "in-flight answer run.  Use on WebSocket/SSE reconnect to catch up "
        "missed events.  Events older than 1 hour are gone (ring buffer TTL). "
        "Returns events strictly AFTER since_event_seq (i.e. event_seq > N). "
        "Max 1000 events per call."
    ),
)
async def get_answer_run_events(
    answer_run_id: Annotated[UUID, Path(description="UUID of the answer run")],
    since_event_seq: Annotated[int, Query(ge=0)] = 0,
    request: Request = ...,  # type: ignore[assignment]
    user: UserContext = Depends(extract_user_context),
) -> list[dict[str, Any]]:
    """Replay events for an answer_run since the given event_seq.

    Returns events strictly AFTER since_event_seq (event_seq > since_event_seq).
    Used by Module 7 UI on WebSocket reconnect to catch up missed events.
    Client deduplicates via event_id set (UUID4 per frame).

    Max 1000 events per call.  Events older than 1h are gone (Redis TTL).
    Returns an empty list if no events exist or all are before since_event_seq.
    """
    pg_pool = getattr(request.app.state, "pg_pool", None)
    redis = getattr(request.app.state, "redis_client", None)
    workspace_id = await resolve_workspace_id(user, request, pg_pool, redis)

    await _check_answer_run_workspace(pg_pool, answer_run_id, workspace_id)

    if redis is None:
        logger.warning(
            "get_answer_run_events: redis_client is None — replay unavailable "
            "answer_run_id=%s",
            answer_run_id,
        )
        return []

    key = f"{_REDIS_KEY_PREFIX}:{answer_run_id}"
    try:
        raw_list: list[bytes] = await redis.lrange(key, 0, -1)
    except Exception as exc:
        logger.error(
            "get_answer_run_events: Redis error for key=%s: %s", key, exc
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="event_replay_failed",
        )

    events: list[dict[str, Any]] = []
    for raw in raw_list:
        try:
            event = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        seq = event.get("event_seq", 0)
        if isinstance(seq, int) and seq > since_event_seq:
            events.append(event)
        if len(events) >= _REPLAY_MAX_EVENTS:
            break

    # Sort ascending by event_seq so client can apply in order.
    events.sort(key=lambda e: e.get("event_seq", 0))
    return events


# ---------------------------------------------------------------------------
# POST /v1/answer_runs/{answer_run_id}/feedback
# ---------------------------------------------------------------------------


@router.post(
    "/{answer_run_id}/feedback",
    response_model=FeedbackRead,
    status_code=status.HTTP_201_CREATED,
    summary="Submit feedback for an answer run",
    description=(
        "Submit thumbs-up or thumbs-down feedback for a completed answer run. "
        "Thumbs-down requires a category from the 6-value taxonomy. "
        "Optional free-form note up to 2000 chars. "
        "Multiple rows per user per answer_run are allowed (latest wins at render time)."
    ),
)
async def post_feedback(
    answer_run_id: Annotated[UUID, Path(description="UUID of the answer run")],
    feedback: FeedbackCreate,
    request: Request = ...,  # type: ignore[assignment]
    user: UserContext = Depends(extract_user_context),
) -> FeedbackRead:
    """POST /v1/answer_runs/{answer_run_id}/feedback — record user feedback.

    Status codes:
      201 Created  — feedback saved; body is FeedbackRead
      400 Bad Request — invalid payload (handled by Pydantic before this runs)
      403 Forbidden   — answer_run doesn't exist OR belongs to different workspace
      500 Internal    — DB write failure
    """
    pg_pool = getattr(request.app.state, "pg_pool", None)
    redis = getattr(request.app.state, "redis_client", None)
    workspace_id = await resolve_workspace_id(user, request, pg_pool, redis)

    await _check_answer_run_workspace(pg_pool, answer_run_id, workspace_id)

    # Resolve user_id from JWT sub-claim (int or None).
    user_id: int | None = None
    if user.user_id is not None:
        try:
            user_id = int(user.user_id)
        except (ValueError, TypeError):
            user_id = None

    if pg_pool is None:
        logger.error(
            "post_feedback: pg_pool is None — cannot persist feedback "
            "answer_run_id=%s",
            answer_run_id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="feedback_store_unavailable",
        )

    try:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO silver.message_feedback
                    (answer_run_id, workspace_id, user_id, polarity, category, note)
                VALUES ($1, $2, $3, $4, $5, $6)
                RETURNING
                    feedback_id,
                    answer_run_id,
                    workspace_id,
                    user_id,
                    polarity,
                    category,
                    note,
                    created_at
                """,
                answer_run_id,
                workspace_id,
                user_id,
                feedback.polarity,
                feedback.category,
                feedback.note,
            )
    except asyncpg.ForeignKeyViolationError:
        # Shouldn't happen after _check_answer_run_workspace passes, but guard anyway.
        logger.warning(
            "post_feedback: FK violation answer_run_id=%s workspace_id=%s",
            answer_run_id,
            workspace_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="answer_run not accessible",
        )
    except asyncpg.CheckViolationError as exc:
        logger.warning(
            "post_feedback: CHECK constraint violation answer_run_id=%s: %s",
            answer_run_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="feedback_constraint_violation",
        )
    except asyncpg.PostgresError as exc:
        logger.error(
            "post_feedback: DB error answer_run_id=%s: %s", answer_run_id, exc
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="feedback_store_failed",
        )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="feedback_store_failed",
        )

    return FeedbackRead(
        feedback_id=row["feedback_id"],
        answer_run_id=row["answer_run_id"],
        workspace_id=row["workspace_id"],
        user_id=row["user_id"],
        polarity=row["polarity"],
        category=row["category"],
        note=row["note"],
        created_at=row["created_at"],
    )


# ---------------------------------------------------------------------------
# GET /v1/answer_runs/{answer_run_id}/trust-summary  (§19.2 Trust Inspector)
# ---------------------------------------------------------------------------
@router.get(
    "/{answer_run_id}/trust-summary",
    summary="Aggregated trust summary for the Trust Inspector drawer",
    description=(
        "Returns the 7-section payload the §19.2 Trust Inspector renders: "
        "evidence count + sources + assumptions + conflicts + confidence + "
        "missing data + provenance chain."
    ),
)
async def get_trust_summary(
    answer_run_id: Annotated[UUID, Path(description="UUID of the answer run")],
    request: Request = ...,  # type: ignore[assignment]
    user: UserContext = Depends(extract_user_context),
) -> dict[str, Any]:
    """§19.2 — aggregate everything a geologist needs to decide whether
    to trust the answer. Joins answer_runs, citation_items, retrieval_items,
    refusal info, and run-level metrics into a single payload.
    """
    from app.services.workspace_resolution import resolve_workspace_id

    pg_pool = getattr(request.app.state, "pg_pool", None)
    redis = getattr(request.app.state, "redis_client", None)
    workspace_id = await resolve_workspace_id(user, request, pg_pool, redis)
    await _check_answer_run_workspace(pg_pool, answer_run_id, workspace_id)

    if pg_pool is None:
        raise HTTPException(503, "pg_pool not initialised")

    async with pg_pool.acquire() as conn:
        # 1. Top-level answer_run header.
        ar = await conn.fetchrow(
            """
            SELECT answer_run_id::text, query_text, query_class, model_name,
                   citation_lifecycle_state, citation_mode, partial_resolution_rate,
                   rejection_reason, created_at, evidence_truncated_count,
                   workspace_data_version_at_query
              FROM silver.answer_runs
             WHERE answer_run_id = $1::uuid
            """,
            answer_run_id,
        )
        if ar is None:
            raise HTTPException(404, "answer_run not found")

        # 2. Citation summary — count per source_store + per
        # accepted-vs-rejected (rows with rejection_reason failed
        # downstream validation; the rest were accepted into the answer).
        citations = await conn.fetch(
            """
            SELECT source_store,
                   CASE WHEN rejection_reason IS NULL THEN 'accepted' ELSE 'rejected' END AS state,
                   count(*)::int AS n
              FROM silver.answer_citation_items
             WHERE answer_run_id = $1::uuid
             GROUP BY source_store, state
             ORDER BY source_store, state
            """,
            answer_run_id,
        )

        # 3. Retrieval summary — what was fetched (& stage)
        retrieval = await conn.fetch(
            """
            SELECT stage, count(*)::int AS n,
                   avg(retriever_score)::float AS avg_retriever,
                   avg(reranker_score)::float AS avg_reranker,
                   sum(CASE WHEN included_in_context THEN 1 ELSE 0 END)::int AS included,
                   sum(CASE WHEN used_in_citation  THEN 1 ELSE 0 END)::int AS used_in_citation
              FROM silver.answer_retrieval_items
             WHERE answer_run_id = $1::uuid
             GROUP BY stage
             ORDER BY stage
            """,
            answer_run_id,
        )

        # 4. Sample citation rows for the "Sources" section
        source_samples = await conn.fetch(
            """
            SELECT answer_citation_item_id::text AS citation_id,
                   source_store,
                   COALESCE(marker_text, '') AS marker_text,
                   COALESCE(confidence, 0)::float AS confidence,
                   evidence_id::text AS evidence_id,
                   CASE WHEN rejection_reason IS NULL THEN 'accepted' ELSE 'rejected' END AS state,
                   rejection_reason
              FROM silver.answer_citation_items
             WHERE answer_run_id = $1::uuid
             ORDER BY confidence DESC NULLS LAST
             LIMIT 25
            """,
            answer_run_id,
        )

        # 5b. §7.4 Claim ledger summary
        try:
            claims = await conn.fetchrow(
                """
                SELECT count(*)::int AS total,
                       sum(CASE WHEN verification_status = 'verified' THEN 1 ELSE 0 END)::int AS verified,
                       sum(CASE WHEN verification_status = 'failed' THEN 1 ELSE 0 END)::int AS failed,
                       sum(CASE WHEN verification_status = 'pending' THEN 1 ELSE 0 END)::int AS pending,
                       sum(CASE WHEN verification_status = 'insufficient' THEN 1 ELSE 0 END)::int AS insufficient
                  FROM silver.claim_ledger
                 WHERE answer_run_id = $1::uuid
                """,
                answer_run_id,
            )
            claim_summary = dict(claims) if claims else {
                "total": 0, "verified": 0, "failed": 0,
                "pending": 0, "insufficient": 0,
            }
        except Exception:
            claim_summary = {
                "total": 0, "verified": 0, "failed": 0,
                "pending": 0, "insufficient": 0,
            }

        # 5. User feedback on this run (table may not exist on every
        # install — guard so the endpoint still returns the rest).
        try:
            fb = await conn.fetch(
                """
                SELECT polarity, category, note, created_at
                  FROM silver.answer_run_feedback
                 WHERE answer_run_id = $1::uuid
                 ORDER BY created_at DESC
                 LIMIT 5
                """,
                answer_run_id,
            )
        except Exception:
            fb = []

    # 6. Compute aggregate confidence — basic heuristic from citation
    # resolution rate + retrieval coverage. (Full hallucination-layer
    # confidence comes from app.agent.hallucination per run; this is
    # the operator-facing rollup.)
    cite_total = sum(c["n"] for c in citations) or 0
    cite_resolved = sum(
        c["n"] for c in citations if (c["lifecycle_state"] or "") == "resolved"
    )
    resolution_pct = round(
        (cite_resolved / cite_total * 100.0) if cite_total else 0.0, 1,
    )

    # 7. Inferred "missing data" — when partial_resolution_rate < 1.0
    # or rejection_reason mentions insufficient evidence
    missing_data: list[str] = []
    prr = ar["partial_resolution_rate"]
    if prr is not None and float(prr) < 1.0:
        missing_data.append(
            f"{round((1.0 - float(prr)) * 100)}% of citations couldn't be resolved"
        )
    if ar["rejection_reason"]:
        missing_data.append(f"Refusal: {ar['rejection_reason']}")
    if ar["evidence_truncated_count"] and int(ar["evidence_truncated_count"]) > 0:
        missing_data.append(
            f"{ar['evidence_truncated_count']} evidence chunks dropped (context truncation)"
        )

    return {
        "answer_run_id":       ar["answer_run_id"],
        "query_text":          ar["query_text"],
        "query_class":         ar["query_class"],
        "model_name":          ar["model_name"],
        "citation_lifecycle_state": ar["citation_lifecycle_state"],
        "citation_mode":       ar["citation_mode"],
        "partial_resolution_rate": (
            float(ar["partial_resolution_rate"])
            if ar["partial_resolution_rate"] is not None else None
        ),
        "rejection_reason":    ar["rejection_reason"],
        "created_at":          ar["created_at"].isoformat() if ar["created_at"] else None,
        "data_version":        int(ar["workspace_data_version_at_query"] or 0),
        # 7-section payload for the drawer
        "citations": {
            "total":               cite_total,
            "resolved":            cite_resolved,
            "resolution_pct":      resolution_pct,
            "per_kind_state":      [dict(c) for c in citations],
        },
        "retrieval": {
            "per_stage":           [dict(r) for r in retrieval],
        },
        "sources":                 [dict(s) for s in source_samples],
        "confidence_summary": {
            "resolution_pct":      resolution_pct,
            "lifecycle":           ar["citation_lifecycle_state"],
            "verdict": (
                "high"   if resolution_pct >= 90 and not missing_data else
                "medium" if resolution_pct >= 70 else
                "low"
            ),
        },
        "missing_data":            missing_data,
        "conflicts": {
            "count":               0,  # Wired separately via /v1/conflicts/{answer_run_id}
            "see_endpoint":        f"/v1/conflicts/{answer_run_id}",
        },
        "assumptions":             [],  # Layer-2 typed-output captures these; surface in v2
        "claim_ledger":            claim_summary,  # §7.4 — verification rollup
        "feedback":                [dict(f) for f in fb],
        "provenance": {
            "trace_id":            ar["query_class"],  # placeholder
            "lookup_endpoint":     f"/v1/answer_runs/{answer_run_id}/events",
        },
    }


# ---------------------------------------------------------------------------
# GET /v1/answer_runs/{answer_run_id}/lineage — Phase 1 / Step 1.5
# ---------------------------------------------------------------------------


@router.get(
    "/{answer_run_id}/lineage",
    summary="Session-level lineage artifact for an answer run",
    description=(
        "Returns the lineage payload persisted alongside the answer: every "
        "chunk that was retrieved (cited or not), the filters and QA/QC "
        "exclusions active at query time, the LLM model used, which guards "
        "fired, and the OIUR schema version. 404 when the run carries no "
        "lineage (legacy pre-OIUR row or OIUR flag was off)."
    ),
)
async def get_answer_run_lineage(
    answer_run_id: Annotated[UUID, Path(description="UUID of the answer run")],
    request: Request = ...,  # type: ignore[assignment]
    user: UserContext = Depends(extract_user_context),
) -> dict[str, Any]:
    """Return the session-level lineage for one answer run.

    Auth: X-Service-Key + workspace match (same RBAC as the other endpoints
    in this router). Cross-tenant requests return 403.

    Response shape:

        {
            "answer_run_id":          "<uuid>",
            "session_id":             "<uuid> | null",
            "query_text":             "<verbatim user query>",
            "query_timestamp":        "<iso-8601 created_at>",
            "llm_model":              "<model_name column>",
            "answer_schema_version":  "1.0 | null",
            "retrieved_sources":      [ { source_type, chunk_id, pdf_id, score, cited }, ... ],
            "filters_applied":        { project_id, workspace_id, ... },
            "qaqc_filters_applied":   { silver_review_excluded_batches, ... },
            "guards_triggered":       { ...hallucination_guard_results... },
        }
    """
    pg_pool = getattr(request.app.state, "pg_pool", None)
    redis = getattr(request.app.state, "redis_client", None)
    workspace_id = await resolve_workspace_id(user, request, pg_pool, redis)

    await _check_answer_run_workspace(pg_pool, answer_run_id, workspace_id)

    if pg_pool is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="lineage_unavailable",
        )

    try:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT  answer_run_id,
                        session_id,
                        query_text,
                        created_at,
                        model_name,
                        answer_schema_version,
                        lineage_retrieved_sources,
                        lineage_filters_applied,
                        lineage_qaqc_filters_applied,
                        hallucination_guard_results
                  FROM  silver.answer_runs
                 WHERE  answer_run_id = $1
                """,
                answer_run_id,
            )
    except asyncpg.PostgresError as exc:
        logger.error(
            "get_answer_run_lineage: DB error answer_run_id=%s: %s",
            answer_run_id,
            exc,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="lineage_lookup_failed",
        )

    if row is None:
        # _check_answer_run_workspace would normally have caught this, but
        # be defensive — return 403 to avoid existence enumeration.
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="answer_run not accessible",
        )

    schema_v = row["answer_schema_version"]
    if schema_v is None:
        # Lineage never captured for this row (pre-OIUR or flag-off run).
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="lineage_not_captured",
        )

    def _decode_jsonb(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return None

    return {
        "answer_run_id":         str(row["answer_run_id"]),
        "session_id":            str(row["session_id"]) if row["session_id"] else None,
        "query_text":            row["query_text"],
        "query_timestamp":       row["created_at"].isoformat() if row["created_at"] else None,
        "llm_model":             row["model_name"],
        "answer_schema_version": schema_v,
        "retrieved_sources":     _decode_jsonb(row["lineage_retrieved_sources"]) or [],
        "filters_applied":       _decode_jsonb(row["lineage_filters_applied"]) or {},
        "qaqc_filters_applied":  _decode_jsonb(row["lineage_qaqc_filters_applied"]) or {},
        "guards_triggered":      _decode_jsonb(row["hallucination_guard_results"]) or {},
    }
