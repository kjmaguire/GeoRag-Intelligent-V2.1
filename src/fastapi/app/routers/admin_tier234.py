"""Phase H4 §11.1 + §11.10 — backup / cold-tier ops surface.

Trimmed 2026-07-28 (A4 completion / task #31): this module used to bundle 8
more admin-cockpit router groups (recommendations test-bench, QP credentials,
workspace members/settings, audit explorer, saved maps, alerts inbox,
phase-H4 health). All 8 had zero Laravel-side callers — the admin pages that
called them were deleted in the reader-core trim, and nothing else in the
codebase reached them; confirmed via a repo-wide grep for each route prefix
across app/, resources/js/, and routes/. Removed along with the phase9 agent
modules (analogue_finder, next_best_data) they exclusively imported.

backups_router survives because it isn't just a dead UI backend: its
SnapshotRun/ColdTierRun/WorkspaceConsistencyResponse models and the router
object itself are imported directly by tests/test_section11_cold_tier_workflow.py
and tests/test_section11_consistency.py, which test genuinely-live features
(the cold_tier_archive Hatchet workflow, the cross_store_consistency service).
No Laravel page calls this router either, but unlike the other 8, deleting it
would have broken real test coverage for real running code — so it's kept
as a standalone REST surface an operator can call directly.

Each route runs through the FastAPI service-key gate.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.services.auth import verify_service_key

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# §11.1 + §11.10 — backup / cold-tier ops surface
# ---------------------------------------------------------------------------
backups_router = APIRouter(
    prefix="/api/v1/admin/backups",
    tags=["backups"],
    dependencies=[Depends(verify_service_key)],
)


class SnapshotRun(BaseModel):
    run_id: str
    store: str
    started_at: datetime
    completed_at: datetime | None = None
    bucket: str | None = None
    object_key: str | None = None
    sha256_hex: str | None = None
    bytes: int | None = None
    status: str
    failure_reason: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class SnapshotRunList(BaseModel):
    items: list[SnapshotRun]
    total: int


@backups_router.get("/snapshot-runs", response_model=SnapshotRunList)
async def list_snapshot_runs(
    limit: int = 100,
    offset: int = 0,
    store: str | None = None,
    status: str | None = None,
) -> SnapshotRunList:
    """List recent backup snapshot runs across all stores.

    Pagination via limit + offset. Optional filters:
      - `store` — postgres | neo4j | qdrant | redis | seaweedfs
      - `status` — running | completed | failed
    """
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "pg_pool not initialised")

    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    where = ["TRUE"]
    params: list[Any] = []
    pi = 1
    if store:
        where.append(f"store = ${pi}")
        params.append(store)
        pi += 1
    if status:
        where.append(f"status = ${pi}")
        params.append(status)
        pi += 1
    where_sql = " AND ".join(where)

    async with pool.acquire() as conn:
        # backups schema may not exist on a fresh install — graceful empty.
        exists = await conn.fetchval(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='backups' AND table_name='snapshot_runs'",
        )
        if not exists:
            return SnapshotRunList(items=[], total=0)

        rows = await conn.fetch(
            f"""
            SELECT run_id::text       AS run_id,
                   store, started_at, completed_at, bucket, object_key,
                   sha256_hex, bytes, status, failure_reason, payload
              FROM backups.snapshot_runs
             WHERE {where_sql}
             ORDER BY started_at DESC
             LIMIT ${pi} OFFSET ${pi + 1}
            """,
            *params, limit, offset,
        )
        total = await conn.fetchval(
            f"SELECT count(*) FROM backups.snapshot_runs WHERE {where_sql}",
            *params,
        )

    items: list[SnapshotRun] = []
    for r in rows:
        payload = r["payload"] or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}
        items.append(SnapshotRun(
            run_id=r["run_id"],
            store=r["store"],
            started_at=r["started_at"],
            completed_at=r["completed_at"],
            bucket=r["bucket"],
            object_key=r["object_key"],
            sha256_hex=r["sha256_hex"],
            bytes=r["bytes"],
            status=r["status"],
            failure_reason=r["failure_reason"],
            payload=payload if isinstance(payload, dict) else {},
        ))
    return SnapshotRunList(items=items, total=int(total or 0))


class ColdTierRun(BaseModel):
    audit_id: str
    action_type: str  # audit.cold_tier.archive.{completed|failed}
    rows_archived: int
    cold_tier_uri: str
    hot_tier_remaining: int | None = None
    verification_passed: bool
    manifest_key: str | None = None
    duration_s: float | None = None
    created_at: datetime
    payload: dict[str, Any] = Field(default_factory=dict)


class ColdTierRunList(BaseModel):
    items: list[ColdTierRun]
    total: int


class WorkspaceConsistencyResponse(BaseModel):
    workspace_id: str
    postgres: dict[str, int]
    postgres_error: str | None = None
    neo4j_nodes: int
    neo4j_error: str | None = None
    qdrant_points: int
    qdrant_error: str | None = None
    redis_keys: int
    redis_error: str | None = None
    total_rows: int
    has_any_error: bool


@backups_router.get(
    "/workspace-consistency/{workspace_id}",
    response_model=WorkspaceConsistencyResponse,
)
async def workspace_consistency(workspace_id: UUID) -> WorkspaceConsistencyResponse:
    """Cross-store footprint report for one workspace (§11.2).

    Walks Postgres + Neo4j + Qdrant + Redis and returns per-store
    row/node/point/key counts. Useful as:
      - operator diagnostic before/after a restore
      - assertion step for the §11.3 restore_workspace round-trip tests
      - smoke check that a workspace is reachable across all five stores

    Errors in one store do not block the others; partial-availability
    states are surfaced via `has_any_error=true` + per-store `*_error`.
    """
    from app.main import app
    from app.services.cross_store_consistency import count_workspace_footprint

    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "pg_pool not initialised")

    footprint = await count_workspace_footprint(str(workspace_id), pool)
    return WorkspaceConsistencyResponse(**footprint.to_dict())


@backups_router.get("/cold-tier-runs", response_model=ColdTierRunList)
async def list_cold_tier_runs(limit: int = 50) -> ColdTierRunList:
    """List recent cold-tier archive runs. Sourced from audit_ledger
    rows where action_type LIKE 'audit.cold_tier.archive.%' — the
    workflow doesn't write to a dedicated table; the audit anchor IS
    the registry."""
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "pg_pool not initialised")
    limit = max(1, min(limit, 500))

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id::text   AS audit_id,
                   action_type, payload, created_at
              FROM audit.audit_ledger
             WHERE action_type LIKE 'audit.cold_tier.archive.%'
             ORDER BY created_at DESC
             LIMIT $1
            """,
            limit,
        )

    items: list[ColdTierRun] = []
    for r in rows:
        payload = r["payload"] or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}
        if not isinstance(payload, dict):
            payload = {}
        items.append(ColdTierRun(
            audit_id=r["audit_id"],
            action_type=r["action_type"],
            rows_archived=int(payload.get("rows_archived") or 0),
            cold_tier_uri=str(payload.get("cold_tier_uri") or ""),
            hot_tier_remaining=payload.get("hot_tier_remaining"),
            verification_passed=bool(payload.get("verification_passed", False)),
            manifest_key=payload.get("manifest_key"),
            duration_s=payload.get("duration_s"),
            created_at=r["created_at"],
            payload=payload,
        ))
    return ColdTierRunList(items=items, total=len(items))


__all__ = [
    "backups_router",
]
