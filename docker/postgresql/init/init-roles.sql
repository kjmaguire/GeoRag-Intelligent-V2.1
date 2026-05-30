-- =============================================================================
-- GeoRAG Database Role Setup
--
-- Creates separate roles for application security (least-privilege principle):
--   georag_read   — SELECT only on silver schema (reports, dashboards)
--   georag_write  — SELECT + INSERT + UPDATE on silver (application layer)
--   georag_admin  — Full DDL + TRUNCATE (migrations, maintenance)
--   georag_audit  — INSERT only on query_audit_log (audit trail)
--
-- Lives in /docker-entrypoint-initdb.d/ and runs automatically on first
-- container init (alphanumeric order — after init-postgis.sql which creates
-- the audit schema this file grants on).
--
-- To re-apply against an already-initialized cluster (the entrypoint skips
-- initdb.d on existing data volumes):
--   docker exec georag-postgresql psql -U georag -d georag -f /docker-entrypoint-initdb.d/init-roles.sql
-- =============================================================================

-- Create roles (idempotent)
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'georag_read') THEN
        CREATE ROLE georag_read NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'georag_write') THEN
        CREATE ROLE georag_write NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'georag_audit') THEN
        CREATE ROLE georag_audit NOLOGIN;
    END IF;
END $$;

-- Grant schema usage
GRANT USAGE ON SCHEMA silver TO georag_read, georag_write;
GRANT USAGE ON SCHEMA public TO georag_read, georag_write, georag_audit;
GRANT USAGE ON SCHEMA bronze TO georag_read, georag_write;
-- audit schema (created in init-postgis.sql) — all three roles can see it.
-- Future tables get role-appropriate grants via the ALTER DEFAULT PRIVILEGES
-- block at the bottom of this file (idempotent on re-run).
GRANT USAGE ON SCHEMA audit TO georag_read, georag_write, georag_audit;

-- Read role: SELECT on all silver + bronze tables
GRANT SELECT ON ALL TABLES IN SCHEMA silver TO georag_read;
GRANT SELECT ON ALL TABLES IN SCHEMA bronze TO georag_read;
ALTER DEFAULT PRIVILEGES IN SCHEMA silver GRANT SELECT ON TABLES TO georag_read;
ALTER DEFAULT PRIVILEGES IN SCHEMA bronze GRANT SELECT ON TABLES TO georag_read;

-- Write role: inherits read + INSERT/UPDATE on silver
GRANT georag_read TO georag_write;
GRANT INSERT, UPDATE ON ALL TABLES IN SCHEMA silver TO georag_write;
GRANT INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO georag_write;
ALTER DEFAULT PRIVILEGES IN SCHEMA silver GRANT INSERT, UPDATE ON TABLES TO georag_write;

-- Audit role: INSERT only on audit-schema tables.
-- The historical grant on `public.query_audit_log` is retained for clusters
-- that haven't yet run 2026_05_07_120000_move_query_audit_log_to_audit_schema.
-- Once that migration runs, the table lives in `audit.query_audit_log` and
-- the migration re-issues the grant on its new location. This block stays
-- defensive so init-roles.sql is correct before AND after the move.
-- Guard: public.query_audit_log is created by Laravel migration
-- 2026_04_12_000000_create_query_audit_log_table and does NOT exist at
-- Docker init time on fresh clusters. Skip this grant if the table is
-- absent — it will be re-issued by the 2026_05_07 migration when it runs.
DO $$ BEGIN
    IF EXISTS (
        SELECT FROM information_schema.tables
        WHERE table_schema = 'public' AND table_name = 'query_audit_log'
    ) THEN
        EXECUTE 'GRANT INSERT ON public.query_audit_log TO georag_audit';
    END IF;
END $$;
ALTER DEFAULT PRIVILEGES IN SCHEMA audit
    GRANT INSERT ON TABLES TO georag_audit;
ALTER DEFAULT PRIVILEGES IN SCHEMA audit
    GRANT INSERT, UPDATE, SELECT ON TABLES TO georag_write;
ALTER DEFAULT PRIVILEGES IN SCHEMA audit
    GRANT SELECT ON TABLES TO georag_read;

-- Grant sequence usage for auto-increment columns
GRANT USAGE ON ALL SEQUENCES IN SCHEMA public TO georag_write;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA silver TO georag_write;

-- The main georag user keeps full privileges (migrations + admin)
-- Application services should use georag_write for normal operations
