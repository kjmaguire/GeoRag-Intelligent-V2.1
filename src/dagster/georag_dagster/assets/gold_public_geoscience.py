"""Gold Public Geoscience — populate the Neo4j knowledge graph.

Phase 3.1. One asset that reads the four canonical `public_geo.pg_*`
tables (populated by Silver in Phase 2.3) and merges them into Neo4j with the
typed-entity model defined in plan §06b:

    Nodes
      :Jurisdiction            {code, name, level, country_code}
      :PublicGeoSource         {source_id, name, service_url, license_url}
      :Commodity               {code, name, grouping}
      :Mine                    (with secondary :PublicGeo label)
      :MineralOccurrence       (with secondary :PublicGeo label)
      :DrillHole               (with secondary :PublicGeo label)
      :ResourcePotentialZone   (new node type — plan §06b)

    Relationships
      (:PublicGeoSource)-[:PUBLISHED_BY]->(:Jurisdiction)
      (:Mine|:MineralOccurrence|:DrillHole|:ResourcePotentialZone)
          -[:SOURCED_FROM]->(:PublicGeoSource)
      (:ResourcePotentialZone)-[:COVERS_AREA_FOR {commodity}]->(:Commodity)
      (:Mine)-[:HAS_COMMODITY]->(:Commodity)
      (:MineralOccurrence)-[:HAS_PRIMARY_COMMODITY|HAS_ASSOCIATED_COMMODITY]
          ->(:Commodity)

All writes are MERGE-based so re-runs are idempotent: re-materializing after a
Silver refresh updates property values on existing nodes rather than creating
duplicates.

Entity keys:
    :Jurisdiction          {code}             → CA-SK
    :PublicGeoSource       {source_id}        → CA-SK-SMDI
    :Commodity             {code}             → Au / Cu / U / …
    :Mine / :MineralOccurrence / :DrillHole / :ResourcePotentialZone
                           {pg_id}            → UUID from pg_* tables

Note on label spelling — both this asset (PG) and the internal
`silver_collars → index_neo4j` pipeline write `:DrillHole` (PascalCase
per §04f Global Invariant 4). PG-sourced nodes additionally carry the
`:PublicGeo` secondary label, which lets cross-corpus queries cheaply
distinguish government records from internal project data without
separate label namespaces. The 2026-04-27 migration script at
`ops/migrations/neo4j/2026-04-27-drillhole-rename.cypher` canonicalised
all live nodes from the legacy `:Drillhole` spelling.

NOTE: Do NOT add `from __future__ import annotations` to this file. Dagster
1.13 Config classes use Pydantic for type introspection and that import
breaks runtime annotation evaluation.
"""

import psycopg2.extras
from dagster import (
    AssetExecutionContext,
    Config,
    MaterializeResult,
    MetadataValue,
    asset,
)

from georag_dagster.assets.silver_public_geoscience import (
    silver_pg_ca_sk_assessment_airborne,
    silver_pg_ca_sk_assessment_ground,
    silver_pg_ca_sk_assessment_underground,
    silver_pg_ca_sk_drillhole,
    silver_pg_ca_sk_mine_loc,
    silver_pg_ca_sk_resource_potential,
    silver_pg_ca_sk_rock_samples,
    silver_pg_ca_sk_smdi,
)
from georag_dagster.resources import Neo4jResource, PostgresResource


# ---------------------------------------------------------------------------
# SQL — pull canonical rows + jurisdiction/source context in one shot per
# entity type. ST_X/ST_Y extract lon/lat for the geom property. We pass
# coordinates through as separate properties (lon/lat) on entity nodes
# so Cypher can use them without needing a spatial plugin.
# ---------------------------------------------------------------------------

JURISDICTIONS_SQL = """
SELECT
    j.jurisdiction_code   AS code,
    j.country_code,
    j.display_name        AS name,
    j.level,
    j.primary_authority,
    j.license_summary,
    j.license_url
  FROM public_geo.jurisdictions j
 WHERE j.status = 'active'
"""

SOURCES_SQL = """
SELECT
    s.source_id,
    s.jurisdiction_code,
    s.name,
    s.canonical_type,
    s.service_url,
    s.license_summary,
    s.license_url
  FROM public_geo.sources s
  JOIN public_geo.jurisdictions j
       ON j.jurisdiction_code = s.jurisdiction_code
 WHERE j.status = 'active'
"""

COMMODITY_ALIASES_SQL = """
SELECT DISTINCT ON (canonical_code)
       canonical_code AS code,
       canonical_name AS name,
       commodity_grouping
  FROM public_geo.commodity_aliases
 ORDER BY canonical_code, canonical_name
"""

MINES_SQL = """
SELECT
    m.id::text            AS pg_id,
    m.jurisdiction_code,
    m.source_id,
    m.source_feature_id,
    m.name,
    m.status,
    m.commodities,
    m.commodity_grouping,
    m.operator,
    m.source_url,
    ST_X(m.geom::geometry) AS lon,
    ST_Y(m.geom::geometry) AS lat,
    m.last_seen_at
  FROM public_geo.pg_mine m
  JOIN public_geo.jurisdictions j
       ON j.jurisdiction_code = m.jurisdiction_code
 WHERE j.status = 'active'
"""

OCCURRENCES_SQL = """
SELECT
    o.id::text            AS pg_id,
    o.jurisdiction_code,
    o.source_id,
    o.source_feature_id,
    o.external_id,
    o.name,
    o.status,
    o.primary_commodities,
    o.associated_commodities,
    o.commodity_grouping,
    o.discovery_type,
    o.production_flag,
    o.source_url,
    ST_X(o.geom::geometry) AS lon,
    ST_Y(o.geom::geometry) AS lat,
    o.last_seen_at
  FROM public_geo.pg_mineral_occurrence o
  JOIN public_geo.jurisdictions j
       ON j.jurisdiction_code = o.jurisdiction_code
 WHERE j.status = 'active'
"""

DRILLHOLES_SQL = """
SELECT
    d.id::text            AS pg_id,
    d.jurisdiction_code,
    d.source_id,
    d.source_feature_id,
    d.drillhole_id,
    d.drillhole_name,
    d.company,
    d.project_name,
    d.date_drilled::text  AS date_drilled,
    d.drill_type,
    d.commodity_of_interest,
    d.total_length_m,
    d.core_availability,
    d.source_url,
    ST_X(d.geom::geometry) AS lon,
    ST_Y(d.geom::geometry) AS lat,
    d.last_seen_at
  FROM public_geo.pg_drillhole_collar d
  JOIN public_geo.jurisdictions j
       ON j.jurisdiction_code = d.jurisdiction_code
 WHERE j.status = 'active'
"""

RESOURCE_POTENTIAL_SQL = """
SELECT
    r.id::text            AS pg_id,
    r.jurisdiction_code,
    r.source_id,
    r.source_feature_id,
    r.commodity,
    r.commodity_grouping,
    r.potential_rank,
    r.methodology_ref,
    ST_XMin(r.geom::geometry) AS min_lon,
    ST_YMin(r.geom::geometry) AS min_lat,
    ST_XMax(r.geom::geometry) AS max_lon,
    ST_YMax(r.geom::geometry) AS max_lat,
    r.last_seen_at
  FROM public_geo.pg_resource_potential_zone r
  JOIN public_geo.jurisdictions j
       ON j.jurisdiction_code = r.jurisdiction_code
 WHERE j.status = 'active'
"""

# Tier-1 expansion entities — added after rock_sample + assessment_survey
# shipped to PostGIS + Martin but before they reached the knowledge graph.
# Both follow the same shape as the originals: point coords for rock
# samples, polygon bbox for assessment surveys.
ROCK_SAMPLES_SQL = """
SELECT
    rs.id::text          AS pg_id,
    rs.jurisdiction_code,
    rs.source_id,
    rs.source_feature_id,
    rs.station,
    rs.sample_number,
    rs.geologist,
    rs.geographic_area,
    rs.report_number,
    rs.map_number,
    rs.map_scale,
    rs.nts_250k,
    rs.nts_50k,
    rs.date_collected::text  AS date_collected,
    rs.source_url,
    ST_X(rs.geom::geometry)  AS lon,
    ST_Y(rs.geom::geometry)  AS lat,
    rs.last_seen_at
  FROM public_geo.pg_rock_sample rs
  JOIN public_geo.jurisdictions j
       ON j.jurisdiction_code = rs.jurisdiction_code
 WHERE j.status = 'active'
"""

ASSESSMENT_SURVEYS_SQL = """
SELECT
    a.id::text               AS pg_id,
    a.jurisdiction_code,
    a.source_id,
    a.source_feature_id,
    a.survey_type,
    a.source_url,
    ST_XMin(a.geom::geometry) AS min_lon,
    ST_YMin(a.geom::geometry) AS min_lat,
    ST_XMax(a.geom::geometry) AS max_lon,
    ST_YMax(a.geom::geometry) AS max_lat,
    a.last_seen_at
  FROM public_geo.pg_assessment_survey a
  JOIN public_geo.jurisdictions j
       ON j.jurisdiction_code = a.jurisdiction_code
 WHERE j.status = 'active'
"""


# ---------------------------------------------------------------------------
# Cypher — all MERGE-based for idempotency. Every batch operation uses
# UNWIND so one round-trip covers many rows.
# ---------------------------------------------------------------------------

MERGE_JURISDICTIONS_CYPHER = """
UNWIND $rows AS r
MERGE (j:Jurisdiction {code: r.code})
  ON CREATE SET
    j.country_code      = r.country_code,
    j.name              = r.name,
    j.level             = r.level,
    j.primary_authority = r.primary_authority,
    j.license_summary   = r.license_summary,
    j.license_url       = r.license_url,
    j.created_at        = datetime()
  ON MATCH SET
    j.country_code      = r.country_code,
    j.name              = r.name,
    j.level             = r.level,
    j.primary_authority = r.primary_authority,
    j.license_summary   = r.license_summary,
    j.license_url       = r.license_url,
    j.last_updated      = datetime()
"""

MERGE_SOURCES_CYPHER = """
UNWIND $rows AS r
MERGE (s:PublicGeoSource {source_id: r.source_id})
  ON CREATE SET
    s.name             = r.name,
    s.canonical_type   = r.canonical_type,
    s.service_url      = r.service_url,
    s.license_summary  = r.license_summary,
    s.license_url      = r.license_url,
    s.created_at       = datetime()
  ON MATCH SET
    s.name             = r.name,
    s.canonical_type   = r.canonical_type,
    s.service_url      = r.service_url,
    s.license_summary  = r.license_summary,
    s.license_url      = r.license_url,
    s.last_updated     = datetime()
WITH s, r
MATCH (j:Jurisdiction {code: r.jurisdiction_code})
MERGE (s)-[:PUBLISHED_BY]->(j)
"""

MERGE_COMMODITIES_CYPHER = """
UNWIND $rows AS r
MERGE (c:Commodity {code: r.code})
  ON CREATE SET
    c.name     = r.name,
    c.grouping = r.grouping,
    c.created_at = datetime()
  ON MATCH SET
    c.name     = r.name,
    c.grouping = r.grouping,
    c.last_updated = datetime()
"""

# ── Mine ────────────────────────────────────────────────────────────────
# Entity nodes get :PublicGeo as a secondary label so future cross-corpus
# queries ("show only government-sourced mines") can filter cheaply. Plan
# §06b.
MERGE_MINES_CYPHER = """
UNWIND $rows AS r
MERGE (m:Mine {pg_id: r.pg_id})
  ON CREATE SET
    m.jurisdiction_code  = r.jurisdiction_code,
    m.source_id          = r.source_id,
    m.source_feature_id  = r.source_feature_id,
    m.name               = r.name,
    m.status             = r.status,
    m.commodity_grouping = r.commodity_grouping,
    m.operator           = r.operator,
    m.source_url         = r.source_url,
    m.lon                = r.lon,
    m.lat                = r.lat,
    m.created_at         = datetime()
  ON MATCH SET
    m.name               = r.name,
    m.status             = r.status,
    m.commodity_grouping = r.commodity_grouping,
    m.operator           = r.operator,
    m.source_url         = r.source_url,
    m.lon                = r.lon,
    m.lat                = r.lat,
    m.last_updated       = datetime()
SET m:PublicGeo
WITH m, r
MATCH (s:PublicGeoSource {source_id: r.source_id})
MERGE (m)-[:SOURCED_FROM]->(s)
WITH m, r
UNWIND (CASE WHEN r.commodities IS NULL THEN [] ELSE r.commodities END) AS cmd
MATCH (c:Commodity {code: cmd})
MERGE (m)-[:HAS_COMMODITY]->(c)
"""

# ── Mineral occurrence ─────────────────────────────────────────────────
# Two commodity-edge flavours (primary vs. associated) so downstream queries
# can weight them differently.
MERGE_OCCURRENCES_CYPHER = """
UNWIND $rows AS r
MERGE (o:MineralOccurrence {pg_id: r.pg_id})
  ON CREATE SET
    o.jurisdiction_code  = r.jurisdiction_code,
    o.source_id          = r.source_id,
    o.source_feature_id  = r.source_feature_id,
    o.external_id        = r.external_id,
    o.name               = r.name,
    o.status             = r.status,
    o.commodity_grouping = r.commodity_grouping,
    o.discovery_type     = r.discovery_type,
    o.production_flag    = r.production_flag,
    o.source_url         = r.source_url,
    o.lon                = r.lon,
    o.lat                = r.lat,
    o.created_at         = datetime()
  ON MATCH SET
    o.external_id        = r.external_id,
    o.name               = r.name,
    o.status             = r.status,
    o.commodity_grouping = r.commodity_grouping,
    o.discovery_type     = r.discovery_type,
    o.production_flag    = r.production_flag,
    o.source_url         = r.source_url,
    o.lon                = r.lon,
    o.lat                = r.lat,
    o.last_updated       = datetime()
SET o:PublicGeo
WITH o, r
MATCH (s:PublicGeoSource {source_id: r.source_id})
MERGE (o)-[:SOURCED_FROM]->(s)
WITH o, r
CALL {
    WITH o, r
    UNWIND (CASE WHEN r.primary_commodities IS NULL THEN [] ELSE r.primary_commodities END) AS cmd
    MATCH (c:Commodity {code: cmd})
    MERGE (o)-[:HAS_PRIMARY_COMMODITY]->(c)
}
CALL {
    WITH o, r
    UNWIND (CASE WHEN r.associated_commodities IS NULL THEN [] ELSE r.associated_commodities END) AS cmd
    MATCH (c:Commodity {code: cmd})
    MERGE (o)-[:HAS_ASSOCIATED_COMMODITY]->(c)
}
"""

# ── DrillHole ──────────────────────────────────────────────────────────
MERGE_DRILLHOLES_CYPHER = """
UNWIND $rows AS r
MERGE (d:DrillHole {pg_id: r.pg_id})
  ON CREATE SET
    d.jurisdiction_code   = r.jurisdiction_code,
    d.source_id           = r.source_id,
    d.source_feature_id   = r.source_feature_id,
    d.drillhole_id        = r.drillhole_id,
    d.drillhole_name      = r.drillhole_name,
    d.company             = r.company,
    d.project_name        = r.project_name,
    d.date_drilled        = r.date_drilled,
    d.drill_type          = r.drill_type,
    d.total_length_m      = r.total_length_m,
    d.core_availability   = r.core_availability,
    d.source_url          = r.source_url,
    d.lon                 = r.lon,
    d.lat                 = r.lat,
    d.created_at          = datetime()
  ON MATCH SET
    d.drillhole_id        = r.drillhole_id,
    d.drillhole_name      = r.drillhole_name,
    d.company             = r.company,
    d.project_name        = r.project_name,
    d.date_drilled        = r.date_drilled,
    d.drill_type          = r.drill_type,
    d.total_length_m      = r.total_length_m,
    d.core_availability   = r.core_availability,
    d.source_url          = r.source_url,
    d.lon                 = r.lon,
    d.lat                 = r.lat,
    d.last_updated        = datetime()
SET d:PublicGeo
WITH d, r
MATCH (s:PublicGeoSource {source_id: r.source_id})
MERGE (d)-[:SOURCED_FROM]->(s)
WITH d, r
UNWIND (CASE WHEN r.commodity_of_interest IS NULL THEN [] ELSE r.commodity_of_interest END) AS cmd
MATCH (c:Commodity {code: cmd})
MERGE (d)-[:HAS_COMMODITY]->(c)
"""

# ── Resource potential zone ────────────────────────────────────────────
# Per plan §06b: :ResourcePotentialZone node + :COVERS_AREA_FOR edge to the
# commodity. The rank is stored as a property on the zone; commodity on the
# edge lets a single zone be associated with multiple commodities in future
# without changing the node schema.
MERGE_RESOURCE_POTENTIAL_CYPHER = """
UNWIND $rows AS r
MERGE (z:ResourcePotentialZone {pg_id: r.pg_id})
  ON CREATE SET
    z.jurisdiction_code  = r.jurisdiction_code,
    z.source_id          = r.source_id,
    z.source_feature_id  = r.source_feature_id,
    z.commodity          = r.commodity,
    z.commodity_grouping = r.commodity_grouping,
    z.potential_rank     = r.potential_rank,
    z.methodology_ref    = r.methodology_ref,
    z.min_lon            = r.min_lon,
    z.min_lat            = r.min_lat,
    z.max_lon            = r.max_lon,
    z.max_lat            = r.max_lat,
    z.created_at         = datetime()
  ON MATCH SET
    z.commodity          = r.commodity,
    z.commodity_grouping = r.commodity_grouping,
    z.potential_rank     = r.potential_rank,
    z.methodology_ref    = r.methodology_ref,
    z.min_lon            = r.min_lon,
    z.min_lat            = r.min_lat,
    z.max_lon            = r.max_lon,
    z.max_lat            = r.max_lat,
    z.last_updated       = datetime()
SET z:PublicGeo
WITH z, r
MATCH (s:PublicGeoSource {source_id: r.source_id})
MERGE (z)-[:SOURCED_FROM]->(s)
WITH z, r
OPTIONAL MATCH (c:Commodity {code: r.commodity})
FOREACH (_ IN CASE WHEN c IS NULL THEN [] ELSE [1] END |
    MERGE (z)-[rel:COVERS_AREA_FOR]->(c)
    SET rel.commodity = r.commodity,
        rel.rank      = r.potential_rank
)
"""

# ── Rock sample ────────────────────────────────────────────────────────
# Point-geometry government rock samples. No direct commodity relationship
# (rock samples are analyzed for a grab bag of elements); analytical data
# hangs off the node as JSONB property if/when ingested in a future phase.
MERGE_ROCK_SAMPLES_CYPHER = """
UNWIND $rows AS r
MERGE (rs:RockSample {pg_id: r.pg_id})
  ON CREATE SET
    rs.jurisdiction_code = r.jurisdiction_code,
    rs.source_id         = r.source_id,
    rs.source_feature_id = r.source_feature_id,
    rs.station           = r.station,
    rs.sample_number     = r.sample_number,
    rs.geologist         = r.geologist,
    rs.geographic_area   = r.geographic_area,
    rs.report_number     = r.report_number,
    rs.map_number        = r.map_number,
    rs.map_scale         = r.map_scale,
    rs.nts_250k          = r.nts_250k,
    rs.nts_50k           = r.nts_50k,
    rs.date_collected    = r.date_collected,
    rs.source_url        = r.source_url,
    rs.lon               = r.lon,
    rs.lat               = r.lat,
    rs.created_at        = datetime()
  ON MATCH SET
    rs.station           = r.station,
    rs.sample_number     = r.sample_number,
    rs.geologist         = r.geologist,
    rs.geographic_area   = r.geographic_area,
    rs.report_number     = r.report_number,
    rs.map_number        = r.map_number,
    rs.nts_250k          = r.nts_250k,
    rs.nts_50k           = r.nts_50k,
    rs.date_collected    = r.date_collected,
    rs.source_url        = r.source_url,
    rs.lon               = r.lon,
    rs.lat               = r.lat,
    rs.last_updated      = datetime()
SET rs:PublicGeo
WITH rs, r
MATCH (s:PublicGeoSource {source_id: r.source_id})
MERGE (rs)-[:SOURCED_FROM]->(s)
"""

# ── Assessment survey ──────────────────────────────────────────────────
# Polygon footprints for airborne / ground / underground surveys. Detailed
# survey content (lines, readings, reports) is delivered through the SMAD
# document ingestion path and linked via the cross-corpus linker —
# AssessmentSurvey nodes are the stable anchor for those links.
MERGE_ASSESSMENT_SURVEYS_CYPHER = """
UNWIND $rows AS r
MERGE (as:AssessmentSurvey {pg_id: r.pg_id})
  ON CREATE SET
    as.jurisdiction_code = r.jurisdiction_code,
    as.source_id         = r.source_id,
    as.source_feature_id = r.source_feature_id,
    as.survey_type       = r.survey_type,
    as.source_url        = r.source_url,
    as.min_lon           = r.min_lon,
    as.min_lat           = r.min_lat,
    as.max_lon           = r.max_lon,
    as.max_lat           = r.max_lat,
    as.created_at        = datetime()
  ON MATCH SET
    as.survey_type       = r.survey_type,
    as.source_url        = r.source_url,
    as.min_lon           = r.min_lon,
    as.min_lat           = r.min_lat,
    as.max_lon           = r.max_lon,
    as.max_lat           = r.max_lat,
    as.last_updated      = datetime()
SET as:PublicGeo
WITH as, r
MATCH (s:PublicGeoSource {source_id: r.source_id})
MERGE (as)-[:SOURCED_FROM]->(s)
"""

# ── Constraints / indexes — run once, idempotent ───────────────────────
# Uniqueness constraints double as indexes and prevent duplicate nodes on
# race between concurrent MERGE calls (single-writer, but future-proofing).
CREATE_CONSTRAINTS_CYPHER = [
    "CREATE CONSTRAINT jurisdiction_code IF NOT EXISTS FOR (j:Jurisdiction) REQUIRE j.code IS UNIQUE",
    "CREATE CONSTRAINT public_geosource_id IF NOT EXISTS FOR (s:PublicGeoSource) REQUIRE s.source_id IS UNIQUE",
    "CREATE CONSTRAINT commodity_code IF NOT EXISTS FOR (c:Commodity) REQUIRE c.code IS UNIQUE",
    "CREATE CONSTRAINT mine_pg_id IF NOT EXISTS FOR (m:Mine) REQUIRE m.pg_id IS UNIQUE",
    "CREATE CONSTRAINT occ_pg_id IF NOT EXISTS FOR (o:MineralOccurrence) REQUIRE o.pg_id IS UNIQUE",
    "CREATE CONSTRAINT dh_pg_id IF NOT EXISTS FOR (d:DrillHole) REQUIRE d.pg_id IS UNIQUE",
    "CREATE CONSTRAINT rpz_pg_id IF NOT EXISTS FOR (z:ResourcePotentialZone) REQUIRE z.pg_id IS UNIQUE",
    "CREATE CONSTRAINT rs_pg_id IF NOT EXISTS FOR (rs:RockSample) REQUIRE rs.pg_id IS UNIQUE",
    "CREATE CONSTRAINT as_pg_id IF NOT EXISTS FOR (as:AssessmentSurvey) REQUIRE as.pg_id IS UNIQUE",
]

# Secondary indexes on the join/filter fields the chat agent will query on.
CREATE_INDEXES_CYPHER = [
    "CREATE INDEX mine_jurisdiction IF NOT EXISTS FOR (m:Mine) ON (m.jurisdiction_code)",
    "CREATE INDEX occ_jurisdiction IF NOT EXISTS FOR (o:MineralOccurrence) ON (o.jurisdiction_code)",
    "CREATE INDEX occ_external_id IF NOT EXISTS FOR (o:MineralOccurrence) ON (o.external_id)",
    "CREATE INDEX dh_jurisdiction IF NOT EXISTS FOR (d:DrillHole) ON (d.jurisdiction_code)",
    "CREATE INDEX dh_drillhole_id IF NOT EXISTS FOR (d:DrillHole) ON (d.drillhole_id)",
    "CREATE INDEX rpz_jurisdiction IF NOT EXISTS FOR (z:ResourcePotentialZone) ON (z.jurisdiction_code)",
    "CREATE INDEX rs_jurisdiction IF NOT EXISTS FOR (rs:RockSample) ON (rs.jurisdiction_code)",
    "CREATE INDEX rs_nts250k IF NOT EXISTS FOR (rs:RockSample) ON (rs.nts_250k)",
    "CREATE INDEX as_jurisdiction IF NOT EXISTS FOR (as:AssessmentSurvey) ON (as.jurisdiction_code)",
    "CREATE INDEX as_type IF NOT EXISTS FOR (as:AssessmentSurvey) ON (as.survey_type)",
]

COUNT_CYPHER = """
MATCH (n)
WHERE n:Jurisdiction OR n:PublicGeoSource OR n:Commodity
   OR (n:PublicGeo AND (
        n:Mine OR n:MineralOccurrence OR n:DrillHole
        OR n:ResourcePotentialZone OR n:RockSample OR n:AssessmentSurvey
      ))
RETURN labels(n) AS labels, count(*) AS n
ORDER BY labels
"""


# ---------------------------------------------------------------------------
# Asset config
# ---------------------------------------------------------------------------

class GoldPublicGeoscienceConfig(Config):
    """Runtime knobs for the Neo4j Gold asset."""

    # Batch size for each UNWIND write. Neo4j community handles a few
    # thousand rows per transaction comfortably.
    batch_size: int = 1000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fetch_dicts(postgres: PostgresResource, sql: str) -> list[dict]:
    """Run SQL → list[dict] via RealDictCursor."""
    with postgres.get_connection() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql)
            return [dict(r) for r in cur.fetchall()]


def _rows_for_cypher(rows: list[dict]) -> list[dict]:
    """Convert psycopg2 types (Decimal, date, etc.) into Neo4j-friendly
    primitives. psycopg2 returns Decimal for NUMERIC; Neo4j wants float.
    """
    import decimal
    out: list[dict] = []
    for row in rows:
        converted: dict = {}
        for k, v in row.items():
            if isinstance(v, decimal.Decimal):
                converted[k] = float(v)
            elif isinstance(v, (list, tuple)):
                # psycopg2 returns TEXT[] as Python list of str — already fine.
                converted[k] = [str(x) for x in v if x is not None]
            else:
                converted[k] = v
        out.append(converted)
    return out


def _run_in_batches(
    session,
    cypher: str,
    rows: list[dict],
    batch_size: int,
    context: AssetExecutionContext,
    label: str,
) -> None:
    """Execute a Cypher UNWIND write in batches."""
    total = len(rows)
    if total == 0:
        context.log.info("gold_public_geoscience: %s — 0 rows, skipping.", label)
        return
    written = 0
    for start in range(0, total, batch_size):
        batch = rows[start : start + batch_size]
        with session.begin_transaction() as tx:
            tx.run(cypher, rows=batch)
            tx.commit()
        written += len(batch)
        context.log.info(
            "gold_public_geoscience: %s — wrote %d / %d", label, written, total,
        )


# ---------------------------------------------------------------------------
# Asset
# ---------------------------------------------------------------------------

@asset(
    group_name="gold",
    deps=[
        silver_pg_ca_sk_mine_loc,
        silver_pg_ca_sk_smdi,
        silver_pg_ca_sk_drillhole,
        silver_pg_ca_sk_resource_potential,
        silver_pg_ca_sk_rock_samples,
        silver_pg_ca_sk_assessment_underground,
        silver_pg_ca_sk_assessment_ground,
        silver_pg_ca_sk_assessment_airborne,
    ],
    description=(
        "Populate the Neo4j knowledge graph with Public Geoscience entities "
        "from the Phase-2.3 canonical PostGIS tables. Creates Jurisdiction, "
        "PublicGeoSource, Commodity, Mine, MineralOccurrence, Drillhole, "
        "ResourcePotentialZone, RockSample, and AssessmentSurvey nodes "
        "(entity nodes carry :PublicGeo as a secondary label) plus "
        "SOURCED_FROM / PUBLISHED_BY / HAS_COMMODITY / COVERS_AREA_FOR "
        "relationships per plan §06b. All writes are MERGE-based and safe "
        "to re-run."
    ),
)
def gold_public_geoscience_neo4j(
    context: AssetExecutionContext,
    config: GoldPublicGeoscienceConfig,
    postgres: PostgresResource,
    neo4j: Neo4jResource,
) -> MaterializeResult:
    context.log.info("gold_public_geoscience: reading canonical PostGIS tables")

    jurisdictions       = _rows_for_cypher(_fetch_dicts(postgres, JURISDICTIONS_SQL))
    sources             = _rows_for_cypher(_fetch_dicts(postgres, SOURCES_SQL))
    commodities         = _rows_for_cypher(_fetch_dicts(postgres, COMMODITY_ALIASES_SQL))
    mines               = _rows_for_cypher(_fetch_dicts(postgres, MINES_SQL))
    occurrences         = _rows_for_cypher(_fetch_dicts(postgres, OCCURRENCES_SQL))
    drillholes          = _rows_for_cypher(_fetch_dicts(postgres, DRILLHOLES_SQL))
    resource_potential  = _rows_for_cypher(_fetch_dicts(postgres, RESOURCE_POTENTIAL_SQL))
    rock_samples        = _rows_for_cypher(_fetch_dicts(postgres, ROCK_SAMPLES_SQL))
    assessment_surveys  = _rows_for_cypher(_fetch_dicts(postgres, ASSESSMENT_SURVEYS_SQL))

    context.log.info(
        "Canonical rows — jurisdictions=%d sources=%d commodities=%d "
        "mines=%d occurrences=%d drillholes=%d resource_potential=%d "
        "rock_samples=%d assessment_surveys=%d",
        len(jurisdictions), len(sources), len(commodities),
        len(mines), len(occurrences), len(drillholes), len(resource_potential),
        len(rock_samples), len(assessment_surveys),
    )

    if (len(mines) + len(occurrences) + len(drillholes) + len(resource_potential)
            + len(rock_samples) + len(assessment_surveys) == 0):
        context.log.warning(
            "No canonical entities present — Silver must run before Gold. "
            "Returning without writing to Neo4j.",
        )
        return MaterializeResult(
            metadata={
                "jurisdictions":           MetadataValue.int(0),
                "sources":                 MetadataValue.int(0),
                "commodities":             MetadataValue.int(0),
                "mines":                   MetadataValue.int(0),
                "occurrences":             MetadataValue.int(0),
                "drillholes":              MetadataValue.int(0),
                "resource_potential_zones": MetadataValue.int(0),
                "rock_samples":            MetadataValue.int(0),
                "assessment_surveys":      MetadataValue.int(0),
            }
        )

    driver = neo4j.get_driver()
    try:
        with driver.session(database="neo4j") as session:
            # ── Constraints + indexes (idempotent) ──────────────────────
            for ddl in CREATE_CONSTRAINTS_CYPHER:
                session.run(ddl)
            for ddl in CREATE_INDEXES_CYPHER:
                session.run(ddl)
            # V1.2 schema-rename cleanup: drop the legacy index named after
            # the old `smdi_id` column (if it was created on a previous
            # run). The replacement `occ_external_id` is created above.
            session.run("DROP INDEX occ_smdi IF EXISTS")
            # 2026-06-24: drop a legacy UNIQUENESS constraint on
            # MineralOccurrence.name. An earlier schema version made `name`
            # unique, but occurrence names are NOT unique (217k rows / ~19.6k
            # distinct names — e.g. "Cluff Lake Radioactive Boulder Train"
            # repeats). The canonical key is `pg_id` (occ_pg_id, created above).
            # While the stale constraint lingers, the MERGE fails with
            # "22N80: Index entry conflict" the moment a duplicate name is
            # written — blocking the whole asset (and its gold_cross_corpus_linker
            # dependent). CREATE CONSTRAINT IF NOT EXISTS never removes it, so
            # drop it explicitly here, idempotently.
            session.run("DROP CONSTRAINT mineral_occurrence_name_unique IF EXISTS")
            context.log.info("Constraints + indexes ensured")

            # ── Vocabulary / registry (must land before entities) ──────
            _run_in_batches(session, MERGE_JURISDICTIONS_CYPHER,
                            jurisdictions, config.batch_size, context, "jurisdictions")
            _run_in_batches(session, MERGE_SOURCES_CYPHER,
                            sources, config.batch_size, context, "sources")
            _run_in_batches(session, MERGE_COMMODITIES_CYPHER,
                            commodities, config.batch_size, context, "commodities")

            # ── Entities ───────────────────────────────────────────────
            _run_in_batches(session, MERGE_MINES_CYPHER,
                            mines, config.batch_size, context, "mines")
            _run_in_batches(session, MERGE_OCCURRENCES_CYPHER,
                            occurrences, config.batch_size, context, "occurrences")
            _run_in_batches(session, MERGE_DRILLHOLES_CYPHER,
                            drillholes, config.batch_size, context, "drillholes")
            _run_in_batches(session, MERGE_RESOURCE_POTENTIAL_CYPHER,
                            resource_potential, config.batch_size, context,
                            "resource_potential_zones")
            _run_in_batches(session, MERGE_ROCK_SAMPLES_CYPHER,
                            rock_samples, config.batch_size, context,
                            "rock_samples")
            _run_in_batches(session, MERGE_ASSESSMENT_SURVEYS_CYPHER,
                            assessment_surveys, config.batch_size, context,
                            "assessment_surveys")

            # ── Count per label for metadata ───────────────────────────
            counts: dict[str, int] = {}
            for record in session.run(COUNT_CYPHER):
                labels = tuple(sorted(record["labels"]))
                counts[",".join(labels)] = int(record["n"])
    finally:
        driver.close()

    context.log.info("gold_public_geoscience: Neo4j population complete")

    return MaterializeResult(
        metadata={
            "jurisdictions":           MetadataValue.int(len(jurisdictions)),
            "sources":                 MetadataValue.int(len(sources)),
            "commodities":             MetadataValue.int(len(commodities)),
            "mines":                   MetadataValue.int(len(mines)),
            "occurrences":             MetadataValue.int(len(occurrences)),
            "drillholes":              MetadataValue.int(len(drillholes)),
            "resource_potential_zones": MetadataValue.int(len(resource_potential)),
            "rock_samples":            MetadataValue.int(len(rock_samples)),
            "assessment_surveys":      MetadataValue.int(len(assessment_surveys)),
            "graph_label_counts":      MetadataValue.json(counts),
        }
    )
