"""Phase H4 UI — /admin/audit combined audit-findings page.

Covers three surfaces that backend graduations need a UI for:
  - §11.5 Tenant Isolation Auditor findings (live state)
  - §11.10 audit cold-tier archival runs
  - §6.4 public/private boundary language violations

  GET  /api/v1/admin/audit/tenant-isolation-findings
  GET  /api/v1/admin/audit/cold-tier-archive-runs
  GET  /api/v1/admin/audit/boundary-violations
  POST /api/v1/admin/audit/cold-tier-archive  (dry-run trigger)

Authentication: service-key (admin proxy).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.audit.cold_tier_archive import archive_window
from app.services.auth import verify_service_key


logger = logging.getLogger(__name__)


router = APIRouter(
    prefix="/api/v1/admin/audit",
    tags=["audit-findings"],
    dependencies=[Depends(verify_service_key)],
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class TenantIsolationFinding(BaseModel):
    schema_name: str = Field(..., alias="schema")
    table: str
    gate: str          # workspace_id | rls_enabled | policy | fk | index
    detail: str

    model_config = {"populate_by_name": True}


class TenantIsolationReport(BaseModel):
    findings: list[TenantIsolationFinding]
    total: int
    auditor_clean: bool


class ColdTierArchiveRun(BaseModel):
    run_id: str
    workspace_id: str | None
    created_at: datetime
    payload: dict[str, Any]


class ColdTierArchiveList(BaseModel):
    runs: list[ColdTierArchiveRun]
    total: int


class ColdTierArchiveRequest(BaseModel):
    cutoff_before_iso: datetime
    archive_bucket: str = "audit-cold-tier"
    workspace_id_scope: UUID | None = None
    dry_run: bool = True


class BoundaryViolation(BaseModel):
    audit_id: str
    workspace_id: str | None
    created_at: datetime
    payload: dict[str, Any]


class BoundaryViolationList(BaseModel):
    violations: list[BoundaryViolation]
    total: int


# ---------------------------------------------------------------------------
# Tenant Isolation Audit findings — live DB probe
# ---------------------------------------------------------------------------


_TENANT_SCHEMAS = ("silver", "gold", "audit", "ops", "workflow", "targeting")
_EXEMPT_WS = {
    ("silver", "workspaces"), ("silver", "users"),
    ("silver", "user_workspace_grants"),
    ("silver", "geological_ontology_terms"),
    ("silver", "geological_ontology_synonyms"),
    ("workflow", "flow_jwt_keys"), ("workflow", "flow_registry"),
    ("targeting", "target_models"), ("targeting", "target_model_versions"),
}


@router.get("/tenant-isolation-findings", response_model=TenantIsolationReport)
async def get_tenant_isolation_findings() -> TenantIsolationReport:
    """Run the §11.5 Tenant Isolation Auditor gates against the live
    schema and return the offender list."""
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )

    findings: list[TenantIsolationFinding] = []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT n.nspname AS schema_name, c.relname AS table_name
              FROM pg_class c
              JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE c.relkind IN ('r', 'p')
               AND n.nspname = ANY($1::text[])
             ORDER BY n.nspname, c.relname
            """,
            list(_TENANT_SCHEMAS),
        )

        for r in rows:
            s, t = r["schema_name"], r["table_name"]
            if (s, t) in _EXEMPT_WS:
                continue

            col = await conn.fetchval(
                "SELECT 1 FROM information_schema.columns "
                "WHERE table_schema=$1 AND table_name=$2 AND column_name='workspace_id'",
                s, t,
            )
            if not col:
                findings.append(TenantIsolationFinding(
                    schema_name=s, table=t, gate="workspace_id",
                    detail="missing workspace_id column",
                ))
                continue

            rls = await conn.fetchval(
                "SELECT c.relrowsecurity FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=$1 AND c.relname=$2",
                s, t,
            )
            if not rls:
                findings.append(TenantIsolationFinding(
                    schema_name=s, table=t, gate="rls_enabled",
                    detail="RLS not enabled",
                ))

            pol = await conn.fetchval(
                """
                WITH RECURSIVE parents AS (
                    SELECT ($1 || '.' || $2)::regclass AS oid
                    UNION ALL
                    SELECT i.inhparent FROM pg_inherits i JOIN parents p ON p.oid = i.inhrelid
                )
                SELECT count(*) FROM pg_policies pp
                  JOIN parents p ON (pp.schemaname || '.' || pp.tablename)::regclass = p.oid
                 WHERE pp.qual ILIKE '%workspace_id%' OR pp.with_check ILIKE '%workspace_id%'
                """,
                s, t,
            )
            if (pol or 0) == 0:
                findings.append(TenantIsolationFinding(
                    schema_name=s, table=t, gate="policy",
                    detail="no workspace_id-filtering policy",
                ))

            idx = await conn.fetchval(
                "SELECT count(*) FROM pg_indexes "
                "WHERE schemaname=$1 AND tablename=$2 AND indexdef ILIKE '%workspace_id%'",
                s, t,
            )
            if (idx or 0) == 0:
                findings.append(TenantIsolationFinding(
                    schema_name=s, table=t, gate="index",
                    detail="no workspace_id index",
                ))

    return TenantIsolationReport(
        findings=findings,
        total=len(findings),
        auditor_clean=(len(findings) == 0),
    )


# ---------------------------------------------------------------------------
# Cold-tier archive runs (from audit ledger)
# ---------------------------------------------------------------------------


@router.get("/cold-tier-archive-runs", response_model=ColdTierArchiveList)
async def list_cold_tier_archive_runs(limit: int = 50) -> ColdTierArchiveList:
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
            SELECT id::text          AS run_id,
                   workspace_id::text AS workspace_id,
                   created_at         AS created_at,
                   payload            AS payload
              FROM audit.audit_ledger
             WHERE action_type IN ('audit.cold_tier_archive.run',
                                   'audit_ledger.archive_window.completed',
                                   'audit_ledger.archive_window.dry_run')
             ORDER BY created_at DESC
             LIMIT $1
            """,
            limit,
        )

    out: list[ColdTierArchiveRun] = []
    for r in rows:
        payload = r["payload"] or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}
        out.append(ColdTierArchiveRun(
            run_id=r["run_id"],
            workspace_id=r["workspace_id"],
            created_at=r["created_at"],
            payload=payload if isinstance(payload, dict) else {},
        ))
    return ColdTierArchiveList(runs=out, total=len(out))


@router.post("/cold-tier-archive")
async def trigger_cold_tier_archive(req: ColdTierArchiveRequest) -> dict[str, Any]:
    """Run the audit cold-tier archive in dry-run mode (default).

    For real (non-dry) runs the operator must explicitly set
    `dry_run: false`. Production typically schedules this via the
    archive cron; this endpoint exists for ad-hoc operator review
    of what WOULD be archived.
    """
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )

    # Need a cold-tier store. For real runs the caller would
    # supply SeaweedFS credentials; for dry-run we don't need one.
    class _NoopStore:
        async def put(self, key: str, content: bytes) -> str:
            return f"noop://{key}"

    async with pool.acquire() as conn:
        run = await archive_window(
            conn,
            cutoff_before=req.cutoff_before_iso,
            archive_bucket=req.archive_bucket,
            cold_tier=_NoopStore(),
            workspace_id_scope=str(req.workspace_id_scope) if req.workspace_id_scope else None,
            dry_run=req.dry_run,
        )

    return run.to_dict()


# ---------------------------------------------------------------------------
# Boundary violations (audit ledger)
# ---------------------------------------------------------------------------


@router.get("/boundary-violations", response_model=BoundaryViolationList)
async def list_boundary_violations(limit: int = 50) -> BoundaryViolationList:
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
                   created_at         AS created_at,
                   payload            AS payload
              FROM audit.audit_ledger
             WHERE action_type ILIKE '%boundary%'
                OR action_type ILIKE '%language_violation%'
                OR action_type ILIKE '%public_private%'
             ORDER BY created_at DESC
             LIMIT $1
            """,
            limit,
        )

    out: list[BoundaryViolation] = []
    for r in rows:
        payload = r["payload"] or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}
        out.append(BoundaryViolation(
            audit_id=r["audit_id"],
            workspace_id=r["workspace_id"],
            created_at=r["created_at"],
            payload=payload if isinstance(payload, dict) else {},
        ))
    return BoundaryViolationList(violations=out, total=len(out))


__all__ = ["router"]
