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

  GET /internal/projects/{project_id}/collars
      Returns a list of CollarRead records with easting/northing/elevation.
      The agent's query_spatial_drill_holes tool uses this to build GeoJSON
      FeatureCollection payloads (MapPayload) and to ground spatial claims.
      Same project_user access check applies before the collar query.

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
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.models.geological import CollarRead, ProjectRead
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


# ---------------------------------------------------------------------------
# GET /projects/{project_id}/collars
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_id}/collars",
    response_model=list[CollarRead],
    summary="List drill-hole collars for a project",
    description=(
        "Returns collar records with spatial coordinates (easting, northing, elevation). "
        "The agent's spatial tools use this to build GeoJSON FeatureCollections and to "
        "ground distance and spatial proximity claims. "
        "Requires the caller to be a member of the project via project_user."
    ),
)
async def get_project_collars(
    project_id: UUID,
    request: Request,
    user: UserContext = Depends(extract_user_context),
    limit: Annotated[int, Query(ge=1, le=2000, description="Maximum number of collars to return")] = 500,
    offset: Annotated[int, Query(ge=0, description="Pagination offset")] = 0,
    status_filter: Annotated[
        str | None,
        Query(
            alias="status",
            description="Filter by hole status: Active | Completed | Abandoned",
            pattern="^(Active|Completed|Abandoned)$",
        ),
    ] = None,
) -> list[CollarRead]:
    """GET /internal/projects/{project_id}/collars — fetch collars from PostGIS.

    Extracts easting and northing from the PostGIS geography(Point) column via
    ST_X / ST_Y.  Results are ordered by hole_id for deterministic pagination.

    Notes on the PostGIS geometry column
    -------------------------------------
    The collars table stores location as a PostGIS geography(Point) column.
    ST_X / ST_Y extract easting and northing in the project CRS.  The agent
    tool that builds GeoJSON for MapPayload converts these to WGS-84 lon/lat
    using ST_Transform when the project CRS is not already WGS-84.
    """
    from app.config import settings  # noqa: PLC0415

    async with request.app.state.pg_pool.acquire() as conn:
        await _assert_project_access(conn, project_id, user, settings.TIMEOUT_POSTGIS_S)

        # Build the WHERE clause and params list dynamically so we only add
        # the status predicate when the filter is provided — avoids a
        # placeholder mismatch.
        params: list[object] = [project_id, limit, offset]
        where_clause = "WHERE c.project_id = $1"
        if status_filter is not None:
            where_clause += " AND c.status = $4"
            params.append(status_filter)

        rows = await asyncio.wait_for(
            conn.fetch(
                f"""
                SELECT collar_id,
                       hole_id,
                       project_id,
                       ST_X(location::geometry) AS easting,
                       ST_Y(location::geometry) AS northing,
                       elevation,
                       total_depth,
                       hole_type,
                       azimuth,
                       dip,
                       drill_date,
                       status
                FROM geo.collars c
                {where_clause}
                ORDER BY hole_id
                LIMIT $2 OFFSET $3
                """,
                *params,
            ),
            timeout=settings.TIMEOUT_POSTGIS_S,
        )

    logger.debug(
        "get_project_collars: returning collars",
        extra={
            "project_id": str(project_id),
            "count": len(rows),
            "limit": limit,
            "offset": offset,
            "status_filter": status_filter,
        },
    )
    return [CollarRead(**dict(row)) for row in rows]
