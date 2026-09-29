"""Plan §2g — project bounding-box geometry supplier.

When a spatial query has no caller-supplied geometry, we fall back
to the active project's bounding box computed from its collars.
This is intentionally narrow — we don't want to invent geometries,
but a "what's near this project" query SHOULD still work without
the user drawing a polygon.

Two strategies (try in order):

  1. **silver.projects.geom_boundary** — the envelope of the project's
     declared boundary polygon (EPSG:4326), when one is set. Cheap PK
     lookup.
  2. **silver.collars envelope** — `ST_Envelope(ST_Collect(geom_4326))`
     for the project's collars when there's no boundary. Bounded by
     `LIMIT 500` collars to keep the query fast.

Database audit 2026-09-29 PG-11: this read `silver.projects.bbox` and
`silver.collars.collar_geom`, neither of which exists, and swallowed the
first UndefinedColumn *inside the transaction* — which aborts it, so the
fallback query could never run either. Both columns are now the real
ones, and the first lookup runs in its own savepoint so a failure there
genuinely falls through.

GIS-16 (audit 2026-09-29): this used to read ``silver.projects.bbox`` and
``silver.collars.collar_geom``. Neither column exists, so both queries
raised, the error was swallowed, and the supplier always returned None.
Both now read real 4326 columns (``collars.geom``, the SRID-32613 twin, was
retired 2026-09-29).

Returns a WKT polygon string or None when neither path resolves
(no boundary, no collars, DB error). The §2g tool refuses to
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
# SQL — try the project boundary first, then envelope from collars
# ---------------------------------------------------------------------------


_BBOX_FROM_PROJECT_COLUMN = """
    SELECT ST_AsText(ST_Envelope(geom_boundary)) AS wkt
    FROM silver.projects
    WHERE project_id = $1::uuid
      AND geom_boundary IS NOT NULL
    LIMIT 1
"""


_BBOX_FROM_COLLARS_ENVELOPE = """
    WITH project_collars AS (
        SELECT geom_4326
        FROM silver.collars
        WHERE project_id = $1::uuid
          AND geom_4326 IS NOT NULL
        LIMIT 500
    )
    SELECT ST_AsText(ST_Envelope(ST_Collect(geom_4326))) AS wkt
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
          - silver.projects.geom_boundary is NULL for this project
            AND silver.collars has no located collars for this project
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
                from app.db import bind_workspace_scope  # noqa: PLC0415
                await bind_workspace_scope(
                conn, workspace_id=workspace_id, site="agent.project_geometry"
            )
                # Project boundary first (cheap PK hit). Its own savepoint:
                # a failed statement aborts the enclosing transaction, so
                # without one the envelope query below could never run
                # after an error here.
                bbox_wkt: str | None = None
                try:
                    async with conn.transaction():
                        row = await conn.fetchrow(
                            _BBOX_FROM_PROJECT_COLUMN, project_id,
                        )
                    if row is not None and row["wkt"]:
                        bbox_wkt = row["wkt"]
                except Exception:
                    logger.warning(
                        "get_project_bbox_wkt: project boundary lookup "
                        "failed; falling back to collars envelope",
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
