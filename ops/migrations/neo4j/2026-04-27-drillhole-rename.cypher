// =============================================================================
// GeoRAG Neo4j Migration — 2026-04-27 — Rename :Drillhole → :DrillHole
// =============================================================================
//
// Purpose
// -------
// Rename the live Neo4j label `Drillhole` to `DrillHole` to match the spec
// spelling in `georag-architecture.html` §04f (Global Invariant 4: PascalCase
// entity names). The spec form `DrillHole` has always returned zero rows from
// live queries because all 33,510 nodes carry the lowercase-h label.
//
// Owner decision: D2 in docs/kyle-decisions.md — approved 2026-04-27.
//
// Blast radius
// ------------
// 33,510 nodes labelled :Drillhole in Neo4j Community 2026.03.1.
// No `:DrillHole` nodes exist pre-migration (confirmed by pre-flight below).
// All relationships attached to these nodes (HAS_HOLE, LOCATED_IN,
// HAS_LITHOLOGY, SOURCED_FROM, HAS_COMMODITY, TARGETS, INTERSECTS) are
// preserved — Neo4j label operations do not touch relationships.
//
// Expected duration
// -----------------
// SET + REMOVE on 33,510 nodes: ~2–5 seconds on a 4G page-cache instance.
// The CALL { } IN TRANSACTIONS batched form below avoids a single large tx.
//
// APOC status
// -----------
// APOC is not enabled (Community Edition + free-licensing rule — no GPL deps
// beyond Neo4j itself). This migration uses the native Cypher label SET/REMOVE
// syntax, which is supported in all Community Edition 5.x / 2026 versions.
//
// Rollback
// --------
// To roll back, run the inverse migration:
//
//   CALL {
//       MATCH (n:DrillHole) WHERE NOT n:Drillhole
//       SET n:Drillhole REMOVE n:DrillHole
//   } IN TRANSACTIONS OF 1000 ROWS;
//
//   DROP CONSTRAINT drillhole_hole_id_unique IF EXISTS;
//   DROP INDEX drillhole_type IF EXISTS;
//   CREATE CONSTRAINT drillhole_collar_id_unique IF NOT EXISTS
//       FOR (d:Drillhole) REQUIRE d.collar_id IS UNIQUE;
//   CREATE CONSTRAINT drillhole_collar_id_internal IF NOT EXISTS
//       FOR (d:Drillhole) REQUIRE d.collar_id IS UNIQUE;
//
// Coordination
// ------------
// This is a Global Invariant 4 schema change. BOTH data AND code must move
// together. Sequence:
//   1. Stop Dagster ingestion (prevents new :Drillhole nodes being written
//      mid-migration by index_neo4j or gold_public_geoscience assets).
//   2. Run this migration script (all three sections below).
//   3. Deploy updated application code (FastAPI + Dagster images with
//      DrillHole label in all Cypher strings).
//   4. Restart services.
//   5. Run smoke test (see ops/runbooks/drillhole-label-rename.md).
//
// Reference: docs/kyle-decisions.md D2
// =============================================================================


// =============================================================================
// SECTION 1 — PRE-FLIGHT CHECKS
// Run these manually before applying the migration. Confirm:
//   a) :Drillhole count matches expected (~33,510)
//   b) :DrillHole count is 0 (no nodes already have the target label)
// =============================================================================

MATCH (n:Drillhole) RETURN count(n) AS drillhole_count_before;
MATCH (n:DrillHole) RETURN count(n) AS drillhole_camel_count_before;


// =============================================================================
// SECTION 2 — LABEL MIGRATION (batched, 1000 rows per transaction)
// =============================================================================

// Step 2a: Add :DrillHole label and remove :Drillhole on all nodes.
// CALL { } IN TRANSACTIONS batches the work so Neo4j does not build a single
// 33k-node transaction. Each batch commits independently — safe to interrupt
// and resume (nodes that were already renamed in an earlier run have no
// :Drillhole label so they are simply not matched again).
CALL {
    MATCH (n:Drillhole)
    SET n:DrillHole
    REMOVE n:Drillhole
} IN TRANSACTIONS OF 1000 ROWS;


// Step 2b: Drop the old Drillhole-keyed constraints.
// These were created by populate_neo4j.py (drillhole_collar_id_unique) and
// index_neo4j.py (drillhole_collar_id_internal). Both reference the old label.
DROP CONSTRAINT drillhole_collar_id_unique IF EXISTS;
DROP CONSTRAINT drillhole_collar_id_internal IF EXISTS;

// Also drop any old auto-generated constraint names that may exist from early
// pre-stable-name environments (see populate_neo4j.py _create_constraints).
DROP CONSTRAINT constraint_14259a2a IF EXISTS;
DROP CONSTRAINT constraint_399688bd IF EXISTS;
DROP CONSTRAINT constraint_3b07bdf8 IF EXISTS;


// Step 2c: Recreate constraints under the new label.
// drillhole_hole_id_unique already exists in init-schema.cypher with the
// correct :DrillHole label — IF NOT EXISTS makes this safe to rerun.
CREATE CONSTRAINT drillhole_hole_id_unique IF NOT EXISTS
    FOR (h:DrillHole) REQUIRE h.hole_id IS UNIQUE;

// Internal collar_id unique constraint (used by index_neo4j / populate_neo4j).
CREATE CONSTRAINT drillhole_collar_id_unique IF NOT EXISTS
    FOR (d:DrillHole) REQUIRE d.collar_id IS UNIQUE;

// Step 2d: Recreate the drillhole_type range index under the new label.
// This index was created by init-schema.cypher on :DrillHole already — the
// IF NOT EXISTS guard means this is a no-op if init-schema ran post-migration.
CREATE INDEX drillhole_type IF NOT EXISTS
    FOR (h:DrillHole) ON (h.type);


// =============================================================================
// SECTION 3 — POST-VALIDATION COUNTS
// Run after migration. Confirm:
//   a) :Drillhole count is 0
//   b) :DrillHole count matches the pre-flight :Drillhole count (~33,510)
// =============================================================================

MATCH (n:Drillhole) RETURN count(n) AS drillhole_count_after;
MATCH (n:DrillHole) RETURN count(n) AS drillhole_camel_count_after;


// =============================================================================
// SECTION 4 — SMOKE TEST (run after code deployment)
// Replace <A_KNOWN_HOLE_ID> with an actual hole_id from the project.
// Expected: exactly 1 result with the hole_id you supplied.
// =============================================================================

// MATCH (h:DrillHole {hole_id: '<A_KNOWN_HOLE_ID>'}) RETURN h.hole_id, labels(h);
