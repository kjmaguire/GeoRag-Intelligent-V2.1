"""Plan §2g — project bounding-box geometry supplier.

When a spatial query has no caller-supplied geometry, we fall back
to the active project's bounding box computed from its collars.
This is intentionally narrow — we don't want to invent geometries,
but a "what's near this project" query SHOULD still work without
the user drawing a polygon.

Two strategies (try in order):

  1. **silver.projects.bbox** — if the project has a pre-computed
     bbox column. Cheap PK lookup.
  2. **silver.collars envelope** — `ST_Envelope(ST_Union(collar_geom))`
     for the project's collars when there's no pre-computed bbox.
     Bounded by `LIMIT 500` collars to keep the query fast.

Returns a WKT polygon string or None when neither path resolves
(no collars, no bbox column, DB error). The §2g tool refuses to
invent geometries — None from this supplier means the spatial
query is skipped, not auto-widened.

Pure-async; sets ``app.workspace_id`` GUC for RLS.
"""

from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger(__name__)


__all__ = [
    "get_project_bbox_wkt",
]


# ---------------------------------------------------------------------------
# SQL — try bbox column first, then envelope from collars
# ---------------------------------------------------------------------------


_BBOX_FROM_PROJECT_COLUMN = """
    SELECT ST_AsText(bbox) AS wkt
    FROM silver.projects
    WHERE project_id = $1::uuid
      AND bbox IS NOT NULL
    LIMIT 1
"""


_BBOX_FROM_COLLARS_ENVELOPE = """
    WITH project_collars AS (
        SELECT collar_geom
        FROM silver.collars
        WHERE project_id = $1::uuid
          AND collar_geom IS NOT NULL
        LIMIT 500
    )
    SELECT ST_AsText(ST_Envelope(ST_Collect(collar_geom))) AS wkt
    FROM project_collars
"""


async def get_project_bbox_wkt(
    pool: Any,
    *,
    workspace_id: str,
    project_id: str,
) -> str | None:
    """Return a WKT polygon for the project's bounding box, or None.

    Workspace tenancy: every query sets `app.workspace_id` GUC
    inside the transaction so RLS applies on silver.collars +
    silver.projects.

    Args:
        pool: asyncpg.Pool-like.
        workspace_id: REQUIRED — sets the GUC. Raises ValueError if empty.
        project_id: UUID string of the active project.

    Returns:
        WKT polygon string or None. None when:
          - silver.projects has no bbox column populated for this project
            AND silver.collars has no collars for this project
          - The DB lookup raises (logged + swallowed; spatial query
            should skip rather than crash)
    """
    if not workspace_id:
        raise ValueError("workspace_id is required (sets app.workspace_id)")
    if not project_id:
        return None

    try:
        async with pool.acquire() as conn:
            async with conn.transaction():
                await bind_workspace_scope(
                conn, workspace_id=workspace_id, site="agent.project_geometry"
            )
                # Try the cached bbox column first (cheap PK hit).
                # silver.projects MAY not have a `bbox` column on
                # every deployment — catch the UndefinedColumn error
                # and fall through to the envelope path.
                bbox_wkt: str | None = None
                try:
                    row = await conn.fetchrow(
                        _BBOX_FROM_PROJECT_COLUMN, project_id,
                    )
                    if row is not None and row["wkt"]:
                        bbox_wkt = row["wkt"]
                except Exception:
                    # UndefinedColumn or schema drift — try envelope path.
                    logger.debug(
                        "get_project_bbox_wkt: silver.projects.bbox not "
                        "available; falling back to collars envelope",
                        exc_info=True,
                    )
                if bbox_wkt:
                    return bbox_wkt

                # Envelope-from-collars fallback.
                row = await conn.fetchrow(
                    _BBOX_FROM_COLLARS_ENVELOPE, project_id,
                )
                if row is not None and row["wkt"]:
                    return row["wkt"]
    except Exception:
        logger.warning(
            "get_project_bbox_wkt: pg lookup failed for project_id=%s",
            project_id,
            exc_info=True,
        )

    return None
