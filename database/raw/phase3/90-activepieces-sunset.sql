-- =============================================================================
-- Phase 3 Step 7 — Activepieces sunset.
--
-- Removes the Activepieces logical DB + role from the Postgres cluster
-- after the migration to Kestra is fully cut over (Steps 4 + 5 verified
-- end-to-end on Kestra; both flows running with per-flow JWT + HMAC).
--
-- Pre-flight (operator before applying this migration):
--   1. The activepieces docker container must be stopped:
--        docker compose --profile dev-data stop activepieces
--   2. A final logical pg_dump of the activepieces DB must exist in the
--      backup bucket (90-day retention). The cluster-level pg_basebackup
--      keeps a copy for at least the standard backup window even after
--      the DB is dropped.
--   3. /admin/integrations dashboard's "Activepieces flows" section
--      shows zero rows (no in-flight executions).
--
-- After this migration:
--   - The activepieces logical DB is dropped (DROP DATABASE … WITH FORCE
--     to terminate any lingering connections — safe because the
--     container is stopped).
--   - The activepieces role is dropped (no objects own it post-DROP).
--   - All `activepieces.*.enabled` feature flags are removed.
--   - feature_flag_history rows for the dropped flags are PRESERVED
--     (audit trail integrity).
--
-- Apply via psql against any DB on the cluster (the DROP commands run
-- outside any transaction). NOT idempotent on the DROP DATABASE step —
-- safe to re-run only after the DB is already gone.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- 1. Drop the activepieces.* feature flags. The history sidecar (R-P1-6)
--    captures the DELETE op so the timeline survives the cleanup.
-- ---------------------------------------------------------------------------
DELETE FROM workspace.feature_flags
 WHERE flag_name LIKE 'activepieces.%.enabled';

-- ---------------------------------------------------------------------------
-- 2. Drop the activepieces logical DB.
--    The container is expected to be stopped already; FORCE handles any
--    leftover idle connections (e.g. monitoring tools).
-- ---------------------------------------------------------------------------
SELECT 'DROP DATABASE activepieces WITH (FORCE)'
WHERE EXISTS (SELECT 1 FROM pg_database WHERE datname = 'activepieces')
\gexec

-- ---------------------------------------------------------------------------
-- 3. Drop the role. Only safe AFTER the DB is gone (the role owns the DB).
-- ---------------------------------------------------------------------------
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'activepieces') THEN
        DROP ROLE activepieces;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 4. Verification.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    db_exists boolean;
    role_exists boolean;
    flag_count int;
BEGIN
    SELECT EXISTS(SELECT 1 FROM pg_database WHERE datname = 'activepieces')
        INTO db_exists;
    SELECT EXISTS(SELECT 1 FROM pg_roles    WHERE rolname = 'activepieces')
        INTO role_exists;
    SELECT count(*) INTO flag_count
        FROM workspace.feature_flags
       WHERE flag_name LIKE 'activepieces.%.enabled';

    RAISE NOTICE 'sunset: db_exists=%, role_exists=%, lingering flags=%',
                 db_exists, role_exists, flag_count;

    IF db_exists OR role_exists OR flag_count > 0 THEN
        RAISE EXCEPTION 'Activepieces sunset incomplete: db=%, role=%, flags=%',
                        db_exists, role_exists, flag_count;
    END IF;
END $$;
