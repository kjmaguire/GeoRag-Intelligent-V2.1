-- =============================================================================
-- GeoRAG — Missing GIST Indices (for Kyle review)
-- =============================================================================
-- Produced by: devops-engineer agent (Claude Sonnet 4.6)
-- Date: 2026-04-19 (Module 2 Phase B, Item 2)
-- Authority: 02-data-stores-hardening.md §B2
--
-- STATUS: EXECUTED 2026-04-19 (Module 2 Phase C, Open Item 1).
-- Authorized by Kyle. All 9 CREATE INDEX statements completed successfully.
-- Execution was instant (8 history tables had 0 rows; seismic_surveys had 1 row).
-- pg_indexes verified: all 9 new indices ONLINE. See Phase C closeout in
-- ops/audit/2026-04-19-datastores-audit.md appendix.
--
-- How to apply:
--   psql -U georag -d georag -f ops/postgis/add-missing-gist-indices.sql
-- Or apply individual statements selectively.
--
-- GIST index inventory (2026-04-19):
--   COVERED (13 tables):
--     public_geoscience: jurisdictions, pg_assessment_survey, pg_bedrock_geology,
--       pg_drillhole_collar, pg_mine, pg_mineral_disposition, pg_mineral_occurrence,
--       pg_resource_potential_zone, pg_rock_sample
--     silver: collars, raster_layers, reports, spatial_features
--
--   MISSING (9 real tables — 8 _history + 1 silver.seismic_surveys):
--     History tables currently have 0 rows (pre-ingestion). Index build is
--     effectively instantaneous now; apply before Module 3 ingestion begins.
--     silver.seismic_surveys has 1 row.
--
--   VIEWS (8, no index possible):
--     public_geoscience: v_pg_assessment_surveys_mvt, v_pg_bedrock_geology_mvt,
--       v_pg_drillhole_collars_mvt, v_pg_mineral_dispositions_mvt,
--       v_pg_mineral_occurrences_mvt, v_pg_mines_mvt, v_pg_resource_potential_mvt,
--       v_pg_rock_samples_mvt
--     Views cannot have indices — the underlying base tables are already indexed.
-- =============================================================================

\set ON_ERROR_STOP on

-- ---------------------------------------------------------------------------
-- public_geoscience — _history tables
-- All are currently empty (0 rows, 2026-04-19). Index builds are instant.
-- Apply before Module 3 populates these tables.
-- ---------------------------------------------------------------------------

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_pg_as_history_geom
    ON public_geoscience.pg_assessment_survey_history USING GIST (geom);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_pg_bg_history_geom
    ON public_geoscience.pg_bedrock_geology_history USING GIST (geom);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_pg_dc_history_geom
    ON public_geoscience.pg_drillhole_collar_history USING GIST (geom);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_pg_mine_history_geom
    ON public_geoscience.pg_mine_history USING GIST (geom);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_pg_md_history_geom
    ON public_geoscience.pg_mineral_disposition_history USING GIST (geom);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_pg_mo_history_geom
    ON public_geoscience.pg_mineral_occurrence_history USING GIST (geom);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_pg_rpz_history_geom
    ON public_geoscience.pg_resource_potential_zone_history USING GIST (geom);

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_pg_rs_history_geom
    ON public_geoscience.pg_rock_sample_history USING GIST (geom);

-- ---------------------------------------------------------------------------
-- silver.seismic_surveys — bbox column (1 row currently)
-- ---------------------------------------------------------------------------

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_seismic_surveys_bbox
    ON silver.seismic_surveys USING GIST (bbox);

-- Verify coverage after applying
SELECT f_table_schema, f_table_name, f_geometry_column,
       CASE WHEN EXISTS (
           SELECT 1 FROM pg_indexes pi
           WHERE pi.schemaname = gc.f_table_schema
             AND pi.tablename = gc.f_table_name
             AND pi.indexdef ILIKE '%GIST%'
             AND pi.indexdef ILIKE '%' || gc.f_geometry_column || '%'
       ) THEN 'COVERED' ELSE 'MISSING' END AS gist_status
FROM geometry_columns gc
WHERE gc.f_table_schema NOT IN ('information_schema', 'pg_catalog')
ORDER BY f_table_schema, f_table_name;
