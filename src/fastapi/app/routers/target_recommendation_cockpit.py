"""§8 Target Recommendation Cockpit endpoints (Phase H4 UI work).

Backs the `/admin/target_recommendation/runs/{run_id}` Inertia page in
Laravel. Provides:

  GET  /api/v1/admin/target_recommendation/runs/{run_id}
       Returns the run's full state envelope: ranked_targets,
       factor breakdowns, uncertainties, map_layer_uris,
       sign-off status.

  POST /api/v1/admin/target_recommendation/runs/{run_id}/signoff
       Records an R5 QP sign-off decision via the §8.5
       geologist_signoff agent. Refuses signed_off without
       credential_verified=True.

  GET  /api/v1/admin/target_recommendation/runs
       Lists recent runs across workspaces (operator-mode read).

Authentication: service-key (operator-only routes; Laravel proxies
authenticated admin clicks through these endpoints).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.agents.phase8.geologist_signoff import geologist_signoff
from app.services.auth import verify_service_key

logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/v1/admin/target_recommendation",
    tags=["target-recommendation-cockpit"],
    dependencies=[Depends(verify_service_key)],
)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class RankedTargetSummary(BaseModel):
    zone_id: str
    rank: int
    aggregate_score: float
    aggregate_uncertainty: float | None = None
    explanation_markdown: str | None = None
    factor_count: int = 0


class TargetRecommendationRunDetail(BaseModel):
    run_id: str
    workspace_id: str
    project_id: str
    project_name: str | None = None
    created_at: datetime | None = None
    ranked_targets: list[RankedTargetSummary] = Field(default_factory=list)
    target_model_slug: str | None = None
    scoring_kind: str | None = None
    map_layer_uris: dict[str, str] = Field(default_factory=dict)
    sign_off_status: Literal["pending", "signed_off", "rejected", "modified"] = "pending"
    last_decision: dict[str, Any] | None = None


class TargetRecommendationRunSummary(BaseModel):
    run_id: str
    workspace_id: str
    project_id: str
    project_name: str | None = None
    created_at: datetime | None = None
    target_count: int = 0
    top_score: float | None = None
    sign_off_status: str = "pending"


class TargetRecommendationRunList(BaseModel):
    runs: list[TargetRecommendationRunSummary]
    total: int


# ---------------------------------------------------------------------------
# Helpers — DB queries scoped operator-mode (no GUC), filtered by run_id
# ---------------------------------------------------------------------------


async def _fetch_run(pool, run_id: UUID) -> dict[str, Any] | None:
    """Return joined run + project + count metadata, or None if not found.

    Operator-mode: the Laravel side proxies authenticated admin clicks
    through this endpoint, so we don't filter by app.workspace_id GUC
    here — the Laravel layer already authorised. We DO pass workspace_id
    back so the caller can render it.
    """
    async with pool.acquire() as conn:
        run_row = await conn.fetchrow(
            """
            SELECT r.run_id::text                AS run_id,
                   r.workspace_id::text          AS workspace_id,
                   r.project_id::text            AS project_id,
                   p.project_name                AS project_name,
                   min(r.created_at)             AS created_at,
                   count(*)                      AS target_count
              FROM targeting.target_recommendations r
              LEFT JOIN silver.projects p ON p.project_id = r.project_id
             WHERE r.run_id = $1::uuid
             GROUP BY r.run_id, r.workspace_id, r.project_id, p.project_name
            """,
            str(run_id),
        )
        if run_row is None:
            return None

        # Set GUC to the run's workspace so the per-row reads see it.
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)",
            run_row["workspace_id"],
        )

        ranked = await conn.fetch(
            """
            SELECT r.zone_id::text          AS zone_id,
                   r.rank                   AS rank,
                   s.aggregate_score        AS aggregate_score,
                   s.aggregate_uncertainty  AS aggregate_uncertainty,
                   r.explanation_markdown   AS explanation_markdown,
                   (SELECT count(*) FROM targeting.target_score_factors f
                     WHERE f.score_id = s.score_id) AS factor_count
              FROM targeting.target_recommendations r
              JOIN targeting.target_scores s ON s.score_id = r.score_id
             WHERE r.run_id = $1::uuid
             ORDER BY r.rank ASC
            """,
            str(run_id),
        )

        last_decision_row = await conn.fetchrow(
            """
            SELECT review_id::text         AS review_id,
                   decision                AS decision,
                   rationale               AS rationale,
                   qp_user_id              AS qp_user_id,
                   qp_signature_method     AS qp_signature_method,
                   signed_at               AS signed_at
              FROM targeting.target_review_decisions
             WHERE target_id IN (
                 SELECT recommendation_id FROM targeting.target_recommendations
                  WHERE run_id = $1::uuid
             )
             ORDER BY signed_at DESC NULLS LAST
             LIMIT 1
            """,
            str(run_id),
        )

        sign_off_status = "pending"
        last_decision: dict[str, Any] | None = None
        if last_decision_row:
            sign_off_status = last_decision_row["decision"]
            last_decision = dict(last_decision_row)

        # Try to find target_model_slug via the first row's score chain.
        target_model_slug = None
        scoring_kind = None
        first = ranked[0] if ranked else None
        if first:
            slug_row = await conn.fetchrow(
                """
                SELECT tm.slug AS slug, mv.scoring_kind AS scoring_kind
                  FROM targeting.target_recommendations r
                  JOIN targeting.target_scores s         ON s.score_id = r.score_id
                  JOIN targeting.target_model_versions mv ON mv.version_id = s.model_version_id
                  JOIN targeting.target_models tm        ON tm.target_model_id = mv.target_model_id
                 WHERE r.zone_id = $1::uuid AND r.run_id = $2::uuid
                 LIMIT 1
                """,
                first["zone_id"], str(run_id),
            )
            if slug_row:
                target_model_slug = slug_row["slug"]
                scoring_kind = slug_row["scoring_kind"]

    return {
        "run_id":              run_row["run_id"],
        "workspace_id":        run_row["workspace_id"],
        "project_id":          run_row["project_id"],
        "project_name":        run_row["project_name"],
        "created_at":          run_row["created_at"],
        "target_count":        run_row["target_count"],
        "ranked_targets":      [dict(r) for r in ranked],
        "target_model_slug":   target_model_slug,
        "scoring_kind":        scoring_kind,
        "sign_off_status":     sign_off_status,
        "last_decision":       last_decision,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}", response_model=TargetRecommendationRunDetail)
async def get_run(run_id: UUID) -> TargetRecommendationRunDetail:
    """Fetch one run's full cockpit state."""
    from app.main import app

    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )

    data = await _fetch_run(pool, run_id)
    if data is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"run_id={run_id} not found",
        )

    ranked = [
        RankedTargetSummary(
            zone_id=r["zone_id"],
            rank=r["rank"],
            aggregate_score=float(r["aggregate_score"]),
            aggregate_uncertainty=(
                float(r["aggregate_uncertainty"])
                if r["aggregate_uncertainty"] is not None else None
            ),
            explanation_markdown=r["explanation_markdown"],
            factor_count=int(r["factor_count"] or 0),
        )
        for r in data["ranked_targets"]
    ]

    return TargetRecommendationRunDetail(
        run_id=data["run_id"],
        workspace_id=data["workspace_id"],
        project_id=data["project_id"],
        project_name=data["project_name"],
        created_at=data["created_at"],
        ranked_targets=ranked,
        target_model_slug=data["target_model_slug"],
        scoring_kind=data["scoring_kind"],
        map_layer_uris={},  # populated by create_map_layers; cockpit could re-fetch from state
        sign_off_status=data["sign_off_status"],
        last_decision=data["last_decision"],
    )


@router.get("/runs/{run_id}/geojson")
async def get_run_geojson(run_id: UUID) -> dict[str, Any]:
    """FeatureCollection of the run's zone polygons for MapLibre.
    Each feature carries zone_id + rank + aggregate_score."""
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )
    async with pool.acquire() as conn:
        ws = await conn.fetchval(
            """
            SELECT z.workspace_id::text
              FROM targeting.target_candidate_zones z
              JOIN targeting.target_recommendations r ON r.zone_id = z.zone_id
             WHERE r.run_id = $1::uuid LIMIT 1
            """,
            str(run_id),
        )
        if ws:
            await conn.execute(
                "SELECT set_config('app.workspace_id', $1, false)", ws,
            )
        rows = await conn.fetch(
            """
            SELECT z.zone_id::text                AS zone_id,
                   ST_AsGeoJSON(z.zone_geom)::json AS geometry,
                   r.rank                          AS rank,
                   s.aggregate_score::float        AS aggregate_score
              FROM targeting.target_candidate_zones z
              JOIN targeting.target_recommendations r ON r.zone_id = z.zone_id
              JOIN targeting.target_scores s ON s.score_id = r.score_id
             WHERE r.run_id = $1::uuid
             ORDER BY r.rank ASC
            """,
            str(run_id),
        )
    # asyncpg returns json columns as Python str (raw JSON text); parse
    # so the geometry is rendered as an object, not a string. ST_AsGeoJSON
    # always produces valid JSON so we don't need a try/except.
    import json as _json
    features = []
    for r in rows:
        geom = r["geometry"]
        if isinstance(geom, str):
            geom = _json.loads(geom)
        features.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "zone_id":         r["zone_id"],
                "rank":            r["rank"],
                "aggregate_score": float(r["aggregate_score"]) if r["aggregate_score"] is not None else None,
            },
        })
    return {"type": "FeatureCollection", "features": features}


@router.get("/runs", response_model=TargetRecommendationRunList)
async def list_runs(limit: int = 50) -> TargetRecommendationRunList:
    """List recent runs across workspaces (operator-mode read)."""
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
            SELECT r.run_id::text         AS run_id,
                   r.workspace_id::text   AS workspace_id,
                   r.project_id::text     AS project_id,
                   p.project_name         AS project_name,
                   min(r.created_at)      AS created_at,
                   count(*)               AS target_count,
                   max(s.aggregate_score) AS top_score,
                   COALESCE(
                       (SELECT d.decision FROM targeting.target_review_decisions d
                         WHERE d.target_id IN (
                             SELECT recommendation_id FROM targeting.target_recommendations
                              WHERE run_id = r.run_id
                         )
                         ORDER BY d.signed_at DESC NULLS LAST LIMIT 1),
                       'pending'
                   ) AS sign_off_status
              FROM targeting.target_recommendations r
              JOIN targeting.target_scores s ON s.score_id = r.score_id
              LEFT JOIN silver.projects p ON p.project_id = r.project_id
             GROUP BY r.run_id, r.workspace_id, r.project_id, p.project_name
             ORDER BY max(r.created_at) DESC NULLS LAST
             LIMIT $1
            """,
            limit,
        )

    summaries = [
        TargetRecommendationRunSummary(
            run_id=row["run_id"],
            workspace_id=row["workspace_id"],
            project_id=row["project_id"],
            project_name=row["project_name"],
            created_at=row["created_at"],
            target_count=int(row["target_count"] or 0),
            top_score=(
                float(row["top_score"]) if row["top_score"] is not None else None
            ),
            sign_off_status=row["sign_off_status"] or "pending",
        )
        for row in rows
    ]
    return TargetRecommendationRunList(runs=summaries, total=len(summaries))


# ---------------------------------------------------------------------------
# Sign-off
# ---------------------------------------------------------------------------


class SignOffRequest(BaseModel):
    target_id: UUID
    qp_user_id: int
    qp_credential_id: str
    decision: Literal["accepted", "modified", "rejected", "signed_off"]
    rationale: str = Field(..., min_length=1)
    qp_signature_method: str = "manual"
    credential_verified: bool = False


@router.post("/runs/{run_id}/signoff", status_code=status.HTTP_201_CREATED)
async def post_signoff(run_id: UUID, req: SignOffRequest) -> dict[str, Any]:
    """Record an R5 sign-off via geologist_signoff agent + persist
    the envelope to targeting.target_review_decisions."""
    from app.main import app

    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )

    # Resolve workspace_id from the target_id so we can scope writes.
    async with pool.acquire() as conn:
        ws_row = await conn.fetchrow(
            """
            SELECT workspace_id::text AS workspace_id
              FROM targeting.target_recommendations
             WHERE recommendation_id = $1::uuid AND run_id = $2::uuid
            """,
            str(req.target_id), str(run_id),
        )
        if ws_row is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"target_id={req.target_id} not part of run_id={run_id}",
            )
        workspace_id = ws_row["workspace_id"]

    # Invoke the geologist_signoff agent (raises ValueError if
    # signed_off without credential_verified).
    inner = getattr(geologist_signoff, "__wrapped__", geologist_signoff)
    try:
        envelope = await inner(
            ctx=None,
            workspace_id=workspace_id,
            target_id=req.target_id,
            qp_user_id=req.qp_user_id,
            qp_credential_id=req.qp_credential_id,
            decision=req.decision,
            rationale=req.rationale,
            qp_signature_method=req.qp_signature_method,
            credential_verified=req.credential_verified,
        )
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc),
        )

    # Persist to targeting.target_review_decisions.
    async with pool.acquire() as conn:
        await conn.execute(
            "SELECT set_config('app.workspace_id', $1, false)", workspace_id,
        )
        await conn.execute(
            """
            INSERT INTO targeting.target_review_decisions (
                review_id, workspace_id, target_id, qp_user_id,
                qp_credential_id, credential_verified_at, decision,
                rationale, qp_signature_method,
                target_recommendations_hash, claim_ledger_hash,
                signed_at
            )
            VALUES (
                $1::uuid, $2::uuid, $3::uuid, $4, $5, $6, $7, $8, $9,
                $10, $11, $12
            )
            """,
            envelope["review_id"],
            envelope["workspace_id"],
            envelope["target_id"],
            envelope["qp_user_id"],
            envelope["qp_credential_id"],
            (
                datetime.fromisoformat(envelope["credential_verified_at"])
                if envelope.get("credential_verified_at") else None
            ),
            envelope["decision"],
            envelope["rationale"],
            envelope["qp_signature_method"],
            envelope["target_recommendations_hash"],
            envelope["claim_ledger_hash"],
            datetime.fromisoformat(envelope["signed_at"]),
        )

    # Audit anchor.
    try:
        from app.audit import emit_audit
        async with pool.acquire() as conn:
            await emit_audit(
                conn,
                action_type=envelope["audit_action_type"],
                workspace_id=workspace_id,
                actor_id=req.qp_user_id,
                actor_kind="user",
                target_schema="targeting",
                target_table="target_review_decisions",
                target_id=envelope["review_id"],
                payload={
                    "run_id":           str(run_id),
                    "target_id":        str(req.target_id),
                    "decision":         req.decision,
                    "credential_verified": req.credential_verified,
                },
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("post_signoff: audit emit failed err=%s", exc)

    return envelope


__all__ = ["router"]
