-- =============================================================================
-- Phase 1 Step 1 — split the application Postgres role (R-P0-10 fix).
--
-- Phase 0 finding: the `georag` role used by every app service is a
-- SUPERUSER with rolbypassrls=true, so RLS policies on Phase 0 tables are
-- silently ineffective. This migration introduces `georag_app` — a
-- non-superuser role that the application services use, with the grants
-- they need but no BYPASSRLS.
--
-- After this migration:
--   - Applications (Laravel, FastAPI, Hatchet workers) connect as `georag_app`
--   - `georag` is reserved for migrations + admin maintenance only
--   - RLS policies actually apply to live application traffic
--
-- Idempotent. Safe to re-run.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- Role
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'georag_app') THEN
        CREATE ROLE georag_app
            LOGIN
            NOSUPERUSER
            NOBYPASSRLS
            NOCREATEDB
            NOCREATEROLE
            INHERIT
            PASSWORD 'georag-app-dev-replace-via-alter-role';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- Schema USAGE (every Phase 0 namespace + the existing legacy ones)
-- ---------------------------------------------------------------------------
GRANT USAGE ON SCHEMA
    public, bronze, silver, gold, "index", topology, public_geo,
    audit, usage, outbox, workflow, workspace, partman
    TO georag_app;

-- ---------------------------------------------------------------------------
-- Table privileges
-- Read+write everywhere the app touches; explicit DELETE only on the
-- transient tables (idempotency_keys + dry_run_outputs); RLS does the
-- workspace clamping. partman.* is read-only for georag_app — the
-- partition maintenance worker still runs as superuser.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA
    public, bronze, silver, gold, "index", topology, public_geo,
    audit, usage, outbox, workflow, workspace
    TO georag_app;

-- DELETE for the transient cache-style tables only.
GRANT DELETE ON workspace.idempotency_keys, workspace.dry_run_outputs TO georag_app;

-- pg_partman read-only (so the app can SELECT from part_config etc.)
GRANT SELECT ON ALL TABLES IN SCHEMA partman TO georag_app;

-- Sequences (auto-increment columns)
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA
    public, silver, audit, usage, outbox, workflow, workspace
    TO georag_app;

-- Functions the app calls (audit emit chain, hash recompute, run_verification)
GRANT EXECUTE ON FUNCTION audit.compute_audit_hash() TO georag_app;
GRANT EXECUTE ON FUNCTION audit.recompute_hash(bytea, bigint, text, text, text, text, text, jsonb, timestamptz) TO georag_app;
GRANT EXECUTE ON FUNCTION audit.verify_hash_chain(timestamptz, timestamptz) TO georag_app;
GRANT EXECUTE ON FUNCTION audit.run_verification(timestamptz, timestamptz, uuid) TO georag_app;

-- ---------------------------------------------------------------------------
-- DEFAULT PRIVILEGES — anything `georag` (the superuser running migrations)
-- creates from now on automatically grants to georag_app too. Without this,
-- every new table in a future migration would need a manual grant.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    s text;
BEGIN
    FOREACH s IN ARRAY ARRAY[
        'public','bronze','silver','gold','public_geo',
        'audit','usage','outbox','workflow','workspace'
    ] LOOP
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE georag IN SCHEMA %I '
            'GRANT SELECT, INSERT, UPDATE ON TABLES TO georag_app', s);
        EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE georag IN SCHEMA %I '
            'GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO georag_app', s);
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- Verification — emit a notice with the role's posture so the apply log
-- makes the new state easy to scan for.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    rsuper boolean; rbypass boolean; rlogin boolean;
BEGIN
    SELECT rolsuper, rolbypassrls, rolcanlogin
      INTO rsuper, rbypass, rlogin
      FROM pg_roles WHERE rolname = 'georag_app';
    RAISE NOTICE 'georag_app: super=%, bypassrls=%, login=%', rsuper, rbypass, rlogin;
    IF rsuper OR rbypass THEN
        RAISE EXCEPTION 'georag_app misconfigured: must NOT be SUPERUSER or BYPASSRLS';
    END IF;
END $$;
