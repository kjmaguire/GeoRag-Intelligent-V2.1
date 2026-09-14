-- =============================================================================
-- Activepieces sunset — Phase 3 Step 7, retained as the cleanup path.
--
-- Activepieces was the Phase 2 integration orchestrator. It was sunset
-- wholesale at Phase 3 Step 7 and replaced by Kestra, which was itself
-- retired on 2026-07-28 without ever having been deployed. Neither exists
-- in this repository any more.
--
-- WHY THIS FILE STILL EXISTS AFTER THE CREATORS ARE GONE
--
-- `phase2/10-activepieces-role-and-db.sql` created a LOGIN role with a
-- hardcoded password plus a logical database, and `phase2/20-activepieces-
-- flow-flags.sql` seeded `activepieces.*.enabled` flags that no code has
-- read since the flag namespace moved to `flows.*`. Both were deleted
-- during the AWS migration (ADR-0022) rather than left in the tree for an
-- operator to apply by hand: a fresh RDS instance must never grow a login
-- role for a service that does not exist.
--
-- Deleting the creators does not clean a cluster where they were already
-- applied — a dev laptop, or any long-lived Postgres from the Phase 2 era.
-- This file is what does that, and it is the only remaining Activepieces
-- artifact in the repository. It is a no-op on a cluster that never had
-- them, which is every AWS cluster.
--
-- After this file:
--   - The activepieces logical DB is dropped (DROP DATABASE … WITH FORCE;
--     stop any container still holding a connection first).
--   - The activepieces role is dropped (no objects own it post-DROP).
--   - All `activepieces.*.enabled` feature flags are removed.
--   - feature_flag_history rows for the dropped flags are PRESERVED
--     (audit trail integrity).
--
-- Apply via psql against any DB on the cluster (the DROP commands run
-- outside any transaction, and `\gexec` is not something `db:apply-raw`
-- can run — which is why this file is not in database/raw/manifest.json).
-- The verification block at the end RAISES unless the role, the database
-- and the flags are all gone, so a partial run is reported, not assumed.
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
