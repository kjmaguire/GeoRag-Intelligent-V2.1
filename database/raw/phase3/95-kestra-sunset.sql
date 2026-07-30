-- =============================================================================
-- Phase 3 Step 8 (retroactive, 2026-07-28) — Kestra sunset.
--
-- Mirrors 90-activepieces-sunset.sql. Kestra never had flows deployed
-- (flow-source-loader.enabled: false, no CI push step) and KESTRA_URL was
-- unset in every environment, so escalation paths that POSTed to it
-- (src/fastapi/app/agents/phase0/{tenant_isolation_auditor,support_packet}.py,
-- app/services/dispatchers/kestra.py) were silently no-ops for months. The
-- compose service (and Caddy, which existed only to front it) were removed
-- in A7, 2026-07-28.
--
-- Unlike the Activepieces sunset, there are no `kestra.*.enabled` feature
-- flags to drop: the Phase 3 Step 3 flag rename (20-rename-flow-flags.sql)
-- moved flow gating to the orchestrator-neutral `flows.<flow>.enabled`
-- namespace before Kestra ever went live, so nothing was ever gated under a
-- kestra-specific flag name.
--
-- Pre-flight (operator before applying this migration):
--   1. The kestra docker container must already be stopped/removed — it no
--      longer has a compose service definition as of A7.
--   2. If any Kestra executions matter for audit purposes, take a final
--      logical pg_dump of the kestra DB before dropping it. Given it was
--      never in production use, this is expected to be a formality.
--
-- After this migration:
--   - The kestra logical DB is dropped (DROP DATABASE … WITH FORCE).
--   - The kestra role is dropped.
--
-- Apply via psql against any DB on the cluster (the DROP commands run
-- outside any transaction). NOT idempotent on the DROP DATABASE step —
-- safe to re-run only after the DB is already gone.
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
