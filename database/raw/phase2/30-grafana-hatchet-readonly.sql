-- =============================================================================
-- Phase 2 Step 7 — read-only Grafana role on the `hatchet` logical DB.
--
-- Adds `grafana_hatchet_readonly` so the Grafana provisioned datasource can
-- query `v1_runs_olap` + `Workflow` + `WorkflowVersion` for the
-- /Integrations dashboard. The existing `grafana_readonly` role (if any)
-- is scoped to the `georag` DB; we keep DB-level isolation by adding a
-- dedicated role for the hatchet side.
--
-- NOSUPERUSER, NOBYPASSRLS, NOCREATEDB, NOCREATEROLE — same posture as
-- georag_app and activepieces. Read-only via SELECT-only grants; no
-- INSERT/UPDATE/DELETE anywhere.
--
-- Apply against the `hatchet` DB (the role itself is cluster-wide; the
-- grants must be issued in-DB so they bind to the hatchet objects).
-- Idempotent.
-- =============================================================================

-- 1. Cluster-wide role.
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'grafana_hatchet_readonly') THEN
        CREATE ROLE grafana_hatchet_readonly
            LOGIN
            NOSUPERUSER
            NOBYPASSRLS
            NOCREATEDB
            NOCREATEROLE
            INHERIT
            PASSWORD 'grafana-hatchet-readonly-replace-via-alter-role';
    END IF;
END $$;

-- 2. Allow CONNECT to the hatchet DB.
GRANT CONNECT ON DATABASE hatchet TO grafana_hatchet_readonly;

-- 3. Schema USAGE + table SELECT must be granted while connected to the
--    hatchet DB. The runner connects via -d hatchet for this section.
\connect hatchet

GRANT USAGE ON SCHEMA public TO grafana_hatchet_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO grafana_hatchet_readonly;

-- Future-proof: any new table the Hatchet engine creates inherits the
-- SELECT grant, so we don't need a re-grant pass after engine upgrades.
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO grafana_hatchet_readonly;

-- 4. Verification.
DO $$
DECLARE
    rsuper boolean;
    rbypass boolean;
    can_select boolean;
BEGIN
    SELECT rolsuper, rolbypassrls INTO rsuper, rbypass
      FROM pg_roles WHERE rolname = 'grafana_hatchet_readonly';
    SELECT has_table_privilege('grafana_hatchet_readonly', 'v1_runs_olap', 'SELECT')
      INTO can_select;
    RAISE NOTICE 'grafana_hatchet_readonly: super=%, bypassrls=%, v1_runs_olap_SELECT=%',
                 rsuper, rbypass, can_select;
    IF rsuper OR rbypass THEN
        RAISE EXCEPTION 'grafana_hatchet_readonly misconfigured';
    END IF;
    IF NOT can_select THEN
        RAISE EXCEPTION 'grafana_hatchet_readonly missing SELECT on v1_runs_olap';
    END IF;
END $$;

-- Restore connection to the application DB so any downstream migrations in
-- a concatenated rollup run against the correct logical database. This file
-- earlier `\connect hatchet`s to grant on the hatchet DB; without restoring
-- here, the Phase 0-4 rollup leaves later statements pointed at `hatchet`.
\connect georag
