"""Phase H4 Tier 1 misc admin routers (small surfaces).

  /api/v1/admin/source-trust/scores      — §21.5 viewer
  /api/v1/admin/export-gate/results       — §29 gate results table
  /api/v1/admin/load-test/runs            — §11.9 k6 launcher + history

All read from existing tables / audit ledger; no schema additions.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.services.auth import verify_service_key


logger = logging.getLogger(__name__)


# ── Source-trust scores viewer ──────────────────────────────────────


source_trust_router = APIRouter(
    prefix="/api/v1/admin/source-trust",
    tags=["source-trust-viewer"],
    dependencies=[Depends(verify_service_key)],
)


class SourceTrustScore(BaseModel):
    trust_score_id: str
    workspace_id: str
    source_document_id: str
    source_title: str | None = None
    trust_score: float
    model_version: str
    computed_at: datetime
    feedback_event_count: int = 0


class SourceTrustList(BaseModel):
    scores: list[SourceTrustScore]
    total: int


@source_trust_router.get("/scores", response_model=SourceTrustList)
async def list_source_trust_scores(
    workspace_id: UUID | None = None,
    limit: int = 100,
) -> SourceTrustList:
    """List per-source trust scores. Optional workspace filter."""
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )
    limit = max(1, min(limit, 500))

    where = "WHERE TRUE"
    params: list[Any] = []
    if workspace_id is not None:
        where += " AND s.workspace_id = $1::uuid"
        params.append(str(workspace_id))

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT s.trust_score_id::text     AS trust_score_id,
                   s.workspace_id::text       AS workspace_id,
                   s.source_document_id::text AS source_document_id,
                   r.title                    AS source_title,
                   s.trust_score::float       AS trust_score,
                   s.model_version            AS model_version,
                   s.computed_at              AS computed_at,
                   (SELECT count(*) FROM silver.source_trust_features f
                     WHERE f.workspace_id = s.workspace_id
                       AND f.source_document_id = s.source_document_id) AS feedback_event_count
              FROM silver.source_trust_scores s
              LEFT JOIN silver.reports r ON r.report_id = s.source_document_id
              {where}
             ORDER BY s.computed_at DESC, s.trust_score DESC
             LIMIT {limit}
            """,
            *params,
        )

    return SourceTrustList(
        scores=[
            SourceTrustScore(
                trust_score_id=r["trust_score_id"],
                workspace_id=r["workspace_id"],
                source_document_id=r["source_document_id"],
                source_title=r["source_title"],
                trust_score=float(r["trust_score"]),
                model_version=r["model_version"],
                computed_at=r["computed_at"],
                feedback_event_count=int(r["feedback_event_count"] or 0),
            )
            for r in rows
        ],
        total=len(rows),
    )


# ── Export Compliance gate results ─────────────────────────────────


export_gate_router = APIRouter(
    prefix="/api/v1/admin/export-gate",
    tags=["export-gate-results"],
    dependencies=[Depends(verify_service_key)],
)


class ExportGateResult(BaseModel):
    audit_id: str
    workspace_id: str | None
    target_id: str | None
    action_type: str
    created_at: datetime
    payload: dict[str, Any]


class ExportGateList(BaseModel):
    results: list[ExportGateResult]
    total: int


@export_gate_router.get("/results", response_model=ExportGateList)
async def list_export_gate_results(limit: int = 100) -> ExportGateList:
    """List recent §29 export compliance gate decisions."""
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
            SELECT id::text          AS audit_id,
                   workspace_id::text AS workspace_id,
                   target_id          AS target_id,
                   action_type        AS action_type,
                   created_at         AS created_at,
                   payload            AS payload
              FROM audit.audit_ledger
             WHERE action_type ILIKE 'report.export.gate%'
                OR action_type ILIKE 'export.compliance%'
                OR action_type ILIKE 'report.export.compliance%'
             ORDER BY created_at DESC
             LIMIT $1
            """,
            limit,
        )

    out: list[ExportGateResult] = []
    for r in rows:
        payload = r["payload"] or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}
        out.append(ExportGateResult(
            audit_id=r["audit_id"],
            workspace_id=r["workspace_id"],
            target_id=r["target_id"],
            action_type=r["action_type"],
            created_at=r["created_at"],
            payload=payload if isinstance(payload, dict) else {},
        ))
    return ExportGateList(results=out, total=len(out))


# ── k6 load test launcher + history ────────────────────────────────


k6_router = APIRouter(
    prefix="/api/v1/admin/load-test",
    tags=["k6-load-test"],
    dependencies=[Depends(verify_service_key)],
)


class K6Script(BaseModel):
    slug: str
    title: str
    path: str
    description: str


class K6ScriptCatalogue(BaseModel):
    scripts: list[K6Script]


_K6_SCRIPTS = [
    K6Script(
        slug="rag-query",
        title="RAG Query",
        path="tests/load_k6/rag_query.k6.js",
        description="20 RPS / p95 < 5s steady-state against /v1/rag/query.",
    ),
    K6Script(
        slug="ingestion-upload",
        title="Ingestion Upload",
        path="tests/load_k6/ingestion_upload.k6.js",
        description="5 RPS / p95 < 8s against POST /v1/documents.",
    ),
    K6Script(
        slug="viz-strip-log",
        title="Visualization Strip Log",
        path="tests/load_k6/viz_strip_log.k6.js",
        description="30 RPS / p95 < 2s against /v1/viz/strip_log.",
    ),
]


@k6_router.get("/scripts", response_model=K6ScriptCatalogue)
async def list_scripts() -> K6ScriptCatalogue:
    """List available k6 scripts. Trigger is operator-side
    (docker run grafana/k6) — the UI surfaces the script catalogue +
    operator runbook commands."""
    return K6ScriptCatalogue(scripts=_K6_SCRIPTS)


__all__ = ["source_trust_router", "export_gate_router", "k6_router"]
