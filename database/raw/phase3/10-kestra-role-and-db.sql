-- =============================================================================
-- Phase 3 Step 1 — `kestra` Postgres role + logical database.
--
-- Mirrors the Phase 0 `hatchet`, Phase 1 `georag_app`, and Phase 2
-- `activepieces` patterns:
--
--   - Dedicated role for the Kestra service. Same Postgres server,
--     separate logical DB, separate role.
--   - NOSUPERUSER + NOBYPASSRLS — Kestra has no business reading rows
--     in the `georag` DB; cross-DB access is structurally blocked at
--     the connection layer + schema USAGE gate.
--   - Role owns its own DB outright.
--
-- Idempotent. Apply via psql (the `\gexec` guard for CREATE DATABASE
-- requires psql, not a generic SQL runner).
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Role
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kestra') THEN
        CREATE ROLE kestra
            LOGIN
            NOSUPERUSER
            NOBYPASSRLS
            NOCREATEDB
            NOCREATEROLE
            INHERIT
            PASSWORD 'kestra-dev-replace-via-alter-role';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. Database — guarded \gexec (CREATE DATABASE can't run inside a
-- transaction or function).
-- ---------------------------------------------------------------------------
SELECT 'CREATE DATABASE kestra OWNER kestra'
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'kestra')
\gexec

GRANT CONNECT ON DATABASE kestra TO kestra;

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
      FROM pg_roles WHERE rolname = 'kestra';

    SELECT pg_catalog.pg_get_userbyid(datdba)
      INTO rdb_owner
      FROM pg_database WHERE datname = 'kestra';

    RAISE NOTICE 'kestra role: super=%, bypassrls=%, login=%; db owner=%',
                 rsuper, rbypass, rlogin, rdb_owner;

    IF rsuper OR rbypass THEN
        RAISE EXCEPTION 'kestra role misconfigured: must NOT be SUPERUSER or BYPASSRLS';
    END IF;
    IF rdb_owner IS DISTINCT FROM 'kestra' THEN
        RAISE EXCEPTION 'kestra DB owner mismatch: got %, expected kestra', rdb_owner;
    END IF;
END $$;
