-- GeoRAG — PostgreSQL 18.3 Initialization Script
-- Runs automatically on first container start (postgres entrypoint sources
-- every *.sql file in /docker-entrypoint-initdb.d/ in filename order).
--
-- This script is IDEMPOTENT: all CREATE statements use IF NOT EXISTS so
-- re-running against an already-initialized database is safe.
--
-- Medallion architecture schema layout (Section 04):
--   bronze  — raw ingested data, immutable records, append-only
--   silver  — cleaned, validated, normalized records
--   gold    — aggregated, project-ready analytical outputs
--   index   — full-text search indices, pre-computed lookup tables
--
-- Extensions are installed in the public schema (PostgreSQL convention).
-- Schemas are created without objects here; individual service migrations
-- (Laravel, FastAPI, Dagster) own the tables within each schema.

-- ============================================================================
-- EXTENSIONS
-- ============================================================================

-- PostGIS: geometry types, spatial functions, coordinate reference system
-- support. Required for all GIS layer storage and PostGIS 3.6 spatial queries.
-- Must be installed before postgis_topology (topology depends on postgis).
CREATE EXTENSION IF NOT EXISTS postgis;

-- PostGIS topology: planar graph model for topological data (polygon
-- adjacency, shared boundaries). Used by the gold layer for formation
-- boundary analysis and area-of-interest polygon operations.
CREATE EXTENSION IF NOT EXISTS postgis_topology;

-- pg_trgm: trigram-based similarity indices for fuzzy text search.
-- Powers the formation name autocomplete and report title search features
-- without a separate Elasticsearch dependency. Enables GIN/GIST index types
-- on text columns for fast ILIKE and similarity() queries.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- uuid-ossp: UUID generation functions (uuid_generate_v4() etc.).
-- Used as the default value source for primary key columns across all schemas.
-- gen_random_uuid() (pgcrypto) is available in PG 13+, but uuid-ossp is
-- explicit here because some legacy scripts reference uuid_generate_v4().
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- pg_stat_statements: query-level execution statistics.
-- Loaded via shared_preload_libraries in postgresql.conf. The CREATE
-- EXTENSION call registers the view; the library must already be loaded.
-- Used by Prometheus postgres_exporter to surface slow query metrics.
CREATE EXTENSION IF NOT EXISTS pg_stat_statements;

-- ============================================================================
-- SCHEMAS — Medallion Architecture
-- ============================================================================

-- bronze: raw, immutable ingested records.
-- Files land here as-received from the ingestion pipeline. No transformation,
-- no validation. Append-only by convention (enforced at the application layer).
-- PostGIS geometry columns in this schema store raw coordinates as-parsed
-- from source files — CRS may be non-WGS84 until the silver transform.
CREATE SCHEMA IF NOT EXISTS bronze;

-- silver: cleaned, validated, CRS-normalized records.
-- All geometry is reprojected to EPSG:4326 (WGS84) at this layer.
-- Mandatory fields are NOT NULL. Foreign keys are enforced.
-- This is the primary read layer for FastAPI domain queries.
CREATE SCHEMA IF NOT EXISTS silver;

-- gold: aggregated and analytical outputs.
-- Pre-computed joins, rollups, and project-scoped views that are expensive
-- to compute at query time. Materialized views in this schema are refreshed
-- by Dagster pipeline jobs on a schedule. The Laravel API reads from gold
-- for dashboard and export endpoints.
CREATE SCHEMA IF NOT EXISTS gold;

-- index: full-text search and lookup support structures.
-- pg_trgm GIN indices, tsvector columns, pre-built entity resolution lookup
-- tables, and denormalized name→id maps. Kept separate from silver/gold so
-- index rebuild operations (REINDEX, REFRESH MATERIALIZED VIEW) don't
-- require locking core data tables.
CREATE SCHEMA IF NOT EXISTS index;

-- audit: app/audit data — query_audit_log, answer_runs, etc. Per §05 step 6
-- of georag-architecture.html, audit data must NOT live in the geological
-- domain schema (silver/bronze/gold/index) nor be muddled with Laravel
-- internals in `public`. The audit role's INSERT-only grant is scoped to
-- this schema so a compromised application path that runs as `georag_audit`
-- cannot reach geological tables. Migration that moves query_audit_log
-- into here: 2026_05_07_120000_move_query_audit_log_to_audit_schema.php.
CREATE SCHEMA IF NOT EXISTS audit;

-- ============================================================================
-- SCHEMA SEARCH PATH
-- ============================================================================

-- Set the default search path so unqualified table references from migrations
-- resolve to silver first (the primary application schema), then public for
-- extension objects (postgis functions, uuid_generate_v4, etc.).
-- audit is included so unqualified references to query_audit_log etc. resolve
-- after the §05 schema split.
-- Each service's migration runner should set this explicitly in its connection
-- string as well (search_path=silver,public).
ALTER DATABASE georag SET search_path TO silver, bronze, gold, index, audit, public;

-- ============================================================================
-- GRANT BASELINE PERMISSIONS
-- ============================================================================

-- The georag application role is created by the docker-compose env
-- (POSTGRES_USER). Grant USAGE on all schemas so the role can see objects
-- without needing superuser. Individual table-level grants are applied by
-- each service's migration.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper = false) THEN
        EXECUTE 'GRANT USAGE ON SCHEMA bronze, silver, gold, index, audit TO ' || quote_ident(current_user);
        EXECUTE 'GRANT CREATE ON SCHEMA bronze, silver, gold, index, audit TO ' || quote_ident(current_user);
    END IF;
END
$$;

-- ============================================================================
-- DAGSTER DATABASE
-- ============================================================================

-- Dagster stores its run history and event log in a separate database so its
-- schema migrations never collide with the application tables.
-- DAGSTER_PG_DB defaults to georag_dagster in docker-compose.yml.
SELECT 'CREATE DATABASE georag_dagster OWNER georag'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'georag_dagster')\gexec

-- ============================================================================
-- VERIFICATION
-- ============================================================================

-- Emit the installed extension versions and schema list to the init log so
-- it is easy to confirm the correct PostGIS version is loaded at startup.
DO $$
DECLARE
    v_postgis TEXT;
    v_pg      TEXT;
BEGIN
    SELECT extversion INTO v_postgis FROM pg_extension WHERE extname = 'postgis';
    SELECT version() INTO v_pg;
    RAISE NOTICE 'GeoRAG init complete — PostgreSQL: % | PostGIS: %', v_pg, v_postgis;
    RAISE NOTICE 'Schemas created: bronze, silver, gold, index';
END
$$;
