-- =============================================================================
-- GeoRAG Phase 0 — Step 1 extensions and Step 2 schema namespaces
--
-- This script runs after init-postgis.sql on first DB init (alphabetic order
-- in /docker-entrypoint-initdb.d/: '1' comes before 'i'). On an already-
-- initialized DB the postgres entrypoint skips initdb scripts entirely; for
-- existing dev databases the same statements are applied via psql one-shot
-- (see scripts/phase0_apply_extensions.sh).
--
-- All statements are idempotent (CREATE ... IF NOT EXISTS / DO blocks).
--
-- Phase 0 reference:
--   - kickoff Step 1 done definition (the 10-extension query)
--   - kickoff Step 2 schema namespaces
--   - master plan §22.1 + §3.2 (with the public_geo → public_geoscience
--     naming override locked 2026-05-09; see project memory)
-- =============================================================================

-- =============================================================================
-- Phase 0 EXTENSIONS — beyond what init-postgis.sql installs
-- =============================================================================

-- auto_explain: built into Postgres core. The .so is loaded via
-- shared_preload_libraries (set in docker-compose command:); CREATE EXTENSION
-- registers the (small) catalog metadata. Without preload, "extension" still
-- works but the auto-logging behavior is inactive.
--
-- 2026-06-03 — Wrapped in a tolerant DO block. The vanilla postgis/postgis
-- Debian image used by the migration-ordering verifier (see
-- scripts/verify_migration_ordering.sh) doesn't ship the auto_explain
-- contrib `.so`, so `CREATE EXTENSION IF NOT EXISTS auto_explain` raises
-- "extension not available". The DO block checks pg_available_extensions
-- and skips silently when missing. Production PG has it; dev / verifier
-- containers don't.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'auto_explain') THEN
        CREATE EXTENSION IF NOT EXISTS auto_explain;
    ELSE
        RAISE NOTICE 'auto_explain not available in this Postgres build — skipping. Production should have it via contrib package.';
    END IF;
END
$$;

-- h3: geospatial hex indexing. Used by gold-tier aggregation and (Phase 5+)
-- target scoring grid math. h3_postgis requires postgis_raster, which has a
-- hard guard requiring it to be installed in the same schema as postgis
-- itself (public). The session-scoped search_path swap below satisfies that
-- guard regardless of any database-level search_path override.
CREATE EXTENSION IF NOT EXISTS h3;
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'postgis_raster') THEN
    SET LOCAL search_path = public;
    CREATE EXTENSION postgis_raster;
  END IF;
END $$;
CREATE EXTENSION IF NOT EXISTS h3_postgis;

-- hypopg: hypothetical indexes — Index Health Agent (Phase 0 agent #4) uses
-- this to evaluate "would adding index X improve plan cost?" without actually
-- creating the index.
CREATE EXTENSION IF NOT EXISTS hypopg;

-- pg_stat_kcache: kernel-level CPU + I/O stats per query. Pairs with
-- pg_stat_statements for actual-resource attribution (track_io_timing alone
-- gives DB-side timing; kcache adds OS-side context-switch + CPU time).
CREATE EXTENSION IF NOT EXISTS pg_stat_kcache;

-- pg_partman: declarative partition maintenance. Phase 0 uses it for
-- audit_ledger (monthly) and workflow_runs (monthly); Phase 1+ may add more.
-- Lives in its own schema by convention so its own catalog tables don't
-- collide with application tables.
CREATE SCHEMA IF NOT EXISTS partman;
CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman;

-- pg_repack: online table reorg without exclusive locks. No upfront work
-- here — pg_repack is invoked on-demand from cron/Hatchet when bloat
-- accumulates on a hot table.
CREATE EXTENSION IF NOT EXISTS pg_repack;

-- pg_ivm: incremental view maintenance. Phase 0 doesn't yet use IMMV
-- (Incrementally Maintained Materialized Views), but the extension lives
-- ready so Phase 5+ aggregations don't have to retrofit it onto a
-- populated database.
CREATE EXTENSION IF NOT EXISTS pg_ivm;

-- =============================================================================
-- Phase 0 SCHEMA NAMESPACES — beyond bronze/silver/gold/index from init-postgis
-- =============================================================================
--
-- Master plan §3.2 prescribes 8 namespaces; we already have silver, gold,
-- and bronze (legacy) plus public_geoscience (legacy alias for public_geo —
-- see memory: phase 0 decision #2 keeps the existing name). The other 5
-- need to exist for Step 2 schema deployment.

CREATE SCHEMA IF NOT EXISTS audit;       -- audit_ledger, audit_ledger_verification_runs
CREATE SCHEMA IF NOT EXISTS usage;       -- usage_events, usage_aggregates_daily, workspace_cost_ceilings
CREATE SCHEMA IF NOT EXISTS outbox;      -- pending_propagations, propagation_attempts
CREATE SCHEMA IF NOT EXISTS workflow;    -- workflow_runs, workflow_run_events, workflow_run_steps
CREATE SCHEMA IF NOT EXISTS workspace;   -- workspaces, users, memberships, roles, agent_timeouts, prompt_versions, ...

-- Application role (georag) needs USAGE + CREATE on each so Laravel/FastAPI
-- migrations can attach tables. Mirrors the pattern in init-postgis.sql.
DO $$
DECLARE
    s text;
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = current_user AND rolsuper = false) THEN
        FOREACH s IN ARRAY ARRAY['audit','usage','outbox','workflow','workspace'] LOOP
            EXECUTE 'GRANT USAGE  ON SCHEMA ' || quote_ident(s) || ' TO ' || quote_ident(current_user);
            EXECUTE 'GRANT CREATE ON SCHEMA ' || quote_ident(s) || ' TO ' || quote_ident(current_user);
        END LOOP;
    END IF;
END $$;

-- =============================================================================
-- VERIFICATION (logged at init time)
-- =============================================================================

DO $$
DECLARE
    ext_count int;
    ns_count  int;
BEGIN
    SELECT count(*) INTO ext_count
    FROM pg_extension
    WHERE extname IN (
        'postgis', 'pg_trgm', 'pg_stat_statements', 'auto_explain',
        'h3', 'hypopg', 'pg_stat_kcache', 'pg_partman', 'pg_repack', 'pg_ivm'
    );
    SELECT count(*) INTO ns_count
    FROM pg_namespace
    WHERE nspname IN ('audit','usage','silver','gold','public_geoscience','outbox','workflow','workspace');
    RAISE NOTICE 'Phase 0 init: % / 10 expected extensions, % / 8 expected namespaces', ext_count, ns_count;
END $$;
