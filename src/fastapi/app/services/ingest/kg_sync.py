"""silver → Neo4j knowledge graph sync.

Doc-phase 180 — Phase C Tier 1.

Walks ingested silver tables and pushes entities to Neo4j so the
orchestrator's Layer 4 entity resolution can match them against
LLM-generated text.

Entities created per project:
  - 1 `:Project` node (name from silver.projects.project_name)
  - 1 `:Project` node per company alias (so "CAMECO RESOURCES" matches)
  - 1 `:Formation` node per field/basin (so "SHIRLEY BASIN" matches)
  - 1 `:Formation` node per county (so "CARBON" matches)
  - N `:DrillHole` nodes per ingested collar
  - M `:Report` nodes per ingested document
  - 1 `:Deposit` node for deposit-type (e.g. "roll-front uranium")

Relationships:
  - (Project)-[:HAS_HOLE]->(DrillHole)
  - (Project)-[:HAS_REPORT]->(Report)
  - (Project)-[:HAS_FORMATION]->(Formation)   # basin/county
  - (Project)-[:TARGETS]->(Deposit)

The orchestrator's `fetch_project_graph_entities` query requires
`degree >= 1` — every entity created here must participate in at
least one relationship.

Idempotency: all merges use `MERGE` on `(label, name, project_id)` so
re-running is safe.
"""
from __future__ import annotations
from app.db import bind_workspace_scope

import logging
import os
from dataclasses import dataclass

import asyncpg
from neo4j import AsyncGraphDatabase

log = logging.getLogger("georag.ingest.kg_sync")


# Wyoming-specific entity additions for the WSGS archive
# Maps recognized field names (case-normalized) → deposit type
_WYOMING_BASIN_DEPOSITS: dict[str, str] = {
    "SHIRLEY BASIN": "sandstone-hosted roll-front uranium",
    "POWDER RIVER BASIN": "sandstone-hosted roll-front uranium",
    "WIND RIVER BASIN": "sandstone-hosted roll-front uranium",
    "GAS HILLS": "sandstone-hosted roll-front uranium",
    "GREAT DIVIDE BASIN": "sandstone-hosted roll-front uranium",
}


@dataclass
class KGSyncResult:
    """Outcome of one project-scoped KG sync."""
    project_id: str
    project_node_count: int = 0
    drillhole_node_count: int = 0
    formation_node_count: int = 0
    deposit_node_count: int = 0
    report_node_count: int = 0
    relationships: int = 0
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


def _build_neo4j_uri() -> str:
    host = os.environ.get("NEO4J_HOST", "neo4j")
    port = os.environ.get("NEO4J_PORT", "7687")
    return f"bolt://{host}:{port}"


async def sync_silver_project_to_neo4j(
    pg_conn: asyncpg.Connection,
    *,
    project_id: str,
) -> KGSyncResult:
    """Walk silver.* for one project and push to Neo4j.

    Args:
        pg_conn: asyncpg connection to PostgreSQL (silver source)
        project_id: silver.projects.project_id (UUID as string)

    Returns:
        KGSyncResult with per-label counts and any errors.
    """
    result = KGSyncResult(project_id=project_id)

    # ── 1. Load the project metadata from silver ────────────────────
    # silver.projects has no RLS so this read works without GUC.
    proj_row = await pg_conn.fetchrow(
        """
        SELECT project_id::text AS project_id,
               project_name, company, region, commodity, slug,
               workspace_id::text AS workspace_id
          FROM silver.projects
         WHERE project_id = $1::uuid
        """,
        project_id,
    )
    if not proj_row:
        result.errors.append(f"project_not_found:{project_id}")
        return result

    # Block-1 RLS (2026-05-15): silver.collars / reports / well_log_curves
    # are workspace_id-scoped now. Set the GUC to the project's workspace
    # so the subsequent reads return the project's rows.
    if proj_row["workspace_id"]:
        await bind_workspace_scope(
            pg_conn, workspace_id=proj_row["workspace_id"], site="ingest.kg_sync"
        )

    proj_name = proj_row["project_name"]
    company = (proj_row["company"] or "").strip()
    region = (proj_row["region"] or "").strip()
    commodity = (proj_row["commodity"] or "").strip()

    # Parse region into county + state (e.g. "CARBON, WY")
    county, state = None, None
    if "," in region:
        parts = [s.strip() for s in region.split(",")]
        if len(parts) == 2:
            county, state = parts[0], parts[1]
    elif region:
        # Single token — assume state
        state = region

    # Field/basin: extract from project name (e.g. "CAMECO ... SHIRLEY BASIN")
    field_name = None
    upper = proj_name.upper()
    for basin in _WYOMING_BASIN_DEPOSITS:
        if basin in upper:
            field_name = basin
            break

    # ── 2. Load drillholes ──────────────────────────────────────────
    holes = await pg_conn.fetch(
        """
        SELECT collar_id::text AS collar_id,
               hole_id, total_depth, drill_date,
               easting, northing, elevation, azimuth, dip, hole_type, status
          FROM silver.collars
         WHERE project_id = $1::uuid
        """,
        project_id,
    )

    # ── 3. Load reports ─────────────────────────────────────────────
    reports = await pg_conn.fetch(
        """
        SELECT report_id::text AS report_id, title, company, region, commodity
          FROM silver.reports
         WHERE project_id = $1::uuid
        """,
        project_id,
    )

    # ── 4. Push to Neo4j ────────────────────────────────────────────
    user = os.environ["NEO4J_USER"]
    password = os.environ["NEO4J_PASSWORD"]
    driver = AsyncGraphDatabase.driver(_build_neo4j_uri(), auth=(user, password))
    try:
        async with driver.session() as session:
            # 4a. Project node
            await session.run(
                """
                MERGE (p:Project {project_id: $project_id})
                  ON CREATE SET p.name = $name, p.commodity = $commodity, p.region = $region
                  ON MATCH  SET p.name = $name, p.commodity = $commodity, p.region = $region
                """,
                project_id=project_id, name=proj_name,
                commodity=commodity, region=region,
            )
            result.project_node_count += 1

            # 4b. Company alias as a Formation node. Many Wyoming projects share
            # the same company (e.g. "WSGS Archive") — Formation.name has a
            # uniqueness constraint, so MERGE on name only and let multiple
            # projects share the node via HAS_FORMATION relationships.
            if company and company.strip():
                try:
                    await session.run(
                        """
                        MERGE (c:Formation {name: $company})
                          ON CREATE SET c.project_id = $project_id,
                                        c.formation_type = 'company',
                                        c.description = $description
                        """,
                        project_id=project_id, company=company,
                        description=f"Operator/company: {company}",
                    )
                    await session.run(
                        """
                        MATCH (p:Project {project_id: $project_id})
                        MATCH (c:Formation {name: $company})
                        MERGE (p)-[:HAS_FORMATION]->(c)
                        """,
                        project_id=project_id, company=company,
                    )
                    result.formation_node_count += 1
                    result.relationships += 1
                except Exception as e:
                    result.errors.append(f"formation_company:{e}")

            # 4c. Field / basin as a Formation node (shared across projects)
            if field_name:
                try:
                    await session.run(
                        """
                        MERGE (f:Formation {name: $field})
                          ON CREATE SET f.project_id = $project_id,
                                        f.formation_type = 'basin',
                                        f.description = 'Wyoming sedimentary basin (uranium-hosting)'
                        """,
                        project_id=project_id, field=field_name,
                    )
                    await session.run(
                        """
                        MATCH (p:Project {project_id: $project_id})
                        MATCH (f:Formation {name: $field})
                        MERGE (p)-[:HAS_FORMATION]->(f)
                        """,
                        project_id=project_id, proj=proj_name, field=field_name,
                    )
                    result.formation_node_count += 1
                    result.relationships += 1
                except Exception as e:
                    result.errors.append(f"formation_basin:{e}")

            # 4d. County as a Formation node (shared across projects)
            if county:
                try:
                    await session.run(
                        """
                        MERGE (c:Formation {name: $county})
                          ON CREATE SET c.project_id = $project_id,
                                        c.formation_type = 'county',
                                        c.description = $description
                        """,
                        project_id=project_id, county=county,
                        description=f"{county} County, {state or 'Wyoming'}",
                    )
                    await session.run(
                        """
                        MATCH (p:Project {project_id: $project_id})
                        MATCH (c:Formation {name: $county})
                        MERGE (p)-[:HAS_FORMATION]->(c)
                        """,
                        project_id=project_id, proj=proj_name, county=county,
                    )
                    result.formation_node_count += 1
                    result.relationships += 1
                except Exception as e:
                    result.errors.append(f"formation_county:{e}")

            # 4e. Deposit type
            deposit_type = None
            if field_name and field_name in _WYOMING_BASIN_DEPOSITS:
                deposit_type = _WYOMING_BASIN_DEPOSITS[field_name]
            elif commodity.lower() == "uranium":
                deposit_type = "uranium occurrence"

            if deposit_type:
                try:
                    await session.run(
                        """
                        MERGE (d:Deposit {name: $name})
                          ON CREATE SET d.project_id = $project_id,
                                        d.commodity = $commodity, d.deposit_type = $name,
                                        d.description = 'Roll-front uranium deposit hosted in sandstone (Wyoming-type)'
                        """,
                        project_id=project_id, name=deposit_type,
                        commodity=commodity,
                    )
                    await session.run(
                        """
                        MATCH (p:Project {project_id: $project_id})
                        MATCH (d:Deposit {name: $name})
                        MERGE (p)-[:TARGETS]->(d)
                        """,
                        project_id=project_id, proj=proj_name, name=deposit_type,
                    )
                    result.deposit_node_count += 1
                    result.relationships += 1
                except Exception as e:
                    result.errors.append(f"deposit:{e}")

            # 4f. Drillhole nodes (one per collar)
            for hole in holes:
                try:
                    await session.run(
                        """
                        MERGE (h:DrillHole {project_id: $project_id, hole_id: $hole_id})
                          ON CREATE SET h.name = $hole_id,
                                        h.collar_id = $collar_id,
                                        h.total_depth = $total_depth,
                                        h.easting = $easting,
                                        h.northing = $northing,
                                        h.elevation = $elevation,
                                        h.azimuth = $azimuth,
                                        h.dip = $dip,
                                        h.hole_type = $hole_type,
                                        h.status = $status,
                                        h.drill_date = $drill_date
                          ON MATCH SET  h.collar_id = $collar_id,
                                        h.total_depth = $total_depth,
                                        h.easting = $easting,
                                        h.northing = $northing
                        """,
                        project_id=project_id,
                        hole_id=hole["hole_id"],
                        collar_id=hole["collar_id"],
                        total_depth=float(hole["total_depth"]) if hole["total_depth"] else None,
                        easting=float(hole["easting"]) if hole["easting"] else None,
                        northing=float(hole["northing"]) if hole["northing"] else None,
                        elevation=float(hole["elevation"]) if hole["elevation"] else None,
                        azimuth=float(hole["azimuth"]) if hole["azimuth"] else None,
                        dip=float(hole["dip"]) if hole["dip"] else None,
                        hole_type=hole["hole_type"],
                        status=hole["status"],
                        drill_date=str(hole["drill_date"]) if hole["drill_date"] else None,
                    )
                    await session.run(
                        """
                        MATCH (p:Project {project_id: $project_id})
                        MATCH (h:DrillHole {project_id: $project_id, hole_id: $hole_id})
                        MERGE (p)-[:HAS_HOLE]->(h)
                        """,
                        project_id=project_id, proj=proj_name, hole_id=hole["hole_id"],
                    )
                    result.drillhole_node_count += 1
                    result.relationships += 1
                except Exception as e:
                    result.errors.append(f"drillhole:{e}")

            # 4g. Report nodes
            for rep in reports:
                try:
                    await session.run(
                        """
                        MERGE (r:Report {project_id: $project_id, report_id: $report_id})
                          ON CREATE SET r.name = $title,
                                        r.title = $title,
                                        r.company = $company,
                                        r.region = $region,
                                        r.commodity = $commodity,
                                        r.report_type = 'pdf'
                        """,
                        project_id=project_id,
                        report_id=rep["report_id"],
                        title=rep["title"],
                        company=rep["company"],
                        region=rep["region"],
                        commodity=rep["commodity"],
                    )
                    await session.run(
                        """
                        MATCH (p:Project {project_id: $project_id})
                        MATCH (r:Report {project_id: $project_id, report_id: $report_id})
                        MERGE (p)-[:HAS_REPORT]->(r)
                        """,
                        project_id=project_id, proj=proj_name, report_id=rep["report_id"],
                    )
                    result.report_node_count += 1
                    result.relationships += 1
                except Exception as e:
                    result.errors.append(f"report:{e}")
    finally:
        await driver.close()

    log.info(
        "kg_sync.completed project_id=%s projects=%d drillholes=%d "
        "formations=%d deposits=%d reports=%d rels=%d",
        project_id, result.project_node_count, result.drillhole_node_count,
        result.formation_node_count, result.deposit_node_count,
        result.report_node_count, result.relationships,
    )
    return result


__all__ = ["sync_silver_project_to_neo4j", "KGSyncResult"]
