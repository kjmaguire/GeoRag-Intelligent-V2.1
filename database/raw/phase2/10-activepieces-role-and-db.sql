-- =============================================================================
-- Phase 2 Step 1 — `activepieces` Postgres role + logical database.
--
-- Mirrors the Phase 0 `hatchet` and Phase 1 `georag_app` patterns:
--
--   - Dedicated role for the Activepieces service. Same Postgres server,
--     separate logical DB, separate role.
--   - NOSUPERUSER + NOBYPASSRLS — the Activepieces service has no business
--     reading rows in the `georag` DB; cross-DB access is structurally
--     blocked by Postgres at the connection layer.
--   - The role owns its own DB outright; we don't share a schema with
--     `georag` or `hatchet`.
--
-- Idempotent. Safe to re-run.
--
-- Apply with the maintenance role (the `georag` superuser-equivalent that
-- ran Phase 0 + Phase 1 migrations). The dev-stack default password
-- below is replaced via ALTER ROLE in the .env-driven init flow.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Role
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'activepieces') THEN
        CREATE ROLE activepieces
            LOGIN
            NOSUPERUSER
            NOBYPASSRLS
            NOCREATEDB
            NOCREATEROLE
            INHERIT
            PASSWORD 'activepieces-dev-replace-via-alter-role';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Database
--
-- CREATE DATABASE cannot run inside a transaction block (or a plpgsql
-- function). The portable idempotent idiom is psql's `\gexec` — emit the
-- CREATE statement only when the database is missing, then execute the
-- emitted SQL. The migration must be applied via psql, not via a generic
-- SQL runner.
-- ---------------------------------------------------------------------------
SELECT 'CREATE DATABASE activepieces OWNER activepieces'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'activepieces')
\gexec

-- Defensive: even if the DB existed before this migration, ensure the
-- `activepieces` role can connect to it.
GRANT CONNECT ON DATABASE activepieces TO activepieces;

-- ---------------------------------------------------------------------------
-- 3. Verification
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    rsuper boolean;
    rbypass boolean;
    rlogin boolean;
    rdb_owner text;
BEGIN
    SELECT rolsuper, rolbypassrls, rolcanlogin
      INTO rsuper, rbypass, rlogin
      FROM pg_roles WHERE rolname = 'activepieces';

    SELECT pg_catalog.pg_get_userbyid(datdba)
      INTO rdb_owner
      FROM pg_database WHERE datname = 'activepieces';

    RAISE NOTICE 'activepieces role: super=%, bypassrls=%, login=%; db owner=%',
                 rsuper, rbypass, rlogin, rdb_owner;

    IF rsuper OR rbypass THEN
        RAISE EXCEPTION 'activepieces role misconfigured: must NOT be SUPERUSER or BYPASSRLS';
    END IF;
    IF rdb_owner IS DISTINCT FROM 'activepieces' THEN
        RAISE EXCEPTION 'activepieces DB owner mismatch: got %, expected activepieces', rdb_owner;
    END IF;
END $$;
