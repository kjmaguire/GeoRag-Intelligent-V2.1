# Module 8 (Map & Tile Layer) — pre-approved intake items

Items flagged during Modules 1–3 that Kyle has pre-approved for Module 8 execution.
Landed here as canonical handoff so Module 8 Phase A picks them up first.

## Silver-trapped data needs tile functions

- **Raised:** 2026-04-20 Module 3 Chunk 1 close-out
- **Source:** `ops/audit/2026-04-20-ingestion-audit.md` Chunk 1 deferred items
- **Finding:** `silver.seismic_surveys` (bbox geometry) and `silver.geochemistry` (point geometry) have PostGIS geometry columns but no Martin tile functions to serve them on the map.
- **Approach:** If Module 8 decides these layers are V1 map features:
  1. Add tile function `pg_seismic_by_project` returning MVT bytea for seismic survey footprints
  2. Add tile function `pg_geochem_by_project` returning MVT bytea for geochem point clusters
  3. Wire both into the Laravel tile proxy
  - Both depend on the (bytea, etag_hash) function signature extension being in place per addendum §05d (Module 8 Phase B scope)
- **Approval:** pre-approved 2026-04-20 — Module 8 decides whether V1 includes these layers or defers to V1.5
- **Owner:** devops-engineer / frontend-engineer during Module 8
