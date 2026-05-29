"""Index layer asset — populate Neo4j knowledge graph from Silver PostGIS data.

Reads project, collar, and lithology data from the Silver PostgreSQL schema
and writes it into the Neo4j knowledge graph as typed nodes and relationships.

All Cypher writes use MERGE (idempotent) so re-running this asset against the
same project data is safe — it will update properties on existing nodes rather
than creating duplicates.

Node types created here (Section 04f):
  - Project       (name, company, region, commodity)
  - DrillHole     (hole_id, total_depth, type, status, drill_date)
  - Formation     (name, type, age)

Relationships created here:
  - (Project)-[:HAS_HOLE]->(Drillhole)
  - (Drillhole)-[:LOCATED_IN]->(Project)   reverse edge for easier traversal
  - (Drillhole)-[:HAS_LITHOLOGY]->(Formation)

NOTE: Do NOT add `from __future__ import annotations` to this file.
Dagster 1.13 Config classes use Pydantic for type introspection and that
import breaks runtime annotation evaluation.
"""

import psycopg2.extras
from dagster import AssetExecutionContext, Config, MaterializeResult, MetadataValue, asset

from georag_dagster.assets.silver import silver_collars
from georag_dagster.assets.silver_lithology import silver_lithology
from georag_dagster.resources import Neo4jResource, PostgresResource


# ---------------------------------------------------------------------------
# Lithology-code → Formation type lookup
#
# Maps the lithology_code values stored in silver.lithology_logs to a human-
# readable rock type string stored on Formation.type.  Codes not present in
# this table fall back to the raw lithology_description (if available) or
# "Unknown".  Age is fixed to "Paleoproterozoic" for all Athabasca Basin
# lithologies per the project geology.
# ---------------------------------------------------------------------------

LITHO_CODE_TO_TYPE: dict[str, str] = {
    # Athabasca Group sandstones
    "SS":   "Sandstone",
    "SSF":  "Sandstone",          # fine-grained sandstone
    "SSM":  "Sandstone",          # medium-grained sandstone
    "SSC":  "Sandstone",          # coarse-grained sandstone
    # Conglomerates / pebbly units
    "CONG": "Conglomerate",
    "CGL":  "Conglomerate",
    # Basement metasediments / pelites
    "PSLT": "Pelite",
    "MSLT": "Pelite",
    "META": "Metasediment",
    # Granitoids
    "GRN":  "Granite",
    "GRAN": "Granite",
    "GRNT": "Granite",
    "PEG":  "Pegmatite",
    # Mafic intrusives
    "GAB":  "Gabbro",
    "BAS":  "Basalt",
    # Alteration
    "CLY":  "Clay",
    "ILL":  "Illite Alteration",
    "CHL":  "Chlorite Alteration",
    "SID":  "Siderite Alteration",
    # Structural
    "BX":   "Breccia",
    "BXCC": "Carbonate Breccia",
    # Unconsolidated overburden
    "OB":   "Overburden",
    "TILL": "Till",
}

DEFAULT_FORMATION_AGE = "Paleoproterozoic"


# ---------------------------------------------------------------------------
# SQL queries — Silver PostGIS
# ---------------------------------------------------------------------------

PROJECT_SQL = """
SELECT
    project_id,
    project_name AS name,
    company,
    region,
    commodity
FROM silver.projects
WHERE project_id = %(project_id)s
LIMIT 1;
"""

COLLARS_SQL = """
SELECT
    hole_id,
    total_depth,
    hole_type,
    status,
    drill_date::text AS drill_date
FROM silver.collars
WHERE project_id = %(project_id)s
ORDER BY hole_id;
"""

# Fetch one representative row per lithology_code for the collars in this
# project so we can build Formation nodes.  The JOIN to silver.collars is
# required because lithology_logs only carries collar_id, not project_id.
LITHOLOGY_CODES_SQL = """
SELECT DISTINCT ON (ll.lithology_code)
    ll.lithology_code,
    ll.lithology_description
FROM silver.lithology_logs ll
INNER JOIN silver.collars c ON c.collar_id = ll.collar_id
WHERE c.project_id = %(project_id)s
  AND ll.lithology_code IS NOT NULL
ORDER BY ll.lithology_code, ll.lithology_description NULLS LAST;
"""

# Fetch (hole_id, lithology_code) pairs so we can wire HAS_LITHOLOGY edges.
HOLE_LITHO_EDGES_SQL = """
SELECT DISTINCT
    c.hole_id,
    ll.lithology_code
FROM silver.lithology_logs ll
INNER JOIN silver.collars c ON c.collar_id = ll.collar_id
WHERE c.project_id = %(project_id)s
  AND ll.lithology_code IS NOT NULL
ORDER BY c.hole_id, ll.lithology_code;
"""


# ---------------------------------------------------------------------------
# Cypher — Neo4j writes (all idempotent via MERGE)
# ---------------------------------------------------------------------------

# Upsert a single Project node.
MERGE_PROJECT_CYPHER = """
MERGE (p:Project {name: $project_name})
  ON CREATE SET
    p.project_id = $project_id,
    p.company    = $company,
    p.region     = $region,
    p.commodity  = $commodity,
    p.created_at = datetime()
  ON MATCH SET
    p.company      = $company,
    p.region       = $region,
    p.commodity    = $commodity,
    p.last_updated = datetime()
RETURN p
"""

# Batch upsert DrillHole nodes and attach to the Project in one statement.
# $collars is a list of maps: {hole_id, total_depth, hole_type, status, drill_date}
# $project_name is a scalar used to look up the already-merged Project node.
MERGE_DRILLHOLES_CYPHER = """
UNWIND $collars AS c
MERGE (dh:DrillHole {hole_id: c.hole_id})
  ON CREATE SET
    dh.total_depth = c.total_depth,
    dh.type        = c.hole_type,
    dh.status      = c.status,
    dh.drill_date  = c.drill_date,
    dh.created_at  = datetime()
  ON MATCH SET
    dh.total_depth = c.total_depth,
    dh.status      = c.status,
    dh.last_updated = datetime()
WITH dh, c
MATCH (p:Project {name: $project_name})
MERGE (p)-[:HAS_HOLE]->(dh)
MERGE (dh)-[:LOCATED_IN]->(p)
"""

# Batch upsert Formation nodes.
# $formations is a list of maps: {name, type, age}
MERGE_FORMATIONS_CYPHER = """
UNWIND $formations AS f
MERGE (fm:Formation {name: f.name})
  ON CREATE SET
    fm.type       = f.type,
    fm.age        = f.age,
    fm.created_at = datetime()
  ON MATCH SET
    fm.type        = f.type,
    fm.age         = f.age,
    fm.last_updated = datetime()
"""

# Batch create HAS_LITHOLOGY edges from DrillHole → Formation.
# $edges is a list of maps: {hole_id, litho_code}
MERGE_LITHO_EDGES_CYPHER = """
UNWIND $edges AS e
MATCH (dh:DrillHole {hole_id: e.hole_id})
MATCH (fm:Formation {name: e.litho_code})
MERGE (dh)-[:HAS_LITHOLOGY]->(fm)
"""

# Count queries for the MaterializeResult metadata.
COUNT_CYPHER = """
MATCH (p:Project {name: $project_name})
OPTIONAL MATCH (p)-[:HAS_HOLE]->(dh:DrillHole)
OPTIONAL MATCH (dh)-[:HAS_LITHOLOGY]->(fm:Formation)
RETURN
    count(DISTINCT p)  AS project_count,
    count(DISTINCT dh) AS drillhole_count,
    count(DISTINCT fm) AS formation_count
"""

COUNT_RELS_CYPHER = """
MATCH (p:Project {name: $project_name})
OPTIONAL MATCH (p)-[r1:HAS_HOLE]->()
OPTIONAL MATCH ()-[r2:LOCATED_IN]->(p)
OPTIONAL MATCH (p)-[:HAS_HOLE]->(:DrillHole)-[r3:HAS_LITHOLOGY]->()
RETURN
    count(r1) AS has_hole_count,
    count(r2) AS located_in_count,
    count(r3) AS has_lithology_count
"""


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class IndexNeo4jConfig(Config):
    """Runtime configuration for the index_neo4j asset.

    project_id must match a row in silver.projects.  The entire graph
    population for that project is (re)run — existing nodes are updated in
    place via MERGE, not re-created.
    """

    project_id: str


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="index",
    deps=[silver_collars, silver_lithology],
    description=(
        "Read project, collar, and lithology data from Silver PostGIS and "
        "populate the Neo4j knowledge graph with Project, DrillHole, and "
        "Formation nodes plus HAS_HOLE, LOCATED_IN, and HAS_LITHOLOGY "
        "relationships.  All writes are idempotent MERGE operations."
    ),
)
def index_neo4j(
    context: AssetExecutionContext,
    config: IndexNeo4jConfig,
    postgres: PostgresResource,
    neo4j: Neo4jResource,
) -> MaterializeResult:
    """Read Silver PostGIS → write typed nodes and relationships into Neo4j."""

    project_id = config.project_id
    context.log.info("index_neo4j: starting graph population for project_id='%s'", project_id)

    # ------------------------------------------------------------------
    # Step 1 — Fetch project metadata from Silver PostgreSQL
    # ------------------------------------------------------------------
    context.log.info("Step 1: querying silver.projects for project_id='%s'", project_id)

    project_row: dict | None = None
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(PROJECT_SQL, {"project_id": project_id})
            row = cur.fetchone()
            if row:
                project_row = dict(row)

    if project_row is None:
        context.log.warning(
            "No project found for project_id='%s' in silver.projects — "
            "graph population skipped.",
            project_id,
        )
        return MaterializeResult(
            metadata={
                "project_id":        MetadataValue.text(project_id),
                "project_found":     MetadataValue.bool(False),
                "collars_loaded":    MetadataValue.int(0),
                "formations_loaded": MetadataValue.int(0),
                "litho_edges_loaded":MetadataValue.int(0),
            }
        )

    project_name: str = project_row["name"]
    context.log.info(
        "Step 1: found project '%s' (company=%s, region=%s, commodity=%s)",
        project_name,
        project_row.get("company"),
        project_row.get("region"),
        project_row.get("commodity"),
    )

    # ------------------------------------------------------------------
    # Step 2 — Fetch collars
    # ------------------------------------------------------------------
    context.log.info("Step 2: querying silver.collars for project_id='%s'", project_id)

    collar_rows: list[dict] = []
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(COLLARS_SQL, {"project_id": project_id})
            collar_rows = [dict(r) for r in cur.fetchall()]

    context.log.info("Step 2: found %d collar(s)", len(collar_rows))

    if not collar_rows:
        context.log.warning(
            "No collars found for project_id='%s' — only Project node will be written.",
            project_id,
        )

    # ------------------------------------------------------------------
    # Step 3 — Fetch distinct lithology codes and hole→litho edges
    # ------------------------------------------------------------------
    context.log.info("Step 3: querying lithology codes for project_id='%s'", project_id)

    litho_code_rows: list[dict] = []
    hole_litho_edges: list[dict] = []

    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(LITHOLOGY_CODES_SQL, {"project_id": project_id})
            litho_code_rows = [dict(r) for r in cur.fetchall()]

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(HOLE_LITHO_EDGES_SQL, {"project_id": project_id})
            hole_litho_edges = [dict(r) for r in cur.fetchall()]

    context.log.info(
        "Step 3: found %d distinct lithology code(s), %d hole-lithology edge(s)",
        len(litho_code_rows),
        len(hole_litho_edges),
    )

    # ------------------------------------------------------------------
    # Step 4 — Build parameter lists for Cypher batch writes
    # ------------------------------------------------------------------

    # Drillhole parameter list for UNWIND
    collar_params = [
        {
            "hole_id":     r["hole_id"],
            "total_depth": float(r["total_depth"]) if r.get("total_depth") is not None else None,
            "hole_type":   r.get("hole_type"),
            "status":      r.get("status"),
            "drill_date":  r.get("drill_date"),  # already cast to text in SQL
        }
        for r in collar_rows
    ]

    # Formation parameter list — resolve type from lookup table
    formation_params = []
    for r in litho_code_rows:
        code = r["lithology_code"]
        # Prefer explicit lookup, then lithology_description, then "Unknown"
        rock_type = LITHO_CODE_TO_TYPE.get(
            code.upper() if code else "",
            r.get("lithology_description") or "Unknown",
        )
        formation_params.append({
            "name": code,
            "type": rock_type,
            "age":  DEFAULT_FORMATION_AGE,
        })

    # HAS_LITHOLOGY edge parameter list
    edge_params = [
        {"hole_id": r["hole_id"], "litho_code": r["lithology_code"]}
        for r in hole_litho_edges
    ]

    # ------------------------------------------------------------------
    # Step 5 — Write to Neo4j (all in a single driver session, two txs)
    # ------------------------------------------------------------------
    context.log.info("Step 5: opening Neo4j session (uri=%s)", neo4j.uri)

    driver = neo4j.get_driver()
    try:
        with driver.session(database="neo4j") as session:

            # --- Transaction A: Project + DrillHoles + Formations ---
            context.log.info(
                "Step 5a: merging Project node '%s' into graph", project_name
            )
            with session.begin_transaction() as tx:
                # Project node
                tx.run(
                    MERGE_PROJECT_CYPHER,
                    project_name=project_name,
                    project_id=str(project_row["project_id"]),
                    company=project_row.get("company") or "",
                    region=project_row.get("region") or "",
                    commodity=project_row.get("commodity") or "",
                )

                # Drillhole nodes + HAS_HOLE / LOCATED_IN relationships
                if collar_params:
                    context.log.info(
                        "Step 5b: merging %d DrillHole node(s) + relationships",
                        len(collar_params),
                    )
                    tx.run(
                        MERGE_DRILLHOLES_CYPHER,
                        collars=collar_params,
                        project_name=project_name,
                    )
                else:
                    context.log.info("Step 5b: no collars to write — skipping DrillHole merge")

                # Formation nodes
                if formation_params:
                    context.log.info(
                        "Step 5c: merging %d Formation node(s)",
                        len(formation_params),
                    )
                    tx.run(MERGE_FORMATIONS_CYPHER, formations=formation_params)
                else:
                    context.log.info("Step 5c: no lithology codes found — skipping Formation merge")

                tx.commit()

            # --- Transaction B: HAS_LITHOLOGY edges ---
            # Separate transaction so the Formation and Drillhole nodes are
            # committed and visible before we try to MATCH them.
            if edge_params:
                context.log.info(
                    "Step 5d: merging %d HAS_LITHOLOGY edge(s)", len(edge_params)
                )
                with session.begin_transaction() as tx:
                    tx.run(MERGE_LITHO_EDGES_CYPHER, edges=edge_params)
                    tx.commit()
            else:
                context.log.info("Step 5d: no lithology edges to write — skipping")

            # --- Collect node/relationship counts for metadata ---
            context.log.info("Step 5e: collecting graph counts for materialisation metadata")
            node_result = session.run(COUNT_CYPHER, project_name=project_name)
            node_record = node_result.single()

            rel_result = session.run(COUNT_RELS_CYPHER, project_name=project_name)
            rel_record = rel_result.single()

    finally:
        driver.close()
        context.log.info("Step 5: Neo4j driver closed")

    # Pull counts (default to 0 if queries returned nothing)
    project_count  = node_record["project_count"]  if node_record else 0
    drillhole_count= node_record["drillhole_count"] if node_record else 0
    formation_count= node_record["formation_count"] if node_record else 0
    has_hole_count      = rel_record["has_hole_count"]       if rel_record else 0
    located_in_count    = rel_record["located_in_count"]     if rel_record else 0
    has_lithology_count = rel_record["has_lithology_count"]  if rel_record else 0

    context.log.info(
        "index_neo4j complete — nodes: Project=%d DrillHole=%d Formation=%d | "
        "rels: HAS_HOLE=%d LOCATED_IN=%d HAS_LITHOLOGY=%d",
        project_count, drillhole_count, formation_count,
        has_hole_count, located_in_count, has_lithology_count,
    )

    return MaterializeResult(
        metadata={
            "project_id":             MetadataValue.text(project_id),
            "project_name":           MetadataValue.text(project_name),
            "project_found":          MetadataValue.bool(True),
            # Node counts in the graph scoped to this project
            "graph_project_nodes":    MetadataValue.int(project_count),
            "graph_drillhole_nodes":  MetadataValue.int(drillhole_count),
            "graph_formation_nodes":  MetadataValue.int(formation_count),
            # Relationship counts
            "graph_has_hole_rels":    MetadataValue.int(has_hole_count),
            "graph_located_in_rels":  MetadataValue.int(located_in_count),
            "graph_has_lithology_rels":MetadataValue.int(has_lithology_count),
            # Input row counts (what was read from Silver)
            "collars_loaded":         MetadataValue.int(len(collar_rows)),
            "formations_loaded":      MetadataValue.int(len(formation_params)),
            "litho_edges_loaded":     MetadataValue.int(len(edge_params)),
        }
    )
