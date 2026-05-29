"""One-off backfill: workspace_id on Project / DrillHole / Report nodes.

Background
----------
Migration 2026-05-20 added a `workspace_id` property to the three
tenant-private Neo4j labels (Project, DrillHole, Report) and three
matching indices. New sync runs populate the property via
`ON CREATE SET … ON MATCH SET …` in `kg_sync.py`. This script does
the one-off fill for nodes that were created BEFORE the sync changes
landed.

Strategy
--------
For each label, walk the corresponding silver table in batches and
issue a `MATCH ... SET n.workspace_id = $workspace_id WHERE n.workspace_id IS NULL`
query against Neo4j. Idempotent: re-running the script after the
backfill completes is a no-op.

Why a batched cypher instead of bulk MATCH ... SET on Neo4j alone:
the workspace_id is in Postgres, not in Neo4j (that was the whole
problem). We pull each (project_id → workspace_id) pair from silver
and issue a targeted MATCH per project.

Run
---
    docker exec -it georag-fastapi python -m app.scripts.backfill_neo4j_workspace_id

The script prints a per-label summary at the end:
    Project   updated: N
    DrillHole updated: N
    Report    updated: N
    Total     elapsed: Xs
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

import asyncpg
from neo4j import AsyncGraphDatabase

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("backfill_workspace_id")


def _pg_dsn() -> str:
    host = os.environ.get("POSTGRES_DIRECT_HOST", "postgresql")
    port = os.environ.get("POSTGRES_DIRECT_PORT", "5432")
    db = os.environ["POSTGRES_DB"]
    user = os.environ["GEORAG_APP_USER"]
    pwd = os.environ["GEORAG_APP_PASSWORD"]
    return f"postgres://{user}:{pwd}@{host}:{port}/{db}"


def _neo4j_uri() -> str:
    host = os.environ.get("NEO4J_HOST", "neo4j")
    port = os.environ.get("NEO4J_PORT", "7687")
    return f"bolt://{host}:{port}"


async def backfill() -> None:
    pg = await asyncpg.connect(_pg_dsn())
    n4_driver = AsyncGraphDatabase.driver(
        _neo4j_uri(),
        auth=(os.environ["NEO4J_USER"], os.environ["NEO4J_PASSWORD"]),
    )
    started = time.monotonic()
    counts: dict[str, int] = {"Project": 0, "DrillHole": 0, "Report": 0}

    try:
        # One pull from silver covers every Project. The workspace_id is
        # the same for all child nodes of that project, so the same map
        # services DrillHole + Report.
        projects = await pg.fetch(
            """
            SELECT project_id::text AS project_id,
                   workspace_id::text AS workspace_id
              FROM silver.projects
             WHERE workspace_id IS NOT NULL
            """
        )
        logger.info(
            "backfill: loaded %d projects from silver", len(projects)
        )

        async with n4_driver.session() as session:
            for row in projects:
                project_id = row["project_id"]
                workspace_id = row["workspace_id"]

                # Project node (one per project).
                res = await session.run(
                    """
                    MATCH (p:Project {project_id: $project_id})
                    WHERE p.workspace_id IS NULL
                    SET p.workspace_id = $workspace_id
                    RETURN count(p) AS updated
                    """,
                    project_id=project_id, workspace_id=workspace_id,
                )
                rec = await res.single()
                counts["Project"] += int((rec or {}).get("updated") or 0)

                # DrillHole nodes — keyed by project_id.
                res = await session.run(
                    """
                    MATCH (h:DrillHole {project_id: $project_id})
                    WHERE h.workspace_id IS NULL
                    SET h.workspace_id = $workspace_id
                    RETURN count(h) AS updated
                    """,
                    project_id=project_id, workspace_id=workspace_id,
                )
                rec = await res.single()
                counts["DrillHole"] += int((rec or {}).get("updated") or 0)

                # Report nodes — keyed by project_id.
                res = await session.run(
                    """
                    MATCH (r:Report {project_id: $project_id})
                    WHERE r.workspace_id IS NULL
                    SET r.workspace_id = $workspace_id
                    RETURN count(r) AS updated
                    """,
                    project_id=project_id, workspace_id=workspace_id,
                )
                rec = await res.single()
                counts["Report"] += int((rec or {}).get("updated") or 0)

    finally:
        await pg.close()
        await n4_driver.close()

    elapsed = time.monotonic() - started
    logger.info(
        "backfill complete: Project=%d DrillHole=%d Report=%d in %.1fs",
        counts["Project"], counts["DrillHole"], counts["Report"], elapsed,
    )

    # Surface a non-zero exit if no nodes were touched — usually that
    # means the script was run twice (first run already idempotent-filled).
    if sum(counts.values()) == 0:
        logger.info("(no rows updated — already backfilled or graph empty)")


if __name__ == "__main__":
    try:
        asyncio.run(backfill())
    except KeyboardInterrupt:
        sys.exit(130)
