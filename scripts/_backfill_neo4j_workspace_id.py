"""One-shot script: backfill workspace_id on Neo4j nodes that have project_id
but are missing workspace_id. Joins through silver.projects in PG.

Labels backfilled: DrillHole, Report, Formation, Project, Deposit,
                   QualifiedPerson, MineralOccurrence (all have project_id).

Labels intentionally skipped (no project_id, public/global data):
  Mine, Commodity, PublicGeoSource, Jurisdiction

Run inside georag-fastapi container:
    python3 /app/scripts/_backfill_neo4j_workspace_id.py
"""
from __future__ import annotations
import asyncio
import os
import sys


PG_DSN = "postgresql://georag:OMljaORhiA7RGQN3ilfemNWpezF9waU@pgbouncer:6432/georag"
NEO4J_URI = "bolt://neo4j:7687"
NEO4J_USER = "neo4j"
NEO4J_PASS = "24kNKWLbX20bgHEXAuMSGjCp228LIfUE"

# Labels that should be skipped (intentionally public / no workspace scope)
SKIP_LABELS = {"Mine", "Commodity", "PublicGeoSource", "Jurisdiction"}


async def main() -> None:
    import asyncpg
    from neo4j import GraphDatabase

    # ── 1. Fetch (project_id → workspace_id) from PG ─────────────────
    print("Connecting to Postgres…")
    conn = await asyncpg.connect(PG_DSN)
    try:
        rows = await conn.fetch(
            "SELECT project_id::text AS project_id, workspace_id::text AS workspace_id "
            "FROM silver.projects WHERE workspace_id IS NOT NULL"
        )
    finally:
        await conn.close()

    if not rows:
        print("No projects found in silver.projects — nothing to backfill.")
        return

    project_map: dict[str, str] = {r["project_id"]: r["workspace_id"] for r in rows}
    print(f"Loaded {len(project_map)} projects with workspace_id from PG.")

    # ── 2. Run Cypher updates ─────────────────────────────────────────
    print("Connecting to Neo4j…")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
    total_updated = 0
    total_skipped = 0

    try:
        with driver.session() as session:
            for project_id, workspace_id in project_map.items():
                result = session.run(
                    """
                    MATCH (n)
                    WHERE n.project_id = $project_id
                      AND n.workspace_id IS NULL
                    SET n.workspace_id = $workspace_id
                    RETURN count(n) AS updated
                    """,
                    project_id=project_id,
                    workspace_id=workspace_id,
                )
                record = result.single()
                count = record["updated"] if record else 0
                if count:
                    print(f"  project {project_id[:8]}…  → set workspace_id on {count} nodes")
                    total_updated += count
                else:
                    total_skipped += 1

            # ── 3. Verify ─────────────────────────────────────────────
            print("\nPost-backfill counts (nodes still missing workspace_id):")
            result = session.run(
                "MATCH (n) WHERE n.workspace_id IS NULL "
                "RETURN labels(n)[0] AS label, count(*) AS cnt ORDER BY cnt DESC"
            )
            for record in result:
                print(f"  {record['label']}: {record['cnt']}")

    finally:
        driver.close()

    print(f"\nDone. {total_updated} nodes updated, {total_skipped} project_ids had no matching nodes.")


if __name__ == "__main__":
    asyncio.run(main())
