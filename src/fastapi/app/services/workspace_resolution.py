"""Workspace resolution service — Module 9 Chunk 9.4 (A2-04).

Replaces the two identical _resolve_workspace_id helpers in evidence.py and
answer_runs.py with a single authoritative implementation that **never falls
back to a hardcoded default UUID**.

Resolution order
----------------
1. JWT carries ``workspace_id`` claim directly → use it.
2. JWT carries ``project_id`` only → SELECT workspace_id FROM silver.projects
   WHERE project_id = $1, cached in Redis for 5 minutes.
3. ``X-Workspace-Id`` header present AND SINGLE_TENANT_MODE=True (i.e.
   MULTI_TENANT_ENFORCEMENT_ENABLED=False) → use header value.
4. Else → HTTP 403 (cannot resolve workspace from request context).

If MULTI_TENANT_ENFORCEMENT_ENABLED=True AND a request supplies
X-Workspace-Id AND it differs from the JWT-derived workspace → HTTP 403
with a warning log.

No fallback to the default workspace UUID a0000000-0000-0000-0000-000000000001.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from fastapi import HTTPException, Request, status

from app.services.auth import UserContext

logger = logging.getLogger(__name__)

_REDIS_PROJ_TO_WS_PREFIX = "georag:proj_to_ws"
# 5-minute TTL — short enough to pick up project→workspace reassignments
# promptly, long enough that a request burst doesn't hammer PG. Project
# moves between workspaces are rare (essentially admin-only); a longer
# TTL would also be safe but 5 min matches the JWT TTL order of magnitude.
_PROJ_TO_WS_TTL_S = 300


async def _lookup_workspace_for_project(
    project_id: str,
    pg_pool: Any,
    redis_client: Any | None,
) -> UUID | None:
    """Return workspace_id for a project, using Redis as a read-through cache.

    Returns None if the project does not exist in silver.projects.
    """
    cache_key = f"{_REDIS_PROJ_TO_WS_PREFIX}:{project_id}"

    # 1. Try Redis cache first.
    if redis_client is not None:
        try:
            cached = await redis_client.get(cache_key)
            if cached is not None:
                value = cached.decode() if isinstance(cached, bytes) else cached
                return UUID(value)
        except Exception:
            logger.warning(
                "workspace_resolution: Redis cache read failed for key=%s",
                cache_key,
                exc_info=True,
            )

    # 2. DB lookup.
    if pg_pool is None:
        return None

    try:
        async with pg_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT workspace_id FROM silver.projects WHERE project_id = $1",
                UUID(project_id),
            )
    except Exception:
        logger.exception(
            "workspace_resolution: DB lookup failed for project_id=%s", project_id
        )
        return None

    if row is None:
        return None

    workspace_id = UUID(str(row["workspace_id"]))

    # 3. Write to Redis cache.
    if redis_client is not None:
        try:
            await redis_client.set(cache_key, str(workspace_id), ex=_PROJ_TO_WS_TTL_S)
        except Exception:
            logger.warning(
                "workspace_resolution: Redis cache write failed for key=%s",
                cache_key,
                exc_info=True,
            )

    return workspace_id


async def resolve_workspace_id(
    user: UserContext,
    request: Request,
    pg_pool: Any,
    redis_client: Any | None = None,
) -> UUID:
    """Resolve workspace UUID from the request context.

    See module docstring for resolution order. Raises HTTP 403 if the
    workspace cannot be determined. **Never** falls back to a hardcoded UUID.

    Parameters
    ----------
    user:
        Populated UserContext from extract_user_context.
    request:
        FastAPI Request object (for header access).
    pg_pool:
        asyncpg Pool — used for project → workspace DB lookup when the
        JWT lacks a direct workspace_id claim.
    redis_client:
        Optional aioredis client for caching the DB lookup result.
    """
    from app.config import settings  # noqa: PLC0415

    # ── Step 1: JWT workspace_id claim (most authoritative) ────────────
    jwt_workspace_id: UUID | None = None
    if user.workspace_id:
        try:
            jwt_workspace_id = UUID(user.workspace_id)
        except ValueError:
            logger.warning(
                "workspace_resolution: JWT workspace_id is not a valid UUID: %s",
                user.workspace_id,
            )

    # ── Step 2: Derive from JWT project_id via DB lookup ──────────────
    if jwt_workspace_id is None and user.project_id:
        jwt_workspace_id = await _lookup_workspace_for_project(
            user.project_id, pg_pool, redis_client
        )
        if jwt_workspace_id is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot resolve workspace: project not found for JWT project_id",
            )

    # ── Step 3: X-Workspace-Id header (single-tenant escape hatch) ────
    header_workspace_id: UUID | None = None
    workspace_header = request.headers.get("X-Workspace-Id")
    if workspace_header:
        try:
            header_workspace_id = UUID(workspace_header)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="X-Workspace-Id is not a valid UUID",
            )

    # Cross-check: if multi-tenant enforcement is on and both sources are
    # present but disagree, that is a mismatch → 403.
    if (
        settings.MULTI_TENANT_ENFORCEMENT_ENABLED
        and jwt_workspace_id is not None
        and header_workspace_id is not None
        and jwt_workspace_id != header_workspace_id
    ):
        logger.warning(
            "workspace_resolution: X-Workspace-Id mismatch "
            "actor_user_id=%s header_workspace_id=%s jwt_workspace_id=%s",
            user.user_id,
            header_workspace_id,
            jwt_workspace_id,
        )
        # §10.12 — emit cross-workspace access alert (best-effort, fail-open).
        try:
            from app.services.cross_workspace_audit import (  # noqa: PLC0415
                emit_cross_workspace_alert,
            )
            actor_uid = (
                int(user.user_id)
                if user.user_id and str(user.user_id).isdigit()
                else None
            )
            await emit_cross_workspace_alert(
                pg_pool,
                actor_user_id=actor_uid,
                jwt_workspace_id=jwt_workspace_id,
                target_workspace_id=header_workspace_id,
                request_path=request.url.path,
                redis_client=redis_client,
            )
        except Exception:  # noqa: BLE001
            logger.exception(
                "workspace_resolution: cross_workspace alert emission failed"
            )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "X-Workspace-Id header does not match workspace derived from JWT. "
                "Use the JWT-derived workspace or omit the header."
            ),
        )

    # JWT-derived workspace is the authoritative path.
    if jwt_workspace_id is not None:
        return jwt_workspace_id

    # Header-only path: only permitted in single-tenant mode.
    if header_workspace_id is not None:
        if not settings.MULTI_TENANT_ENFORCEMENT_ENABLED:
            return header_workspace_id
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot resolve workspace from request context",
        )

    # ── Step 4: No source available → 403, never a hardcoded UUID ──────
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Cannot resolve workspace from request context",
    )
