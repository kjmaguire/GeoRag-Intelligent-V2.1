-- =============================================================================
-- Kestra sunset — retroactive (2026-07-28), retained as the cleanup path.
--
-- Mirrors 90-activepieces-sunset.sql. Kestra never had flows deployed
-- (flow-source-loader.enabled: false, no CI push step) and KESTRA_URL was
-- set in no environment, so the escalation paths that were supposed to
-- reach it were silently no-ops for months. It was removed wholesale
-- along with its Caddy edge proxy, the Laravel SSO controllers and the
-- compose service block.
--
-- Unlike the Activepieces sunset there are no `kestra.*.enabled` feature
-- flags to drop: the flag namespace had already moved to the neutral
-- `flows.*` before Kestra ever went live, so nothing was gated under a
-- kestra-specific name.
--
-- WHY THIS FILE STILL EXISTS AFTER THE CREATOR IS GONE
--
-- `phase3/10-kestra-role-and-db.sql` created a LOGIN role with a hardcoded
-- password plus a logical database. It was deleted during the AWS
-- migration (ADR-0022) rather than left in the tree for an operator to
-- apply by hand: a fresh RDS instance must never grow a login role for a
-- service that does not exist. Deleting the creator does not clean a
-- cluster where it was already applied — a dev laptop, or any long-lived
-- Postgres from the Phase 3 era — and this file is what does that. It is
-- a no-op on a cluster that never had it, which is every AWS cluster.
--
-- Pre-flight (operator, on a cluster that did have it):
--   1. The kestra container must already be stopped/removed.
--   2. If any Kestra executions matter for audit, take a final logical
--      pg_dump of the kestra DB first. Given it was never live, this is
--      almost certainly an empty schema.
--
-- Apply via psql against any DB on the cluster (the DROP commands run
-- outside any transaction, and `\gexec` is not something `db:apply-raw`
-- can run — which is why this file is not in database/raw/manifest.json).
-- The verification block at the end RAISES unless both the role and the
-- database are gone, so a partial run is reported, not assumed.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Drop the kestra logical DB.
-- ---------------------------------------------------------------------------
SELECT 'DROP DATABASE kestra WITH (FORCE)'
WHERE EXISTS (SELECT 1 FROM pg_database WHERE datname = 'kestra')
\gexec

-- ---------------------------------------------------------------------------
-- 2. Drop the role. Only safe AFTER the DB is gone (the role owns the DB).
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'kestra') THEN
        DROP ROLE kestra;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 3. Verification.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    db_exists boolean;
    role_exists boolean;
BEGIN
    SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = 'kestra')
        INTO db_exists;
    SELECT EXISTS(SELECT 1 FROM pg_roles    WHERE rolname = 'kestra')
        INTO role_exists;

    RAISE NOTICE 'kestra sunset: db_exists=%, role_exists=%',
                 db_exists, role_exists;

    IF db_exists OR role_exists THEN
        RAISE EXCEPTION 'Kestra sunset incomplete: db=%, role=%',
                        db_exists, role_exists;
    END IF;
END $$;
