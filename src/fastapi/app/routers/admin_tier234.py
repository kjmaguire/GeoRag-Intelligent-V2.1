"""Phase H4 Tier 2/3/4 admin routers.

Bundles the remaining UI surfaces into one module:

  /api/v1/admin/recommendations/nbd               — §9.5 NBD test-bench
  /api/v1/admin/recommendations/analogue          — §9.6 Analogue test-bench
  /api/v1/admin/qp-credentials                     — QP credential CRUD
  /api/v1/admin/workspace-members                  — workspace membership CRUD
  /api/v1/admin/workspace-settings                 — per-workspace prefs
  /api/v1/admin/audit-explorer                     — generic audit ledger search
  /api/v1/admin/saved-maps                         — silver.saved_map_views

Each routes through the FastAPI service-key gate (Laravel admin
controllers verify the operator's session before proxying).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.agents.phase9.analogue_finder import analogue_finder
from app.agents.phase9.next_best_data import next_best_data
from app.services.auth import verify_service_key

logger = logging.getLogger(__name__)


# ── §9.5 Next-Best-Data + §9.6 Analogue Finder test-benches ─────────


rec_router = APIRouter(
    prefix="/api/v1/admin/recommendations",
    tags=["recommendations-test-bench"],
    dependencies=[Depends(verify_service_key)],
)


class NbdRequest(BaseModel):
    workspace_id: UUID
    project_id: UUID
    evidence_gaps: list[str] = Field(..., min_length=1)
    budget_ceiling_usd: float | None = None


@rec_router.post("/nbd")
async def run_nbd(req: NbdRequest) -> dict[str, Any]:
    inner = getattr(next_best_data, "__wrapped__", next_best_data)
    return await inner(
        ctx=None,
        workspace_id=req.workspace_id,
        project_id=req.project_id,
        evidence_gaps=req.evidence_gaps,
        budget_ceiling_usd=req.budget_ceiling_usd,
    )


class AnalogueRequest(BaseModel):
    workspace_id: UUID
    target_model_id: UUID | str
    project_attributes: dict[str, Any]
    top_k: int = 10


@rec_router.post("/analogue")
async def run_analogue(req: AnalogueRequest) -> dict[str, Any]:
    inner = getattr(analogue_finder, "__wrapped__", analogue_finder)
    return await inner(
        ctx=None,
        workspace_id=req.workspace_id,
        target_model_id=req.target_model_id,
        project_attributes=req.project_attributes,
        top_k=req.top_k,
    )


# ── QP credentials CRUD ────────────────────────────────────────────


qp_router = APIRouter(
    prefix="/api/v1/admin/qp-credentials",
    tags=["qp-credentials"],
    dependencies=[Depends(verify_service_key)],
)


class QpCredential(BaseModel):
    qp_credential_id: str
    user_id: int
    name: str
    issuing_body: str             # e.g. "APGO", "EGBC", "PEGNL"
    registration_number: str
    jurisdiction: str
    expires_at: datetime | None = None
    verified_at: datetime | None = None
    is_active: bool = True


class QpCredentialList(BaseModel):
    credentials: list[QpCredential]
    total: int


@qp_router.get("", response_model=QpCredentialList)
async def list_qp_credentials() -> QpCredentialList:
    """List QP credentials from silver.qp_credentials (creates the
    table on first call if it doesn't exist — see migration 101)."""
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )
    async with pool.acquire() as conn:
        # Ensure the table exists (idempotent DDL — wrapped because the
        # prod role typically lacks CREATE on silver; migration 101 owns it).
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS silver.qp_credentials (
                    qp_credential_id   text PRIMARY KEY,
                    user_id            integer NOT NULL,
                    name               text NOT NULL,
                    issuing_body       text NOT NULL,
                    registration_number text NOT NULL,
                    jurisdiction       text NOT NULL,
                    expires_at         timestamptz,
                    verified_at        timestamptz,
                    is_active          boolean NOT NULL DEFAULT true,
                    created_at         timestamptz NOT NULL DEFAULT now(),
                    updated_at         timestamptz NOT NULL DEFAULT now()
                )
                """,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("qp_credentials DDL skipped (migration 101 owns it): %s", exc)
        rows = await conn.fetch(
            "SELECT qp_credential_id, user_id, name, issuing_body, registration_number, "
            "jurisdiction, expires_at, verified_at, is_active "
            "FROM silver.qp_credentials ORDER BY name",
        )
    return QpCredentialList(
        credentials=[QpCredential(**dict(r)) for r in rows],
        total=len(rows),
    )


class QpCreate(BaseModel):
    user_id: int
    name: str = Field(..., min_length=1, max_length=200)
    issuing_body: str = Field(..., min_length=1, max_length=80)
    registration_number: str = Field(..., min_length=1, max_length=80)
    jurisdiction: str = Field(..., min_length=1, max_length=40)
    expires_at: datetime | None = None


@qp_router.post("", status_code=status.HTTP_201_CREATED)
async def create_qp_credential(req: QpCreate) -> dict[str, Any]:
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )
    qp_id = f"{req.issuing_body}-{req.registration_number}"
    async with pool.acquire() as conn:
        try:
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS silver.qp_credentials (
                    qp_credential_id text PRIMARY KEY, user_id integer NOT NULL,
                    name text NOT NULL, issuing_body text NOT NULL,
                    registration_number text NOT NULL, jurisdiction text NOT NULL,
                    expires_at timestamptz, verified_at timestamptz,
                    is_active boolean NOT NULL DEFAULT true,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    updated_at timestamptz NOT NULL DEFAULT now()
                )
                """,
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug("qp_credentials DDL skipped (migration 101 owns it): %s", exc)
        await conn.execute(
            """
            INSERT INTO silver.qp_credentials (
                qp_credential_id, user_id, name, issuing_body,
                registration_number, jurisdiction, expires_at
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (qp_credential_id) DO UPDATE
                SET name = EXCLUDED.name,
                    expires_at = EXCLUDED.expires_at,
                    updated_at = now()
            """,
            qp_id, req.user_id, req.name, req.issuing_body,
            req.registration_number, req.jurisdiction, req.expires_at,
        )
    return {"qp_credential_id": qp_id, "status": "ok"}


class QpVerifyRequest(BaseModel):
    qp_credential_id: str


@qp_router.post("/{qp_credential_id}/verify")
async def verify_qp(qp_credential_id: str) -> dict[str, Any]:
    """Mark a QP credential as verified (staffed-ops action)."""
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )
    async with pool.acquire() as conn:
        n = await conn.execute(
            "UPDATE silver.qp_credentials SET verified_at = now(), "
            "updated_at = now() WHERE qp_credential_id = $1",
            qp_credential_id,
        )
    return {"qp_credential_id": qp_credential_id, "status": "verified", "rows": n}


# ── Workspace members ──────────────────────────────────────────────


ws_members_router = APIRouter(
    prefix="/api/v1/admin/workspace-members",
    tags=["workspace-members"],
    dependencies=[Depends(verify_service_key)],
)


class WorkspaceMember(BaseModel):
    workspace_id: str
    user_id: int
    user_name: str | None = None
    user_email: str | None = None
    role: str
    granted_at: datetime | None = None


class WorkspaceMemberList(BaseModel):
    members: list[WorkspaceMember]
    total: int


@ws_members_router.get("", response_model=WorkspaceMemberList)
async def list_workspace_members(workspace_id: UUID | None = None) -> WorkspaceMemberList:
    """List workspace members from workspace.workspace_memberships
    joined with workspace_roles for the role name + users for name/email.

    Returns empty when the workspace schema isn't present (e.g. a fresh
    install without the workspace-management migration applied).
    """
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )
    where = "WHERE TRUE"
    params: list[Any] = []
    if workspace_id is not None:
        where += " AND m.workspace_id = $1::uuid"
        params.append(str(workspace_id))

    async with pool.acquire() as conn:
        # Graceful degradation when the workspace schema isn't present.
        exists = await conn.fetchval(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'workspace' AND table_name = 'workspace_memberships'",
        )
        if not exists:
            return WorkspaceMemberList(members=[], total=0)
        rows = await conn.fetch(
            f"""
            SELECT m.workspace_id::text AS workspace_id,
                   m.user_id            AS user_id,
                   u.name               AS user_name,
                   u.email              AS user_email,
                   r.name               AS role,
                   COALESCE(m.accepted_at, m.created_at) AS granted_at
              FROM workspace.workspace_memberships m
              LEFT JOIN workspace.workspace_roles  r ON r.id = m.role_id
              LEFT JOIN public.users               u ON u.id = m.user_id
             {where}
             ORDER BY COALESCE(m.accepted_at, m.created_at) DESC NULLS LAST
            """,
            *params,
        )
    return WorkspaceMemberList(
        members=[WorkspaceMember(**dict(r)) for r in rows],
        total=len(rows),
    )


# ── Workspace settings (Tier 3) ────────────────────────────────────


ws_settings_router = APIRouter(
    prefix="/api/v1/admin/workspace-settings",
    tags=["workspace-settings"],
    dependencies=[Depends(verify_service_key)],
)


class WorkspaceSetting(BaseModel):
    workspace_id: str
    default_tone: str = "technical"          # technical | executive | regulator
    default_report_type: str | None = None
    sla_max_response_ms: int | None = None
    extra_payload: dict[str, Any] = Field(default_factory=dict)


async def _ensure_workspace_settings_table(conn) -> None:
    """Idempotent table creation for dev. In prod the role typically
    lacks CREATE on silver — the table is created by migration 101.
    We swallow InsufficientPrivilege so the SELECT below proceeds.
    """
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS silver.workspace_settings (
                workspace_id          uuid PRIMARY KEY REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE,
                default_tone          text NOT NULL DEFAULT 'technical'
                                        CHECK (default_tone IN ('technical', 'executive', 'regulator')),
                default_report_type   text,
                sla_max_response_ms   integer,
                extra_payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
                created_at            timestamptz NOT NULL DEFAULT now(),
                updated_at            timestamptz NOT NULL DEFAULT now()
            )
            """,
        )
    except Exception as exc:  # noqa: BLE001 — broad on purpose
        logger.debug("workspace_settings DDL skipped (likely already created by migration): %s", exc)


@ws_settings_router.get("/{workspace_id}", response_model=WorkspaceSetting)
async def get_workspace_settings(workspace_id: UUID) -> WorkspaceSetting:
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )
    async with pool.acquire() as conn:
        await _ensure_workspace_settings_table(conn)
        row = await conn.fetchrow(
            "SELECT workspace_id::text AS workspace_id, default_tone, "
            "default_report_type, sla_max_response_ms, extra_payload "
            "FROM silver.workspace_settings WHERE workspace_id = $1::uuid",
            str(workspace_id),
        )
    if row is None:
        return WorkspaceSetting(workspace_id=str(workspace_id))
    return WorkspaceSetting(
        workspace_id=row["workspace_id"],
        default_tone=row["default_tone"],
        default_report_type=row["default_report_type"],
        sla_max_response_ms=row["sla_max_response_ms"],
        extra_payload=row["extra_payload"] or {},
    )


class WorkspaceSettingPut(BaseModel):
    default_tone: str = "technical"
    default_report_type: str | None = None
    sla_max_response_ms: int | None = None
    extra_payload: dict[str, Any] = Field(default_factory=dict)


@ws_settings_router.put("/{workspace_id}", response_model=WorkspaceSetting)
async def put_workspace_settings(
    workspace_id: UUID, req: WorkspaceSettingPut,
) -> WorkspaceSetting:
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )
    async with pool.acquire() as conn:
        await _ensure_workspace_settings_table(conn)
        await conn.execute(
            """
            INSERT INTO silver.workspace_settings (
                workspace_id, default_tone, default_report_type,
                sla_max_response_ms, extra_payload
            )
            VALUES ($1::uuid, $2, $3, $4, $5::jsonb)
            ON CONFLICT (workspace_id) DO UPDATE
                SET default_tone = EXCLUDED.default_tone,
                    default_report_type = EXCLUDED.default_report_type,
                    sla_max_response_ms = EXCLUDED.sla_max_response_ms,
                    extra_payload = EXCLUDED.extra_payload,
                    updated_at = now()
            """,
            str(workspace_id), req.default_tone, req.default_report_type,
            req.sla_max_response_ms, json.dumps(req.extra_payload),
        )
    return WorkspaceSetting(
        workspace_id=str(workspace_id),
        default_tone=req.default_tone,
        default_report_type=req.default_report_type,
        sla_max_response_ms=req.sla_max_response_ms,
        extra_payload=req.extra_payload,
    )


# ── Activepieces channels router removed 2026-05-17 ─────────────────
# Service was sunset at Phase 3 Step 7; Kestra is the integration boundary
# owner per master-plan §1. The workflow.activepieces_channels table is
# dropped by a follow-up migration.

ap_router = None  # type: ignore[assignment]


# ── Audit explorer (Tier 4) ────────────────────────────────────────


audit_explorer_router = APIRouter(
    prefix="/api/v1/admin/audit-explorer",
    tags=["audit-explorer"],
    dependencies=[Depends(verify_service_key)],
)


class AuditEntry(BaseModel):
    id: str
    workspace_id: str | None
    action_type: str
    target_schema: str | None
    target_table: str | None
    target_id: str | None
    actor_id: int | None
    created_at: datetime
    payload: dict[str, Any]


class AuditPage(BaseModel):
    entries: list[AuditEntry]
    total: int


@audit_explorer_router.get("/search", response_model=AuditPage)
async def search_audit(
    action_type_prefix: str | None = None,
    workspace_id: UUID | None = None,
    target_id: str | None = None,
    actor_id: int | None = None,
    limit: int = 100,
) -> AuditPage:
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )
    limit = max(1, min(limit, 500))

    where: list[str] = []
    params: list[Any] = []
    next_param = 1
    if action_type_prefix:
        where.append(f"action_type ILIKE ${next_param}")
        params.append(f"{action_type_prefix}%")
        next_param += 1
    if workspace_id is not None:
        where.append(f"workspace_id = ${next_param}::uuid")
        params.append(str(workspace_id))
        next_param += 1
    if target_id:
        where.append(f"target_id = ${next_param}")
        params.append(target_id)
        next_param += 1
    if actor_id is not None:
        where.append(f"actor_id = ${next_param}")
        params.append(actor_id)
        next_param += 1
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id::text           AS id,
                   workspace_id::text AS workspace_id,
                   action_type        AS action_type,
                   target_schema      AS target_schema,
                   target_table       AS target_table,
                   target_id          AS target_id,
                   actor_id           AS actor_id,
                   created_at         AS created_at,
                   payload            AS payload
              FROM audit.audit_ledger
             {where_sql}
             ORDER BY created_at DESC
             LIMIT {limit}
            """,
            *params,
        )
    out: list[AuditEntry] = []
    for r in rows:
        payload = r["payload"] or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}
        out.append(AuditEntry(
            id=r["id"],
            workspace_id=r["workspace_id"],
            action_type=r["action_type"],
            target_schema=r["target_schema"],
            target_table=r["target_table"],
            target_id=r["target_id"],
            actor_id=r["actor_id"],
            created_at=r["created_at"],
            payload=payload if isinstance(payload, dict) else {},
        ))
    return AuditPage(entries=out, total=len(out))


class ChainVerifyResponse(BaseModel):
    rows_verified: int
    continuous: bool
    failure_reason: str | None = None
    first_break_id: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None


@audit_explorer_router.get("/verify-chain", response_model=ChainVerifyResponse)
async def verify_audit_chain(
    since: datetime | None = None,
    until: datetime | None = None,
    workspace_id: UUID | None = None,
    limit: int = 100_000,
) -> ChainVerifyResponse:
    """On-demand audit-ledger hash-chain integrity verification.

    Walks rows in (created_at, id) order; every row[i+1].previous_hash
    must equal row[i].hash. Halts on the first break and surfaces the
    offending audit_id.

    Recommended cadence:
      - nightly cron against the prior 24 h
      - smoke test after a suspicious deploy
      - back-validate before a Phase 0 cold-tier archive
    """
    from app.audit.chain_verify import verify_chain_window
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )
    limit = max(1, min(limit, 1_000_000))
    async with pool.acquire() as conn:
        result = await verify_chain_window(
            conn,
            since=since,
            until=until,
            workspace_id_scope=str(workspace_id) if workspace_id else None,
            limit=limit,
        )
    return ChainVerifyResponse(
        rows_verified=result.rows_verified,
        continuous=result.continuous,
        failure_reason=result.failure_reason,
        first_break_id=result.first_break_id,
        window_start=result.window_start,
        window_end=result.window_end,
    )


# ── Saved map views (Tier 4) ───────────────────────────────────────


saved_maps_router = APIRouter(
    prefix="/api/v1/admin/saved-maps",
    tags=["saved-maps"],
    dependencies=[Depends(verify_service_key)],
)


class SavedMapView(BaseModel):
    view_id: str
    workspace_id: str
    project_id: str | None = None
    name: str
    payload: dict[str, Any]
    created_at: datetime


class SavedMapList(BaseModel):
    views: list[SavedMapView]
    total: int


@saved_maps_router.get("", response_model=SavedMapList)
async def list_saved_maps(workspace_id: UUID | None = None) -> SavedMapList:
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )
    where = "WHERE TRUE"
    params: list[Any] = []
    if workspace_id is not None:
        where += " AND workspace_id = $1::uuid"
        params.append(str(workspace_id))

    async with pool.acquire() as conn:
        # Check the table exists; if not, return empty.
        exists = await conn.fetchval(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = 'silver' AND table_name = 'saved_map_views'",
        )
        if not exists:
            return SavedMapList(views=[], total=0)
        rows = await conn.fetch(
            f"""
            SELECT view_id::text       AS view_id,
                   workspace_id::text  AS workspace_id,
                   project_id::text    AS project_id,
                   name                AS name,
                   view_state          AS payload,
                   created_at          AS created_at
              FROM silver.saved_map_views
             {where}
             ORDER BY created_at DESC
            """,
            *params,
        )
    out: list[SavedMapView] = []
    for r in rows:
        payload = r["payload"] or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}
        out.append(SavedMapView(
            view_id=r["view_id"],
            workspace_id=r["workspace_id"],
            project_id=r["project_id"],
            name=r["name"],
            payload=payload if isinstance(payload, dict) else {},
            created_at=r["created_at"],
        ))
    return SavedMapList(views=out, total=len(out))


# ---------------------------------------------------------------------------
# Alerts inbox — audit-anchored alerts surface (cost burn, vllm security, etc.)
# ---------------------------------------------------------------------------
alerts_router = APIRouter(
    prefix="/api/v1/admin/alerts-inbox",
    tags=["alerts-inbox"],
    dependencies=[Depends(verify_service_key)],
)


class AlertItem(BaseModel):
    audit_id: str
    action_type: str
    workspace_id: str | None = None
    actor_id: int | None = None
    actor_kind: str | None = None
    target_schema: str | None = None
    target_table: str | None = None
    target_id: str | None = None
    severity: str | None = None
    payload: dict[str, Any]
    created_at: datetime
    acknowledged_at: datetime | None = None
    acknowledged_by_user_id: int | None = None


class AlertList(BaseModel):
    items: list[AlertItem]
    total: int


@alerts_router.get("", response_model=AlertList)
async def list_alerts(
    limit: int = 100,
    offset: int = 0,
    include_acknowledged: bool = False,
    workspace_id: UUID | None = None,
    action_type_prefix: str | None = None,
) -> AlertList:
    """List recent audit-ledger alert events, newest first.

    Sources:
      - cost.burn.alert       (LLM spend > threshold per hour/workspace)
      - vllm_security.alert   (Phase 0 §29 vLLM security gate)
      - *.alert               (any future alert convention)

    Alerts are acknowledged by writing a `<action_type>.acknowledged`
    counter-row keyed on the same target_id — that's a follow-on ticket;
    for now `acknowledged_at` is left null and operators can dismiss in
    the UI without server persistence (read-only inbox).
    """
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )

    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    # Build the WHERE clauses dynamically so we don't need a NULL-OR
    # construct in PG that the planner has to defeat.
    where_clauses = ["a.action_type LIKE '%.alert'"]
    where_clauses.append("($1::boolean OR ack.created_at IS NULL)")
    params: list[Any] = [include_acknowledged]
    next_idx = 2
    if workspace_id is not None:
        where_clauses.append(f"a.workspace_id = ${next_idx}::uuid")
        params.append(str(workspace_id))
        next_idx += 1
    if action_type_prefix:
        where_clauses.append(f"a.action_type LIKE ${next_idx} || '%'")
        params.append(action_type_prefix)
        next_idx += 1
    params.extend([limit, offset])

    sql = f"""
        SELECT a.id::text               AS audit_id,
               a.action_type            AS action_type,
               a.workspace_id::text     AS workspace_id,
               a.actor_id               AS actor_id,
               a.actor_kind             AS actor_kind,
               a.target_schema          AS target_schema,
               a.target_table           AS target_table,
               a.target_id              AS target_id,
               a.payload                AS payload,
               a.created_at             AS created_at,
               ack.created_at           AS acknowledged_at,
               ack.actor_id             AS acknowledged_by_user_id
          FROM audit.audit_ledger a
     LEFT JOIN LATERAL (
              SELECT created_at, actor_id
                FROM audit.audit_ledger a2
               WHERE a2.action_type = a.action_type || '.acknowledged'
                 AND a2.target_id   = a.target_id
               ORDER BY a2.created_at DESC
               LIMIT 1
          ) ack ON TRUE
         WHERE {' AND '.join(where_clauses)}
         ORDER BY a.created_at DESC
         LIMIT ${next_idx} OFFSET ${next_idx + 1}
    """
    # Total-count query: rebuild from scratch with its own placeholder
    # numbering so we don't have to rewrite the listing's clauses.
    count_clauses: list[str] = ["a.action_type LIKE '%.alert'"]
    count_params: list[Any] = []
    pi = 1
    if workspace_id is not None:
        count_clauses.append(f"a.workspace_id = ${pi}::uuid")
        count_params.append(str(workspace_id))
        pi += 1
    if action_type_prefix:
        count_clauses.append(f"a.action_type LIKE ${pi} || '%'")
        count_params.append(action_type_prefix)
        pi += 1
    if not include_acknowledged:
        count_clauses.append("""NOT EXISTS (
              SELECT 1 FROM audit.audit_ledger a2
               WHERE a2.action_type = a.action_type || '.acknowledged'
                 AND a2.target_id   = a.target_id
        )""")
    count_sql = f"""
        SELECT COUNT(*) AS n
          FROM audit.audit_ledger a
         WHERE {' AND '.join(count_clauses)}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *params)
        total = await conn.fetchval(count_sql, *count_params)

    items: list[AlertItem] = []
    for r in rows:
        payload = r["payload"] or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:  # noqa: BLE001
                payload = {}
        severity = None
        if isinstance(payload, dict):
            sev = payload.get("severity")
            if isinstance(sev, str):
                severity = sev
        items.append(AlertItem(
            audit_id=r["audit_id"],
            action_type=r["action_type"],
            workspace_id=r["workspace_id"],
            actor_id=r["actor_id"],
            actor_kind=r["actor_kind"],
            target_schema=r["target_schema"],
            target_table=r["target_table"],
            target_id=r["target_id"],
            severity=severity,
            payload=payload if isinstance(payload, dict) else {},
            created_at=r["created_at"],
            acknowledged_at=r["acknowledged_at"],
            acknowledged_by_user_id=r["acknowledged_by_user_id"],
        ))
    return AlertList(items=items, total=int(total or 0))


class AcknowledgeAlert(BaseModel):
    """Pydantic-validated. audit_id must be a syntactically valid UUID
    so a malformed input returns 422 instead of 500 from the SQL layer."""
    audit_id: UUID
    actor_id: int


@alerts_router.post("/acknowledge", status_code=status.HTTP_201_CREATED)
async def acknowledge_alert(req: AcknowledgeAlert) -> dict[str, Any]:
    """Write a `<action_type>.acknowledged` counter-row to ack an alert.

    Acks are themselves audit-anchored — both the alert and the ack are
    immutable audit rows, so the timeline is reconstructable. Operators
    typically include a short rationale in payload.note (optional).
    """
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "pg_pool not initialised",
        )

    audit_id_str = str(req.audit_id)
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT action_type, workspace_id::text AS workspace_id,
                   target_schema, target_table, target_id
              FROM audit.audit_ledger
             WHERE id = $1::uuid
             LIMIT 1
            """,
            audit_id_str,
        )
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "alert not found")
    if not row["action_type"].endswith(".alert"):
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"audit_id={audit_id_str} is not a *.alert row",
        )

    ack_action = f"{row['action_type']}.acknowledged"
    from app.audit import emit_audit
    async with pool.acquire() as conn:
        await emit_audit(
            conn,
            action_type=ack_action,
            # workspace_id mirrors the alert's. NULL is fine — audit_ledger.workspace_id is nullable.
            workspace_id=row["workspace_id"] or None,
            actor_id=req.actor_id,
            actor_kind="user",
            target_schema=row["target_schema"] or "audit",
            target_table=row["target_table"] or "audit_ledger",
            target_id=row["target_id"] or audit_id_str,
            payload={"original_audit_id": audit_id_str},
        )
    return {"ok": True, "acknowledged_action": ack_action}


# ---------------------------------------------------------------------------
# Phase H4 surface health — single endpoint summarising every dependency
# that the Phase H4 admin pages need. Cheap (mostly metadata queries) so it
# can be polled from a status dashboard or a deploy-validation CI step.
# ---------------------------------------------------------------------------
phase_h4_health_router = APIRouter(
    prefix="/api/v1/admin/phase-h4-health",
    tags=["phase-h4-health"],
    dependencies=[Depends(verify_service_key)],
)


class PhaseH4Check(BaseModel):
    name: str
    ok: bool
    detail: str | None = None


class PhaseH4Health(BaseModel):
    ok: bool
    checks: list[PhaseH4Check]
    timestamp: datetime


@phase_h4_health_router.get("", response_model=PhaseH4Health)
async def phase_h4_health() -> PhaseH4Health:
    """Composite health check for every Phase H4 dependency.

    Returns ok=true only when every named check passes. Each individual
    check carries its own ok + detail so a status dashboard can pinpoint
    the broken surface.

    Checks performed:
      - pg_pool initialised
      - silver.qp_credentials present
      - silver.workspace_settings present (FORCE RLS enabled)
      - audit_ledger_alerts_idx exists
      - audit_ledger_acks_idx exists
      - FASTAPI_SERVICE_KEY env var present
    """
    from app.main import app
    checks: list[PhaseH4Check] = []

    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        checks.append(PhaseH4Check(name="pg_pool", ok=False, detail="not initialised"))
        return PhaseH4Health(
            ok=False, checks=checks, timestamp=datetime.now(),
        )
    checks.append(PhaseH4Check(name="pg_pool", ok=True))

    # Tables
    async with pool.acquire() as conn:
        for schema, table, label in [
            ("silver", "qp_credentials", "silver.qp_credentials"),
            ("silver", "workspace_settings", "silver.workspace_settings"),
            ("audit", "audit_ledger", "audit.audit_ledger"),
            ("targeting", "target_candidate_zones", "targeting.target_candidate_zones"),
        ]:
            exists = await conn.fetchval(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema = $1 AND table_name = $2",
                schema, table,
            )
            checks.append(PhaseH4Check(
                name=f"table:{label}",
                ok=bool(exists),
                detail=None if exists else "table missing — check migrations 100-102",
            ))

        # workspace_settings RLS forced
        rls = await conn.fetchrow(
            """
            SELECT relrowsecurity AS enabled, relforcerowsecurity AS forced
              FROM pg_class
             WHERE relname = 'workspace_settings'
               AND relnamespace = (SELECT oid FROM pg_namespace WHERE nspname='silver')
            """,
        )
        rls_ok = bool(rls and rls["enabled"] and rls["forced"])
        checks.append(PhaseH4Check(
            name="rls:silver.workspace_settings",
            ok=rls_ok,
            detail=None if rls_ok else "RLS not enabled+forced",
        ))

        # Partial indexes for alerts inbox
        idx_rows = await conn.fetch(
            """
            SELECT indexname FROM pg_indexes
             WHERE schemaname='audit'
               AND indexname IN ('audit_ledger_alerts_idx', 'audit_ledger_acks_idx')
            """,
        )
        found = {r["indexname"] for r in idx_rows}
        for expected in ("audit_ledger_alerts_idx", "audit_ledger_acks_idx"):
            ok = expected in found
            checks.append(PhaseH4Check(
                name=f"index:{expected}",
                ok=ok,
                detail=None if ok else "missing — run database/raw/phase0/102-phase-h4-alerts-index.sql",
            ))

    # Service key env var
    import os
    has_key = bool(os.environ.get("FASTAPI_SERVICE_KEY"))
    checks.append(PhaseH4Check(
        name="env:FASTAPI_SERVICE_KEY",
        ok=has_key,
        detail=None if has_key else "must match Laravel .env for the broadcast bridge",
    ))

    overall = all(c.ok for c in checks)
    return PhaseH4Health(ok=overall, checks=checks, timestamp=datetime.now())


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


# ---------------------------------------------------------------------------
# §10.6 — eval promotion-gate enforcer
# ---------------------------------------------------------------------------
eval_promotion_router = APIRouter(
    prefix="/api/v1/admin/eval",
    tags=["eval"],
    dependencies=[Depends(verify_service_key)],
)


class AssessPromotionRequest(BaseModel):
    workspace_id: UUID
    candidate_run_id: UUID
    baseline_run_id: UUID
    actor_user_id: int | None = None
    dry_run: bool = False


class AssessPromotionResponse(BaseModel):
    allow: bool
    workspace_id: str
    candidate_run_id: str
    baseline_run_id: str
    regression_threshold_pct: float
    blocking_sets: list[str]
    set_deltas: list[dict[str, Any]]
    regressions: list[dict[str, Any]]


@eval_promotion_router.post(
    "/assess-promotion",
    response_model=AssessPromotionResponse,
)
async def assess_promotion_endpoint(
    body: AssessPromotionRequest,
) -> AssessPromotionResponse:
    """§10.6 — assess whether a candidate eval run may be promoted.

    Compares ``candidate_run_id`` against ``baseline_run_id`` and
    blocks promotion if any per-question_set pass-rate regression
    exceeds the locked 5-percentage-point threshold.

    Emits an ``eval.promotion.{allowed,blocked}`` audit row by
    default; pass ``dry_run=true`` to skip the audit emission.
    """
    from app.main import app
    from app.services.eval.promotion_gate import assess_promotion

    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "pg_pool not initialised")

    try:
        assessment = await assess_promotion(
            pool,
            workspace_id=body.workspace_id,
            candidate_run_id=body.candidate_run_id,
            baseline_run_id=body.baseline_run_id,
            actor_user_id=body.actor_user_id,
            emit_audit_row=not body.dry_run,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    return AssessPromotionResponse(**assessment.to_dict())


# ---------------------------------------------------------------------------
# §10-v2 — eval runs listing + per-set summary (powers the trend + compare UI)
# ---------------------------------------------------------------------------
class EvalRunRow(BaseModel):
    run_id: str
    triggered_by: str
    question_set_filter: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    question_count: int
    pass_count: int
    fail_count: int
    regression_count: int
    blocks_promotion: bool


class EvalRunList(BaseModel):
    items: list[EvalRunRow]
    total: int


@eval_promotion_router.get("/runs", response_model=EvalRunList)
async def list_eval_runs(
    limit: int = 50,
    offset: int = 0,
    question_set: str | None = None,
    days: int | None = 30,
) -> EvalRunList:
    """§10-v2 — list recent eval runs for the dashboard trend chart.

    Pulls from ``eval.run_summaries``. Default window: last 30 days.
    Pass ``days=None`` (or 0) for the all-time view.
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
    if question_set:
        where.append(f"question_set_filter = ${pi}")
        params.append(question_set)
        pi += 1
    if days and days > 0:
        where.append(f"started_at >= now() - (${pi}::int || ' days')::interval")
        params.append(days)
        pi += 1
    where_sql = " AND ".join(where)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT run_id::text       AS run_id,
                   triggered_by, question_set_filter,
                   started_at, completed_at,
                   question_count, pass_count, fail_count,
                   regression_count, blocks_promotion
              FROM eval.run_summaries
             WHERE {where_sql}
             ORDER BY started_at DESC
             LIMIT ${pi} OFFSET ${pi + 1}
            """,
            *params, limit, offset,
        )
        total = await conn.fetchval(
            f"SELECT count(*) FROM eval.run_summaries WHERE {where_sql}",
            *params,
        )

    items = [
        EvalRunRow(
            run_id=r["run_id"],
            triggered_by=r["triggered_by"],
            question_set_filter=r["question_set_filter"],
            started_at=r["started_at"],
            completed_at=r["completed_at"],
            question_count=int(r["question_count"]),
            pass_count=int(r["pass_count"]),
            fail_count=int(r["fail_count"]),
            regression_count=int(r["regression_count"]),
            blocks_promotion=bool(r["blocks_promotion"]),
        )
        for r in rows
    ]
    return EvalRunList(items=items, total=int(total or 0))


class PerSetSummaryRow(BaseModel):
    question_set: str
    pass_count: int
    fail_count: int
    total_count: int
    pass_rate_pct: float


class PerSetSummaryResponse(BaseModel):
    run_id: str
    per_set: list[PerSetSummaryRow]


@eval_promotion_router.get(
    "/runs/{run_id}/per-set-summary",
    response_model=PerSetSummaryResponse,
)
async def per_set_summary(run_id: UUID) -> PerSetSummaryResponse:
    """§10-v2 — per-question_set pass/fail breakdown for one run.

    Powers the dashboard's per-set bar chart + the compare-runs
    side-by-side view.
    """
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "pg_pool not initialised")

    async with pool.acquire() as conn:
        # Confirm the run exists; 404 if not.
        exists = await conn.fetchval(
            "SELECT 1 FROM eval.run_summaries WHERE run_id = $1", run_id,
        )
        if not exists:
            # Also tolerate runs that only have results, no summary row
            exists = await conn.fetchval(
                "SELECT 1 FROM eval.run_results WHERE run_id = $1 LIMIT 1",
                run_id,
            )
        if not exists:
            raise HTTPException(404, f"run_id {run_id} not found")

        rows = await conn.fetch(
            """
            SELECT gq.question_set,
                   sum(CASE WHEN rr.passed THEN 1 ELSE 0 END)::int AS pass_count,
                   sum(CASE WHEN rr.passed THEN 0 ELSE 1 END)::int AS fail_count,
                   count(*)::int AS total_count
              FROM eval.run_results rr
              JOIN eval.golden_questions gq ON gq.question_id = rr.question_id
             WHERE rr.run_id = $1
             GROUP BY gq.question_set
             ORDER BY gq.question_set
            """,
            run_id,
        )

    per_set = [
        PerSetSummaryRow(
            question_set=r["question_set"],
            pass_count=int(r["pass_count"]),
            fail_count=int(r["fail_count"]),
            total_count=int(r["total_count"]),
            pass_rate_pct=round(
                (r["pass_count"] / r["total_count"] * 100.0)
                if r["total_count"] > 0 else 0.0,
                2,
            ),
        )
        for r in rows
    ]
    return PerSetSummaryResponse(run_id=str(run_id), per_set=per_set)


# ---------------------------------------------------------------------------
# §10-v2 — golden questions CRUD (powers the authoring UI)
# ---------------------------------------------------------------------------
eval_questions_router = APIRouter(
    prefix="/api/v1/admin/eval/questions",
    tags=["eval"],
    dependencies=[Depends(verify_service_key)],
)


_VALID_QUESTION_SETS = {
    "core_chat", "public_private_boundary", "numeric_grounding",
    "refusal_correctness", "target_recommendation", "report_section",
    "schema_mapping", "ocr_triage",
}
_VALID_DIFFICULTIES = {"easy", "medium", "hard"}
_VALID_STATUSES = {"draft", "active", "retired"}


class GoldenQuestion(BaseModel):
    question_id: str
    question_set: str
    question_text: str
    context_setup: dict[str, Any]
    expected_intent_class: str | None = None
    expected_citations: list[dict[str, Any]] = Field(default_factory=list)
    expected_entities: list[dict[str, Any]] = Field(default_factory=list)
    expected_numeric_values: list[dict[str, Any]] = Field(default_factory=list)
    expected_refusal: bool = False
    expected_refusal_reason: str | None = None
    expected_language_compliance: list[dict[str, Any]] = Field(default_factory=list)
    difficulty: str
    status: str
    authored_by_user_id: int
    authored_at: datetime
    reviewed_by_user_id: int | None = None
    reviewed_at: datetime | None = None


class GoldenQuestionList(BaseModel):
    items: list[GoldenQuestion]
    total: int


def _jsonb(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return value


def _row_to_question(r: Any) -> GoldenQuestion:
    return GoldenQuestion(
        question_id=str(r["question_id"]),
        question_set=r["question_set"],
        question_text=r["question_text"],
        context_setup=_jsonb(r["context_setup"], {}),
        expected_intent_class=r["expected_intent_class"],
        expected_citations=_jsonb(r["expected_citations"], []),
        expected_entities=_jsonb(r["expected_entities"], []),
        expected_numeric_values=_jsonb(r["expected_numeric_values"], []),
        expected_refusal=bool(r["expected_refusal"]),
        expected_refusal_reason=r["expected_refusal_reason"],
        expected_language_compliance=_jsonb(r["expected_language_compliance"], []),
        difficulty=r["difficulty"],
        status=r["status"],
        authored_by_user_id=int(r["authored_by_user_id"]),
        authored_at=r["authored_at"],
        reviewed_by_user_id=r["reviewed_by_user_id"],
        reviewed_at=r["reviewed_at"],
    )


@eval_questions_router.get("", response_model=GoldenQuestionList)
async def list_questions(
    limit: int = 50,
    offset: int = 0,
    question_set: str | None = None,
    status: str | None = None,
    search: str | None = None,
) -> GoldenQuestionList:
    """§10-v2 — paginated list of golden questions, optional filters."""
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "pg_pool not initialised")

    limit = max(1, min(limit, 500))
    offset = max(0, offset)

    where = ["TRUE"]
    params: list[Any] = []
    pi = 1
    if question_set:
        where.append(f"question_set = ${pi}")
        params.append(question_set); pi += 1
    if status:
        where.append(f"status = ${pi}")
        params.append(status); pi += 1
    if search:
        where.append(f"question_text ILIKE ${pi}")
        params.append(f"%{search}%"); pi += 1
    where_sql = " AND ".join(where)

    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT * FROM eval.golden_questions
             WHERE {where_sql}
             ORDER BY authored_at DESC, question_id
             LIMIT ${pi} OFFSET ${pi + 1}
            """,
            *params, limit, offset,
        )
        total = await conn.fetchval(
            f"SELECT count(*) FROM eval.golden_questions WHERE {where_sql}",
            *params,
        )

    return GoldenQuestionList(
        items=[_row_to_question(r) for r in rows],
        total=int(total or 0),
    )


@eval_questions_router.get("/{question_id}", response_model=GoldenQuestion)
async def get_question(question_id: UUID) -> GoldenQuestion:
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "pg_pool not initialised")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM eval.golden_questions WHERE question_id = $1",
            question_id,
        )
    if row is None:
        raise HTTPException(404, f"question {question_id} not found")
    return _row_to_question(row)


class UpsertQuestionRequest(BaseModel):
    question_set: str
    question_text: str
    context_setup: dict[str, Any] = Field(default_factory=dict)
    expected_intent_class: str | None = None
    expected_citations: list[dict[str, Any]] = Field(default_factory=list)
    expected_entities: list[dict[str, Any]] = Field(default_factory=list)
    expected_numeric_values: list[dict[str, Any]] = Field(default_factory=list)
    expected_refusal: bool = False
    expected_refusal_reason: str | None = None
    expected_language_compliance: list[dict[str, Any]] = Field(default_factory=list)
    difficulty: str = "medium"
    authored_by_user_id: int


def _validate_upsert(body: UpsertQuestionRequest) -> None:
    if body.question_set not in _VALID_QUESTION_SETS:
        raise HTTPException(
            400,
            f"question_set must be one of {sorted(_VALID_QUESTION_SETS)}",
        )
    if body.difficulty not in _VALID_DIFFICULTIES:
        raise HTTPException(
            400,
            f"difficulty must be one of {sorted(_VALID_DIFFICULTIES)}",
        )


@eval_questions_router.post("", response_model=GoldenQuestion)
async def create_question(body: UpsertQuestionRequest) -> GoldenQuestion:
    """§10-v2 — create a new golden question in draft status."""
    _validate_upsert(body)
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "pg_pool not initialised")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO eval.golden_questions
                (question_set, question_text, context_setup,
                 expected_intent_class, expected_citations,
                 expected_entities, expected_numeric_values,
                 expected_refusal, expected_refusal_reason,
                 expected_language_compliance, difficulty,
                 authored_by_user_id, status)
            VALUES ($1, $2, $3::jsonb,
                    $4, $5::jsonb, $6::jsonb, $7::jsonb,
                    $8, $9, $10::jsonb, $11, $12, 'draft')
            RETURNING *
            """,
            body.question_set, body.question_text,
            json.dumps(body.context_setup),
            body.expected_intent_class,
            json.dumps(body.expected_citations),
            json.dumps(body.expected_entities),
            json.dumps(body.expected_numeric_values),
            body.expected_refusal, body.expected_refusal_reason,
            json.dumps(body.expected_language_compliance),
            body.difficulty, body.authored_by_user_id,
        )
    return _row_to_question(row)


@eval_questions_router.put("/{question_id}", response_model=GoldenQuestion)
async def update_question(
    question_id: UUID, body: UpsertQuestionRequest,
) -> GoldenQuestion:
    """§10-v2 — update an existing question. Cannot edit retired ones."""
    _validate_upsert(body)
    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "pg_pool not initialised")

    async with pool.acquire() as conn:
        current = await conn.fetchrow(
            "SELECT status FROM eval.golden_questions WHERE question_id = $1",
            question_id,
        )
        if current is None:
            raise HTTPException(404, f"question {question_id} not found")
        if current["status"] == "retired":
            raise HTTPException(409, "retired questions cannot be edited")

        row = await conn.fetchrow(
            """
            UPDATE eval.golden_questions
               SET question_set = $2,
                   question_text = $3,
                   context_setup = $4::jsonb,
                   expected_intent_class = $5,
                   expected_citations = $6::jsonb,
                   expected_entities = $7::jsonb,
                   expected_numeric_values = $8::jsonb,
                   expected_refusal = $9,
                   expected_refusal_reason = $10,
                   expected_language_compliance = $11::jsonb,
                   difficulty = $12
             WHERE question_id = $1
             RETURNING *
            """,
            question_id, body.question_set, body.question_text,
            json.dumps(body.context_setup),
            body.expected_intent_class,
            json.dumps(body.expected_citations),
            json.dumps(body.expected_entities),
            json.dumps(body.expected_numeric_values),
            body.expected_refusal, body.expected_refusal_reason,
            json.dumps(body.expected_language_compliance),
            body.difficulty,
        )
    return _row_to_question(row)


class TransitionRequest(BaseModel):
    status: str  # draft | active | retired
    reviewer_user_id: int | None = None


@eval_questions_router.post(
    "/{question_id}/transition", response_model=GoldenQuestion,
)
async def transition_question(
    question_id: UUID, body: TransitionRequest,
) -> GoldenQuestion:
    """§10-v2 — flip a question's status; emits a golden_question audit row.

    Allowed transitions:
      draft   → active   (reviewer required, must differ from author)
      active  → retired
      draft   → retired  (reject without activating)
      retired → draft    (un-retire for re-edit)
    """
    if body.status not in _VALID_STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(_VALID_STATUSES)}")

    from app.main import app
    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "pg_pool not initialised")

    async with pool.acquire() as conn:
        current = await conn.fetchrow(
            "SELECT status, authored_by_user_id FROM eval.golden_questions WHERE question_id = $1",
            question_id,
        )
        if current is None:
            raise HTTPException(404, f"question {question_id} not found")

        # Activate requires a reviewer != author
        if body.status == "active":
            if body.reviewer_user_id is None:
                raise HTTPException(
                    400, "reviewer_user_id required to activate a question",
                )
            if body.reviewer_user_id == current["authored_by_user_id"]:
                raise HTTPException(
                    400, "reviewer must differ from author",
                )

        row = await conn.fetchrow(
            """
            UPDATE eval.golden_questions
               SET status = $2,
                   reviewed_by_user_id = COALESCE($3, reviewed_by_user_id),
                   reviewed_at = CASE WHEN $3 IS NOT NULL THEN now() ELSE reviewed_at END
             WHERE question_id = $1
             RETURNING *
            """,
            question_id, body.status, body.reviewer_user_id,
        )

    # Emit audit row (best-effort)
    try:
        from app.audit import emit_audit
        await emit_audit(
            pool,
            action_type=f"eval.golden_question.{body.status}",
            actor_id=body.reviewer_user_id or int(current["authored_by_user_id"]),
            actor_kind="user",
            target_schema="eval",
            target_table="golden_questions",
            target_id=str(question_id),
            payload={
                "previous_status": current["status"],
                "new_status": body.status,
            },
        )
    except Exception:
        logger.exception("transition_question: audit emission failed")

    return _row_to_question(row)


class DryRunResponse(BaseModel):
    question_id: str
    passed: bool
    failure_layer: str | None = None
    failure_detail: str | None = None
    latency_ms: int | None = None
    actual_payload: dict[str, Any] = Field(default_factory=dict)


@eval_questions_router.post(
    "/{question_id}/dry-run", response_model=DryRunResponse,
)
async def dry_run_question(question_id: UUID) -> DryRunResponse:
    """§10-v2 — run the single-question evaluator (synthetic stub today,
    real RAG once §04i graduates). Returns the same shape that lands in
    ``eval.run_results`` but does NOT persist a row.
    """
    from app.main import app
    from app.services.eval.workspace_evaluator import (
        QuestionRecord,
        evaluate_question,
    )

    pool = getattr(app.state, "pg_pool", None)
    if pool is None:
        raise HTTPException(503, "pg_pool not initialised")

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM eval.golden_questions WHERE question_id = $1",
            question_id,
        )
        if row is None:
            raise HTTPException(404, f"question {question_id} not found")

        record = QuestionRecord(
            question_id=row["question_id"],
            question_set=row["question_set"],
            question_text=row["question_text"],
            context_setup=_jsonb(row["context_setup"], {}),
            expected_intent_class=row["expected_intent_class"],
            expected_citations=_jsonb(row["expected_citations"], []),
            expected_entities=_jsonb(row["expected_entities"], []),
            expected_numeric_values=_jsonb(row["expected_numeric_values"], []),
            expected_refusal=bool(row["expected_refusal"]),
            expected_refusal_reason=row["expected_refusal_reason"],
            expected_language_compliance=_jsonb(row["expected_language_compliance"], []),
            difficulty=row["difficulty"],
        )
        result = await evaluate_question(conn, record)

    return DryRunResponse(
        question_id=str(question_id),
        passed=result.passed,
        failure_layer=result.failure_layer,
        failure_detail=result.failure_detail,
        latency_ms=result.latency_ms,
        actual_payload=result.actual_payload,
    )


__all__ = [
    "rec_router",
    "qp_router",
    "ws_members_router",
    "ws_settings_router",
    "audit_explorer_router",
    "saved_maps_router",
    "alerts_router",
    "phase_h4_health_router",
    "backups_router",
    "eval_promotion_router",
    "eval_questions_router",
]
