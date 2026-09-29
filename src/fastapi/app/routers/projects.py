"""Project read endpoints — internal use by the Pydantic AI agent tools.

These endpoints expose project metadata and spatial collar data that the agent
tools query via PostGIS. They are NOT user-facing; Laravel calls them
server-side to assemble map payloads and to hydrate the agent's ProjectContext.

Routes
------
  GET /internal/projects/{project_id}
      Returns ProjectRead metadata for the given project UUID.
      Access is scoped to the authenticated user via the project_user pivot
      table (public.project_user).  A user with no pivot row for this project
      receives HTTP 403, not 404, to prevent enumeration.

  GET /internal/projects/{project_id}/collars was REMOVED 2026-09-29
  (API-7): it queried ``geo.collars`` and a ``location`` column, neither of
  which exists (the table is silver.collars, geometry column ``geom``), so
  every call 500'd — and nothing called it. The agent's spatial tools query
  PostGIS directly.

Architecture references
-----------------------
  Section 04e  — PostGIS schema (projects + collar table shapes)
  Section 05c  — async driver patterns and caching
  Section 06   — timeout values (TIMEOUT_POSTGIS_S)
  Section 07d  — Laravel<->FastAPI API surface
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.models.geological import ProjectRead
from app.services.auth import UserContext, extract_user_context, verify_service_key

logger = logging.getLogger(__name__)

router = APIRouter(
    tags=["projects"],
    dependencies=[Depends(verify_service_key)],
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _assert_project_access(
    conn,
    project_id: UUID,
    user: UserContext,
    timeout_s: float,
) -> None:
    """Raise HTTP 403 if the caller has no project_user row for this project.

    When the JWT user_id is absent (graceful rollout / service-key-only auth)
    the check is skipped so legacy callers are not broken.  This mirrors the
    same graceful-rollout pattern used in the queries router.

    We check the pivot before fetching project data so that a missing row
    returns 403 rather than 404 — 404 would allow enumeration of valid UUIDs.
    """
    if user.user_id is None:
        # No JWT — service-key-only call; skip user-level check.
        return

    row = await asyncio.wait_for(
        conn.fetchrow(
            """
            SELECT role
            FROM public.project_user
            WHERE user_id = $1
              AND project_id = $2
            """,
            int(user.user_id),
            project_id,
        ),
        timeout=timeout_s,
    )
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have access to this project.",
        )


# ---------------------------------------------------------------------------
# GET /projects/{project_id}
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_id}",
    response_model=ProjectRead,
    summary="Get project metadata",
    description=(
        "Returns full metadata for a project. "
        "Used by the agent to scope queries. "
        "Requires the caller to be a member of the project via project_user."
    ),
)
async def get_project(
    project_id: UUID,
    request: Request,
    user: UserContext = Depends(extract_user_context),
) -> ProjectRead:
    """GET /internal/projects/{project_id} — fetch project metadata from PostGIS.

    Queries silver.projects with an explicit column list (no SELECT *).
    The project_user pivot is checked first to enforce access control.
    Returns 403 when the caller has no project_user row (not 404, to prevent
    UUID enumeration).  Returns 404 only when the row genuinely does not exist
    after an access-allowed pivot check.
    """
    from app.config import settings  # noqa: PLC0415

    async with request.app.state.pg_pool.acquire() as conn:
        await _assert_project_access(conn, project_id, user, settings.TIMEOUT_POSTGIS_S)

        row = await asyncio.wait_for(
            conn.fetchrow(
                """
                SELECT project_id,
                       project_name,
                       crs_datum,
                       company,
                       magnetic_declination,
                       orientation_reference,
                       commodity,
                       region,
                       status,
                       slug,
                       created_at,
                       updated_at
                FROM silver.projects
                WHERE project_id = $1
                """,
                project_id,
            ),
            timeout=settings.TIMEOUT_POSTGIS_S,
        )

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project {project_id} not found.",
        )

    logger.debug(
        "get_project: returning project",
        extra={"project_id": str(project_id)},
    )
    return ProjectRead(**dict(row))
