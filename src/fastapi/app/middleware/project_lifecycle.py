"""Project lifecycle guard — CC-03 Item 8.

Provides ``require_active_project``, a lightweight async helper that
checks the ``lifecycle_state`` column on ``silver.projects`` before
allowing ingest, AI-query, or other mutable operations to proceed.

Design decisions
----------------
- Application-layer only.  RLS on ``silver.projects`` continues to
  filter by ``app.workspace_id`` (row visibility) — lifecycle checks
  are an *operational* concern, not a *data visibility* concern.  The
  migration comment on the column records this explicitly.

- Single SELECT per call.  The ``silver.projects`` table is small and
  the query is indexed on ``(workspace_id, lifecycle_state)`` added by
  the migration, so the check adds < 1 ms in practice.

- Raises ``HTTPException`` directly (FastAPI convention for route-level
  enforcement):
    hibernated → 403  detail="project_hibernated"
    archived   → 403  detail="project_archived"
    past_due   → 402  detail="project_past_due"

  These string detail values are the wire-contract consumed by Laravel's
  ``GeoRagService`` to surface the right user-facing error message.

- ``asyncpg.Connection`` typed argument so callers can pass a connection
  already inside the appropriate ``app.workspace_id`` GUC transaction.
  The helper does NOT open its own connection — the caller manages the
  connection lifecycle.

Usage example (in a route handler or Hatchet step)::

    async with deps.acquire_scoped() as conn:
        await require_active_project(project_id=body.project_id, conn=conn)
        # ... rest of handler
"""

from __future__ import annotations

import logging

import asyncpg
from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

# SQL — deliberately minimal: single column, no JOIN, uses the index
# added by migration 2026_05_30_000001_add_lifecycle_state_to_projects.
_SELECT_LIFECYCLE_SQL = """
SELECT lifecycle_state
FROM silver.projects
WHERE project_id = $1::uuid
"""


async def require_active_project(
    project_id: str,
    conn: asyncpg.Connection,
) -> None:
    """Assert that *project_id* has ``lifecycle_state = 'active'``.

    Raises
    ------
    HTTPException(403, "project_hibernated")
        Project is in the ``hibernated`` state.  Data is intact;
        reactivation is instant.  Block ingest + AI queries.
    HTTPException(403, "project_archived")
        Project is permanently archived.  Data is intact but the
        project is end-of-life.
    HTTPException(402, "project_past_due")
        Project access is suspended due to a payment lapse.
    HTTPException(404, "project_not_found")
        No row in ``silver.projects`` matched *project_id*.  Callers
        should treat this the same as a 403 from a user-facing
        perspective; the distinct code helps server-side diagnostics.

    Returns
    -------
    None
        Silent return when the project is ``active``.
    """
    row = await conn.fetchrow(_SELECT_LIFECYCLE_SQL, project_id)

    if row is None:
        logger.warning(
            "require_active_project: project_id=%s not found in silver.projects",
            project_id,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="project_not_found",
        )

    state: str = row["lifecycle_state"]

    if state == "active":
        return

    logger.info(
        "require_active_project: blocking operation — project=%s state=%s",
        project_id,
        state,
    )

    if state == "hibernated":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="project_hibernated",
        )

    if state == "archived":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="project_archived",
        )

    if state == "past_due":
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail="project_past_due",
        )

    # Unknown state — fail closed; surface as 403 so callers handle it
    # gracefully.  This branch should never be reached because the DB
    # CHECK constraint rejects unexpected values, but defensive coding
    # is preferable to a 500 if the constraint is somehow bypassed.
    logger.error(
        "require_active_project: unknown lifecycle_state=%r for project=%s; "
        "failing closed with 403",
        state,
        project_id,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"project_lifecycle_unknown:{state}",
    )
