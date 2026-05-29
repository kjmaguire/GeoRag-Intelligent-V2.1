// =============================================================================
// GeoRAG Neo4j Community Edition — Schema Initialisation
// =============================================================================
// Neo4j Community Edition 2026 (5.x line)
// Run this script once on a fresh database, or safely re-run at any time
// because every statement uses IF NOT EXISTS for full idempotency.
//
// Community Edition constraint support:
//   SUPPORTED   — IS UNIQUE (uniqueness constraints)
//   NOT SUPPORTED — IS NOT NULL (existence) — Enterprise only
//   NOT SUPPORTED — IS NODE KEY             — Enterprise only
//
// All CREATE INDEX statements use RANGE provider (the 5.x default), which
// supports equality and range predicates and supersedes the old b-tree index.
//
// Owned by: graph-engineer agent
// Architecture reference: Section 04f (entity model), Section 06b (perf config)
// =============================================================================

// -----------------------------------------------------------------------------
// SECTION 1 — UNIQUENESS CONSTRAINTS
// Each constraint implicitly creates a backing index on the constrained
// property, so no separate index is needed for those lookup paths.
// -----------------------------------------------------------------------------

// Project.name — projects are the root anchor; cross-project MERGE operations
// depend on this being unique and fast.
CREATE CONSTRAINT project_name_unique IF NOT EXISTS
    FOR (p:Project) REQUIRE p.name IS UNIQUE;

// DrillHole.hole_id — hole IDs are the primary lookup key in ingestion MERGE
// operations and in CITES_DRILLHOLE citation queries.
CREATE CONSTRAINT drillhole_hole_id_unique IF NOT EXISTS
    FOR (h:DrillHole) REQUIRE h.hole_id IS UNIQUE;

// Formation.name — formation names are referenced by both Report and
// MineralOccurrence nodes via REFERENCES_FORMATION / HOSTED_BY_FORMATION.
// Uniqueness prevents duplicate formation nodes from mis-matched spelling
// variants that slip through entity resolution.
CREATE CONSTRAINT formation_name_unique IF NOT EXISTS
    FOR (f:Formation) REQUIRE f.name IS UNIQUE;

// Report.title — NI 43-101 reports are identified by title during ingestion.
// Uniqueness guards against re-processing the same document creating duplicate
// Report nodes that would pollute citation provenance chains.
CREATE CONSTRAINT report_title_unique IF NOT EXISTS
    FOR (r:Report) REQUIRE r.title IS UNIQUE;

// Publication.title — academic publications are deduplicated on title before
// CITES_DATA_FROM relationships are created.
CREATE CONSTRAINT publication_title_unique IF NOT EXISTS
    FOR (pub:Publication) REQUIRE pub.title IS UNIQUE;

// -----------------------------------------------------------------------------
// SECTION 2 — RANGE INDICES
// Cover properties that are not part of a uniqueness constraint but appear
// frequently in WHERE clauses, ORDER BY, or relationship traversal filters.
// Targeting NodeIndexSeek over AllNodesScan (verify with PROFILE during dev).
// -----------------------------------------------------------------------------

// MineralOccurrence.commodity — commodity filter is the most common WHERE
// predicate on MineralOccurrence nodes ("show me all gold occurrences").
CREATE INDEX mineral_occurrence_commodity IF NOT EXISTS
    FOR (m:MineralOccurrence) ON (m.commodity);

// GeophysicalSurvey.type — surveys are filtered by type (gravity, magnetics,
// IP, etc.) in anomaly-correlation queries.
CREATE INDEX geophysical_survey_type IF NOT EXISTS
    FOR (s:GeophysicalSurvey) ON (s.type);

// Report.date — reports are filtered and sorted by date in timeline queries
// and provenance chain lookups. RANGE index supports date comparisons.
CREATE INDEX report_date IF NOT EXISTS
    FOR (r:Report) ON (r.date);

// DrillHole.type — drill type filter (RC, diamond, RAB, AC) is used when
// restricting grade calculations to specific drilling methods.
CREATE INDEX drillhole_type IF NOT EXISTS
    FOR (h:DrillHole) ON (h.type);

// Formation.age — geological age is queried in stratigraphic traversals
// (OVERLIES / UNDERLIES) to filter by era or period.
CREATE INDEX formation_age IF NOT EXISTS
    FOR (f:Formation) ON (f.age);

// Project.region — multi-project regional queries filter on region before
// traversing into project subgraphs. Avoids full Project scan.
CREATE INDEX project_region IF NOT EXISTS
    FOR (p:Project) ON (p.region);

// Project.commodity — commodity-level project filtering for portfolio views
// ("all gold projects in Western Australia").
CREATE INDEX project_commodity IF NOT EXISTS
    FOR (p:Project) ON (p.commodity);

// MineralOccurrence.deposit_type — deposit type filter pairs with commodity
// in grade/tonnage classification queries.
CREATE INDEX mineral_occurrence_deposit_type IF NOT EXISTS
    FOR (m:MineralOccurrence) ON (m.deposit_type);

// GeophysicalSurvey.date — survey date range queries for temporal analysis
// of geophysical anomaly evolution over successive survey campaigns.
CREATE INDEX geophysical_survey_date IF NOT EXISTS
    FOR (s:GeophysicalSurvey) ON (s.date);

// Publication.year — year-based filtering and citation age analysis.
CREATE INDEX publication_year IF NOT EXISTS
    FOR (pub:Publication) ON (pub.year);
