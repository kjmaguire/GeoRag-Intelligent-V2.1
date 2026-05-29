"""Populate the Neo4j knowledge graph from silver tables + Qdrant.

This script is idempotent — it uses MERGE on all node creation so re-running
it updates properties rather than creating duplicates.

Entity types created (per Section 04e):
  Project              — from silver.projects / silver.reports
  DrillHole            — from silver.collars
  Formation            — from silver.lithology_logs (lithology codes as unit names)
  Report               — from silver.reports
  QualifiedPerson      — from silver.reports.authors
  Deposit              — extracted from NI 43-101 text (Triple R deposit)
  MineralOccurrence    — from silver.reports.commodity + NI 43-101 text

Relationships:
  (Project)-[:HAS_HOLE]->(DrillHole)
  (DrillHole)-[:HAS_LITHOLOGY]->(Formation)
  (Project)-[:HAS_REPORT]->(Report)
  (Report)-[:AUTHORED_BY]->(QualifiedPerson)
  (Project)-[:HOSTS]->(Deposit)
  (Deposit)-[:HAS_MINERALIZATION]->(MineralOccurrence)
  (DrillHole)-[:INTERSECTS]->(Formation)   # same as HAS_LITHOLOGY, kept for query variety
  (Report)-[:DESCRIBES]->(Deposit)

Usage:
    docker exec georag-fastapi python /app/scripts/populate_neo4j.py
"""

import asyncio
import logging
import os
import sys

import asyncpg
from neo4j import AsyncGraphDatabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config — same env vars as the FastAPI app
# ---------------------------------------------------------------------------

PG_DSN = os.environ.get(
    "DATABASE_URL",
    "postgresql://georag:georag_dev_password@pgbouncer:6432/georag",
)
NEO4J_URI = os.environ.get("NEO4J_URI", "bolt://neo4j:7687")
NEO4J_USER = os.environ.get("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("NEO4J_PASSWORD", "georag_neo4j_dev")

PROJECT_ID = "019d74a1-fba8-7165-9ae6-a5bf93eef97d"


async def main() -> None:
    logger.info("Connecting to PostgreSQL…")
    pg = await asyncpg.connect(PG_DSN)

    logger.info("Connecting to Neo4j…")
    driver = AsyncGraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))

    try:
        await _create_constraints(driver)
        await _populate_project(driver, pg)
        await _populate_drill_holes(driver, pg)
        await _populate_formations(driver, pg)
        await _populate_reports(driver, pg)
        await _populate_qps(driver, pg)
        await _populate_deposits(driver)
        await _populate_mineral_occurrences(driver)
        await _create_cross_relationships(driver, pg)

        # Final stats
        async with driver.session() as session:
            result = await session.run(
                "MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt "
                "ORDER BY cnt DESC"
            )
            records = await result.data()
            logger.info("=== Graph population complete ===")
            for r in records:
                logger.info("  %-25s %d nodes", r["label"], r["cnt"])

            result2 = await session.run(
                "MATCH ()-[r]->() RETURN type(r) AS rel, count(r) AS cnt "
                "ORDER BY cnt DESC"
            )
            rels = await result2.data()
            for r in rels:
                logger.info("  %-25s %d rels", r["rel"], r["cnt"])

    finally:
        await pg.close()
        await driver.close()

    logger.info("Done.")


# ---------------------------------------------------------------------------
# Constraints (idempotent)
# ---------------------------------------------------------------------------

async def _create_constraints(driver) -> None:
    """Idempotent constraint setup with explicit stable names.

    Neo4j review #6 — pre-existing deployments have anonymous constraints
    (auto-generated names like `constraint_14259a2a`) from earlier
    `CREATE CONSTRAINT FOR (n:X) REQUIRE n.y IS UNIQUE` runs without a
    name. This function:
      1. Drops the auto-generated names (no-op if they don't exist).
      2. Recreates with explicit stable names so future migrations can
         reference them portably across environments.
    The migration is safe: dropping a UNIQUE constraint takes an
    AccessExclusiveLock for microseconds; recreating immediately
    re-validates the existing data (succeeds because the data hasn't
    changed) and the constraint is back in place.
    """
    legacy_drops = [
        # Anonymous constraint names observed in dev — DROP IF EXISTS is
        # a no-op on environments where the migration already ran or
        # where these names were never generated.
        "DROP CONSTRAINT constraint_14259a2a IF EXISTS",
        "DROP CONSTRAINT constraint_399688bd IF EXISTS",
        "DROP CONSTRAINT constraint_4f72fbb8 IF EXISTS",
        "DROP CONSTRAINT constraint_72d8c66d IF EXISTS",
        "DROP CONSTRAINT constraint_e932d48f IF EXISTS",
    ]
    constraints = [
        # Each constraint carries a stable, descriptive name so:
        #   (a) future migrations can DROP/CREATE by that name portably
        #   (b) `SHOW CONSTRAINTS` output is human-readable
        #   (c) the index that backs each constraint inherits the name
        "CREATE CONSTRAINT project_id_unique IF NOT EXISTS "
        "FOR (p:Project) REQUIRE p.project_id IS UNIQUE",
        "CREATE CONSTRAINT drillhole_collar_id_unique IF NOT EXISTS "
        "FOR (d:DrillHole) REQUIRE d.collar_id IS UNIQUE",
        "CREATE CONSTRAINT formation_name_unique IF NOT EXISTS "
        "FOR (f:Formation) REQUIRE f.name IS UNIQUE",
        "CREATE CONSTRAINT report_id_unique IF NOT EXISTS "
        "FOR (r:Report) REQUIRE r.report_id IS UNIQUE",
        "CREATE CONSTRAINT qualified_person_name_unique IF NOT EXISTS "
        "FOR (q:QualifiedPerson) REQUIRE q.name IS UNIQUE",
        "CREATE CONSTRAINT deposit_name_unique IF NOT EXISTS "
        "FOR (d:Deposit) REQUIRE d.name IS UNIQUE",
        "CREATE CONSTRAINT mineral_occurrence_name_unique IF NOT EXISTS "
        "FOR (m:MineralOccurrence) REQUIRE m.name IS UNIQUE",
        # Eval 14 follow-up (2026-05-20) — workspace_id property indices
        # on tenant-private labels so post-MERGE filter queries
        # (`WHERE n.workspace_id = $ws`) hit an index, not a full scan.
        # Formation / Deposit / MineralOccurrence stay GLOBAL by design
        # (geological reference data shared across tenants — Kyle 2026-05-20).
        "CREATE INDEX project_workspace_id_idx IF NOT EXISTS "
        "FOR (p:Project) ON (p.workspace_id)",
        "CREATE INDEX drillhole_workspace_id_idx IF NOT EXISTS "
        "FOR (d:DrillHole) ON (d.workspace_id)",
        "CREATE INDEX report_workspace_id_idx IF NOT EXISTS "
        "FOR (r:Report) ON (r.workspace_id)",
        # Eval 14 R3 follow-up — full-text indices powering entity
        # disambiguation. The retrieval layer's `resolve_formation_term`
        # helper resolves natural-language terms like "the Athabasca
        # Sandstone" via CALL db.index.fulltext.queryNodes("formation_name_fts", …)
        # which is two orders of magnitude faster than CONTAINS scan.
        # Neo4j 5+ fulltext index syntax:
        "CREATE FULLTEXT INDEX formation_name_fts IF NOT EXISTS "
        "FOR (f:Formation) ON EACH [f.name, f.description]",
        "CREATE FULLTEXT INDEX deposit_name_fts IF NOT EXISTS "
        "FOR (d:Deposit) ON EACH [d.name]",
        "CREATE FULLTEXT INDEX mineral_occurrence_name_fts IF NOT EXISTS "
        "FOR (m:MineralOccurrence) ON EACH [m.name]",
    ]
    async with driver.session() as session:
        for stmt in legacy_drops:
            await session.run(stmt)
        for c in constraints:
            await session.run(c)
    logger.info(
        "Constraints ensured: dropped %d legacy + ensured %d stable-name",
        len(legacy_drops),
        len(constraints),
    )


# ---------------------------------------------------------------------------
# Project
# ---------------------------------------------------------------------------

async def _populate_project(driver, pg) -> None:
    row = await pg.fetchrow(
        "SELECT project_id::text, project_name FROM silver.projects WHERE project_id = $1",
        PROJECT_ID,
    )
    if row is None:
        logger.warning("Project %s not found in silver.projects", PROJECT_ID)
        return

    async with driver.session() as session:
        await session.run(
            "MERGE (p:Project {project_id: $pid}) "
            "SET p.name = $name",
            pid=row["project_id"],
            name=row["project_name"],
        )
    logger.info("Project node: %s", row["project_name"])


# ---------------------------------------------------------------------------
# Drillholes
# ---------------------------------------------------------------------------

async def _populate_drill_holes(driver, pg) -> None:
    rows = await pg.fetch(
        "SELECT collar_id::text, hole_id, total_depth, hole_type, status, "
        "drill_date::text, ST_X(geom) AS easting, ST_Y(geom) AS northing, "
        "elevation, azimuth, dip "
        "FROM silver.collars WHERE project_id = $1 ORDER BY hole_id",
        PROJECT_ID,
    )
    async with driver.session() as session:
        for r in rows:
            await session.run(
                "MERGE (d:DrillHole {collar_id: $cid}) "
                "SET d.hole_id = $hid, d.total_depth = $td, d.hole_type = $ht, "
                "    d.status = $st, d.drill_date = $dd, d.easting = $e, "
                "    d.northing = $n, d.elevation = $el, d.azimuth = $az, "
                "    d.dip = $dp, d.project_id = $pid, d.name = $hid",
                cid=r["collar_id"],
                hid=r["hole_id"],
                td=float(r["total_depth"]),
                ht=r["hole_type"],
                st=r["status"],
                dd=r["drill_date"],
                e=float(r["easting"]),
                n=float(r["northing"]),
                el=float(r["elevation"]),
                az=float(r["azimuth"]),
                dp=float(r["dip"]),
                pid=PROJECT_ID,
            )
        # HAS_HOLE relationships
        await session.run(
            "MATCH (p:Project {project_id: $pid}), (d:DrillHole {project_id: $pid}) "
            "MERGE (p)-[:HAS_HOLE]->(d)",
            pid=PROJECT_ID,
        )
    logger.info("DrillHole nodes: %d", len(rows))


# ---------------------------------------------------------------------------
# Formations (from lithology codes + proper names)
# ---------------------------------------------------------------------------

async def _populate_formations(driver, pg) -> None:
    # Get unique lithology codes from the project
    rows = await pg.fetch(
        "SELECT DISTINCT l.lithology_code, l.lithology_description "
        "FROM silver.lithology_logs l "
        "JOIN silver.collars c ON c.collar_id = l.collar_id "
        "WHERE c.project_id = $1 AND l.lithology_code IS NOT NULL "
        "ORDER BY l.lithology_code",
        PROJECT_ID,
    )

    # Also add the real geological formations from the NI 43-101 text
    extra_formations = [
        {"name": "Athabasca Group", "description": "Mesoproterozoic sandstone basin cover, host to unconformity-type uranium deposits", "formation_type": "sedimentary_basin"},
        {"name": "Paleoproterozoic Basement", "description": "Pelitic and semipelitic gneisses intruded by graphitic metapelite units", "formation_type": "basement"},
        {"name": "Triple R Deposit Zone", "description": "Unconformity-related uranium mineralization zone at the Athabasca-basement contact", "formation_type": "mineralized_zone"},
    ]

    async with driver.session() as session:
        for r in rows:
            await session.run(
                "MERGE (f:Formation {name: $name}) "
                "SET f.code = $code, f.description = $desc, f.formation_type = 'lithology_unit', "
                "    f.project_id = $pid",
                name=r["lithology_code"],
                code=r["lithology_code"],
                desc=r["lithology_description"],
                pid=PROJECT_ID,
            )

        for ef in extra_formations:
            await session.run(
                "MERGE (f:Formation {name: $name}) "
                "SET f.description = $desc, f.formation_type = $ft, f.project_id = $pid",
                name=ef["name"],
                desc=ef["description"],
                ft=ef["formation_type"],
                pid=PROJECT_ID,
            )

        # HAS_LITHOLOGY + INTERSECTS relationships
        await session.run(
            "MATCH (d:DrillHole {project_id: $pid}) "
            "WITH d "
            "MATCH (f:Formation) WHERE f.code IS NOT NULL AND f.project_id = $pid "
            "MERGE (d)-[:HAS_LITHOLOGY]->(f) "
            "MERGE (d)-[:INTERSECTS]->(f)",
            pid=PROJECT_ID,
        )

        # All holes intersect the Athabasca Group (sandstone cover)
        await session.run(
            "MATCH (d:DrillHole {project_id: $pid}), "
            "      (f:Formation {name: 'Athabasca Group'}) "
            "MERGE (d)-[:INTERSECTS]->(f)",
            pid=PROJECT_ID,
        )

        # All holes intersect the basement
        await session.run(
            "MATCH (d:DrillHole {project_id: $pid}), "
            "      (f:Formation {name: 'Paleoproterozoic Basement'}) "
            "MERGE (d)-[:INTERSECTS]->(f)",
            pid=PROJECT_ID,
        )

    logger.info("Formation nodes: %d litho + %d geological", len(rows), len(extra_formations))


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

async def _populate_reports(driver, pg) -> None:
    rows = await pg.fetch(
        "SELECT report_id::text, title, company, filing_date::text, "
        "commodity, project_name, region "
        "FROM silver.reports LIMIT 50"
    )
    async with driver.session() as session:
        for r in rows:
            await session.run(
                "MERGE (rpt:Report {report_id: $rid}) "
                "SET rpt.title = $title, rpt.company = $company, "
                "    rpt.filing_date = $fd, rpt.commodity = $commodity, "
                "    rpt.project_name = $pn, rpt.region = $region, "
                "    rpt.name = $title, rpt.project_id = $pid",
                rid=r["report_id"],
                title=r["title"],
                company=r["company"],
                fd=r["filing_date"],
                commodity=r["commodity"],
                pn=r["project_name"],
                region=r["region"],
                pid=PROJECT_ID,
            )

        # HAS_REPORT relationship
        await session.run(
            "MATCH (p:Project {project_id: $pid}), (rpt:Report {project_id: $pid}) "
            "MERGE (p)-[:HAS_REPORT]->(rpt)",
            pid=PROJECT_ID,
        )

    logger.info("Report nodes: %d", len(rows))


# ---------------------------------------------------------------------------
# QualifiedPersons
# ---------------------------------------------------------------------------

async def _populate_qps(driver, pg) -> None:
    rows = await pg.fetch(
        "SELECT report_id::text, authors FROM silver.reports WHERE authors IS NOT NULL"
    )
    count = 0
    async with driver.session() as session:
        for r in rows:
            authors = r["authors"] or []
            for author in authors:
                author = author.strip()
                if not author:
                    continue
                await session.run(
                    "MERGE (q:QualifiedPerson {name: $name}) "
                    "SET q.project_id = $pid",
                    name=author,
                    pid=PROJECT_ID,
                )
                # AUTHORED_BY
                await session.run(
                    "MATCH (rpt:Report {report_id: $rid}), "
                    "      (q:QualifiedPerson {name: $name}) "
                    "MERGE (rpt)-[:AUTHORED_BY]->(q)",
                    rid=r["report_id"],
                    name=author,
                )
                count += 1
    logger.info("QualifiedPerson nodes: %d", count)


# ---------------------------------------------------------------------------
# Deposits
# ---------------------------------------------------------------------------

async def _populate_deposits(driver) -> None:
    # From the NI 43-101 report: Triple R deposit at Patterson Lake South
    deposits = [
        {
            "name": "Triple R",
            "deposit_type": "unconformity-related uranium",
            "description": (
                "Classic unconformity-related uranium deposit at the Athabasca "
                "Basin unconformity contact. Comparable to McArthur River and "
                "Cigar Lake deposits."
            ),
            "commodity": "uranium",
            "status": "advanced exploration",
        },
    ]
    async with driver.session() as session:
        for d in deposits:
            await session.run(
                "MERGE (dep:Deposit {name: $name}) "
                "SET dep.deposit_type = $dt, dep.description = $desc, "
                "    dep.commodity = $commodity, dep.status = $status, "
                "    dep.project_id = $pid",
                name=d["name"],
                dt=d["deposit_type"],
                desc=d["description"],
                commodity=d["commodity"],
                status=d["status"],
                pid=PROJECT_ID,
            )
        # Project HOSTS Deposit
        await session.run(
            "MATCH (p:Project {project_id: $pid}), (dep:Deposit {project_id: $pid}) "
            "MERGE (p)-[:HOSTS]->(dep)",
            pid=PROJECT_ID,
        )
        # Report DESCRIBES Deposit
        await session.run(
            "MATCH (rpt:Report {project_id: $pid}), (dep:Deposit {project_id: $pid}) "
            "MERGE (rpt)-[:DESCRIBES]->(dep)",
            pid=PROJECT_ID,
        )
        # Deposit sits at the Triple R zone formation
        await session.run(
            "MATCH (dep:Deposit {name: 'Triple R'}), "
            "      (f:Formation {name: 'Triple R Deposit Zone'}) "
            "MERGE (dep)-[:HOSTED_BY]->(f)",
        )
    logger.info("Deposit nodes: %d", len(deposits))


# ---------------------------------------------------------------------------
# MineralOccurrences
# ---------------------------------------------------------------------------

async def _populate_mineral_occurrences(driver) -> None:
    occurrences = [
        {
            "name": "Uranium (U3O8)",
            "element": "U",
            "formula": "U3O8",
            "description": "Primary commodity — high-grade unconformity-type uranium mineralization",
        },
    ]
    async with driver.session() as session:
        for m in occurrences:
            await session.run(
                "MERGE (mo:MineralOccurrence {name: $name}) "
                "SET mo.element = $el, mo.formula = $formula, "
                "    mo.description = $desc, mo.project_id = $pid",
                name=m["name"],
                el=m["element"],
                formula=m["formula"],
                desc=m["description"],
                pid=PROJECT_ID,
            )
        # Deposit HAS_MINERALIZATION
        await session.run(
            "MATCH (dep:Deposit {project_id: $pid}), "
            "      (mo:MineralOccurrence {project_id: $pid}) "
            "MERGE (dep)-[:HAS_MINERALIZATION]->(mo)",
            pid=PROJECT_ID,
        )
    logger.info("MineralOccurrence nodes: %d", len(occurrences))


# ---------------------------------------------------------------------------
# Cross-entity relationships
# ---------------------------------------------------------------------------

async def _create_cross_relationships(driver, pg) -> None:
    async with driver.session() as session:
        # DrillHole -[:TARGETS]-> Deposit (all PLS holes target Triple R)
        await session.run(
            "MATCH (d:DrillHole {project_id: $pid}), "
            "      (dep:Deposit {name: 'Triple R'}) "
            "WHERE d.hole_id STARTS WITH 'PLS' "
            "MERGE (d)-[:TARGETS]->(dep)",
            pid=PROJECT_ID,
        )

        # Formation hierarchy: litho units are PART_OF the geological formations
        await session.run(
            "MATCH (f:Formation {name: 'SST'}) "
            "MATCH (parent:Formation {name: 'Athabasca Group'}) "
            "MERGE (f)-[:PART_OF]->(parent)"
        )
        await session.run(
            "MATCH (f:Formation) WHERE f.name IN ['PGN', 'GPT'] "
            "MATCH (parent:Formation {name: 'Paleoproterozoic Basement'}) "
            "MERGE (f)-[:PART_OF]->(parent)"
        )

        # QP associated with project
        await session.run(
            "MATCH (q:QualifiedPerson {project_id: $pid}), "
            "      (p:Project {project_id: $pid}) "
            "MERGE (q)-[:WORKS_ON]->(p)",
            pid=PROJECT_ID,
        )

    logger.info("Cross-entity relationships created")


if __name__ == "__main__":
    asyncio.run(main())
