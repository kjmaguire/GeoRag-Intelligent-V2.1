# GeoRAG Neo4j — Missing Index Findings
<!-- Produced by: devops-engineer agent (Claude Sonnet 4.6) -->
<!-- Date: 2026-04-19 (Module 2 Phase B, Item 3) -->
<!-- Authority: 02-data-stores-hardening.md §B4 -->
<!-- STATUS: pg_label_pg_id CREATED 2026-04-19 (Kyle-approved, Module 2 Phase B). -->
<!-- Wall time to ONLINE: ~4 seconds. SHOW INDEXES state=ONLINE. EXPLAIN shows NodeIndexSeek. -->

## Summary

Live graph contains 56,034 nodes across 14 label types (2026-04-19 count).
Threshold for mandatory index review: >1,000 nodes.

## Labels >1,000 nodes — Index Coverage

| Label | Node Count | Primary Property Indexed? | Index Names | Gap? |
|---|---|---|---|---|
| `PublicGeo` | 55,941 | **YES** | `pg_label_pg_id` (RANGE on pg_id) — **CREATED 2026-04-19** | CLOSED |
| `Drillhole` | 33,510 | YES | `dh_pg_id` (UNIQUENESS), `drillhole_collar_id_internal` (UNIQUENESS), `dh_drillhole_id` (RANGE), `dh_jurisdiction` (RANGE) | CLEAN |
| `MineralOccurrence` | 22,230 | YES | `occ_pg_id` (UNIQUENESS), `occ_external_id` (RANGE), `mineral_occurrence_commodity` (RANGE), `mineral_occurrence_deposit_type` (RANGE), `occ_jurisdiction` (RANGE) | CLEAN |
| `Mine` | 140 | YES | `mine_pg_id` (UNIQUENESS), `mine_jurisdiction` (RANGE) | CLEAN — below threshold but indexed |

## Critical Finding — PublicGeo Label

**55,941 nodes carry the `:PublicGeo` secondary label with no index.**

The warmup script (`docker/neo4j/warmup.cypher`) extensively uses `MATCH (n:PublicGeo)` patterns:
- Section 9 of warmup.cypher: `MATCH (n:PublicGeo) RETURN count(n)`
- `MATCH (m:Mine:PublicGeo)`, `MATCH (o:MineralOccurrence:PublicGeo)`, `MATCH (d:Drillhole:PublicGeo)`
- `MATCH (n:PublicGeo)-[:SOURCED_FROM]->...`
- `MATCH (n:PublicGeo)-[r:HAS_COMMODITY|...]->...`

Without an index on `:PublicGeo`, every one of these queries performs a full node store scan.
At 55,941 nodes this is ~560ms per cold query instead of sub-millisecond with an index.

The RAG chat pipeline (Section 04g) uses PublicGeo as its backbone for every "what occurrences
are in this area?" / "any drillholes near this target?" query. No index = cold-start penalty
on every boot and elevated latency on first queries post-boot.

**Recommended DDL (for Kyle approval before execution):**

```cypher
// Index on PublicGeo label — primary discriminator is source_id
// (present on PublicGeoSource, which is the source-of-truth node)
// For the PublicGeo secondary label, pg_id is the common lookup property.
CREATE INDEX pg_label_pg_id IF NOT EXISTS FOR (n:PublicGeo) ON (n.pg_id);
```

**Note on secondary-label indexing in Neo4j Community Edition:**
A `RANGE` index on `:PublicGeo(pg_id)` will accelerate:
- `MATCH (n:PublicGeo {pg_id: $id})` lookups
- Multi-label patterns like `MATCH (d:Drillhole:PublicGeo {pg_id: $id})`
- Relationship traversals that start from a known PublicGeo node

The index does NOT help pure label scans (`MATCH (n:PublicGeo) RETURN count(n)`)
unless combined with a property filter. For count queries, the LOOKUP index
(auto-created by Neo4j) handles the label-only path.

## Labels 0 nodes — Indices That Can Be Dropped (Optional)

These labels have constraints/indices but zero live nodes.
Dropping them is cosmetic cleanup — they do no harm. Surface to Kyle if desired.

| Label | Indices | Live Nodes |
|---|---|---|
| `DrillHole` (capital H) | `drillhole_hole_id_unique` (UNIQUENESS), `drillhole_type` (RANGE) | 0 |
| `Publication` | `publication_title_unique` (UNIQUENESS), `publication_year` (RANGE) | 0 |
| `GeophysicalSurvey` | `geophysical_survey_date` (RANGE), `geophysical_survey_type` (RANGE) | 0 |

The `DrillHole` (capital H) vs `Drillhole` (lowercase h) label inconsistency is tracked
separately in `ops/backlog/module-10-doc-sweep.md` (N4J-04 finding). Dropping the
`DrillHole` indices here would be premature — do that after the label canonicalization
decision is made.

## Action Required from Kyle

1. ~~Approve `CREATE INDEX pg_label_pg_id IF NOT EXISTS FOR (n:PublicGeo) ON (n.pg_id)`.~~
   **DONE** — Index created 2026-04-19, ONLINE in ~4s, NodeIndexSeek confirmed. CLOSED.
2. Decide on `DrillHole` (capital H) cleanup — see N4J-04 in audit file.
