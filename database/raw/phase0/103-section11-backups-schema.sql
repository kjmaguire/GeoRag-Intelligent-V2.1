-- =============================================================================
-- §11.1 — backups.snapshot_runs registry for per-store backup cron output.
--
-- Every Hatchet backup_* workflow run writes one row here:
--   - the store identifier (postgres | neo4j | qdrant | redis | seaweedfs)
--   - run timestamps
--   - the snapshot bucket + object key
--   - the snapshot sha256 (post-write verification)
--   - the byte count (matches the manifest's claimed size)
--   - status: running | completed | failed
--   - failure_reason when status='failed'
--
-- Kept out of the silver schema because backups are platform-level (no
-- workspace scoping). Cross-tenant readable only via the admin Gate.
--
-- Idempotent. Owned by georag; readable by georag_app for the
-- admin endpoints that surface backup status.
-- =============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS backups;

CREATE TABLE IF NOT EXISTS backups.snapshot_runs (
    run_id           uuid       PRIMARY KEY DEFAULT gen_random_uuid(),
    store            text       NOT NULL
        CHECK (store IN ('postgres', 'neo4j', 'qdrant', 'redis', 'seaweedfs')),
    started_at       timestamptz NOT NULL DEFAULT now(),
    completed_at     timestamptz,
    bucket           text,
    object_key       text,
    sha256_hex       text,
    bytes            bigint,
    status           text       NOT NULL DEFAULT 'running'
        CHECK (status IN ('running', 'completed', 'failed')),
    failure_reason   text,
    payload          jsonb      NOT NULL DEFAULT '{}'::jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- Ordered listing of recent runs per store (admin endpoint pattern).
CREATE INDEX IF NOT EXISTS idx_snapshot_runs_store_started
    ON backups.snapshot_runs (store, started_at DESC);

-- Quick lookup of in-flight runs (any store) for the operator dashboard.
CREATE INDEX IF NOT EXISTS idx_snapshot_runs_running
    ON backups.snapshot_runs (started_at DESC)
    WHERE status = 'running';

COMMENT ON TABLE backups.snapshot_runs IS
    '§11.1 — per-store backup snapshot run registry. One row per cron '
    'invocation. cross-tenant; admin-gated.';

-- Grants — the admin endpoints read via georag_app; cron writes via
-- the direct georag connection (bypassing pgbouncer).
GRANT USAGE ON SCHEMA backups TO georag_app;
GRANT SELECT ON backups.snapshot_runs TO georag_app;
GRANT INSERT, UPDATE ON backups.snapshot_runs TO georag_app;

COMMIT;
