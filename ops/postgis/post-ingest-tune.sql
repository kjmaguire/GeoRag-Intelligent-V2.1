-- =============================================================================
-- GeoRAG — Post-Ingest Tune Script
-- =============================================================================
-- Produced by: devops-engineer agent (Claude Sonnet 4.6)
-- Date: 2026-04-19 (Module 2 Phase B, Item 2)
-- Authority: 02-data-stores-hardening.md §B2
--
-- PURPOSE:
--   Called at the tail of each Module 3+ ingestion run to CLUSTER the primary
--   spatial table, force ANALYZE for fresh planner statistics, and refresh any
--   materialized views downstream of the ingestion target.
--
-- USAGE:
--   psql -U georag -d georag \
--     -v target_table=silver.collars \
--     -v geom_idx=idx_collars_geom \
--     -v matview=silver.mv_collar_summary \
--     -f ops/postgis/post-ingest-tune.sql
--
-- PARAMETERS (all required — set with psql -v key=value):
--   :target_table  — fully-qualified table name, e.g. silver.collars
--   :geom_idx      — name of the GIST index on that table used for CLUSTER
--   :matview       — fully-qualified materialized view to refresh afterward
--                    (set to 'none' to skip the REFRESH step)
--
-- KNOWN TABLE / INDEX / MATVIEW PAIRS (2026-04-19 inventory):
--
--   silver layer (populated by Module 3 ingestion):
--     target_table=silver.collars            geom_idx=idx_collars_geom           matview=silver.mv_collar_summary
--     target_table=silver.reports            geom_idx=idx_reports_geom            matview=none
--     target_table=silver.spatial_features   geom_idx=idx_spatial_features_geom   matview=none
--     target_table=silver.seismic_surveys    geom_idx=idx_seismic_surveys_bbox    matview=none   (bbox column)
--
--   public_geoscience layer (populated by Module 3 public-data ingest):
--     Use the per-table index names from add-missing-gist-indices.sql.
--     No materialized views exist on public_geoscience tables yet.
--
-- NOTES:
--   - CLUSTER rewrites the table in GIST order, which dramatically speeds up
--     spatial range queries on tiles with small bounding boxes (ST_Intersects,
--     ST_Within). It acquires an ACCESS EXCLUSIVE lock — run only when the
--     ingestion job is the sole writer (Dagster pipeline tail).
--   - ANALYZE WITHOUT STATISTICS RESET (plain ANALYZE) refreshes planner
--     stats. Runs after CLUSTER because CLUSTER voids the previous stats.
--   - REFRESH MATERIALIZED VIEW CONCURRENTLY requires a UNIQUE index on the
--     view (present on mv_collar_summary). It does NOT block readers.
--   - \set ON_ERROR_STOP on ensures Dagster sees a non-zero exit code on
--     any SQL failure and can retry or alert.
--
-- DAGSTER INTEGRATION (Module 3):
--   Add a post-op step to each spatial asset that calls this script via
--   subprocess or a psycopg execute. Example in Python:
--
--     import subprocess
--     subprocess.run([
--         "psql", "-U", "georag", "-d", "georag",
--         "-v", f"target_table={table}",
--         "-v", f"geom_idx={idx}",
--         "-v", f"matview={mv}",
--         "-f", "/ops/postgis/post-ingest-tune.sql",
--     ], check=True)
-- =============================================================================

\set ON_ERROR_STOP on

-- Step 1: Cluster table on its GIST index for spatial locality
-- ACCESS EXCLUSIVE lock — pipeline must be the sole writer
CLUSTER :target_table USING :geom_idx;

-- Step 2: Refresh planner statistics after CLUSTER rewrites the physical layout
ANALYZE :target_table;

-- Step 3: Refresh downstream materialized view (skip if matview='none')
-- Use DO block to allow conditional skip without a client-side if/else
DO $$
BEGIN
    IF :'matview' <> 'none' THEN
        EXECUTE 'REFRESH MATERIALIZED VIEW CONCURRENTLY ' || :'matview';
    END IF;
END;
$$;
