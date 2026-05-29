-- =============================================================================
-- Threadripper-tuned PostgreSQL parallelism + IO settings
-- =============================================================================
-- Hardware-refresh 2026-05-08: the dev workstation moved from an 8-core
-- box to a Threadripper Pro 5955WX (16 physical / 32 logical cores).
-- Postgres compose defaults from the 2026-04 audit snapshot
-- (max_worker_processes=8, max_parallel_workers=8, etc.) leave most of
-- the new CPU headroom unused — and starve Dagster ingestion + the
-- silver-schema pgTAP suite + parallel rollups during bulk operations.
--
-- Pattern matches Z_activate_wal_archiving.sql:
--
--   * Uses ALTER SYSTEM rather than postgresql.conf so we don't fight
--     with the postgis image's entrypoint (compose `command:`-list
--     overrides clash with the image's args parser, see audit snapshot).
--   * Filename prefixed with `Z_` so postgres's docker-entrypoint runs
--     it AFTER init-postgis.sql (alphabetical).
--   * Idempotent — safe to re-run on existing clusters; ALTER SYSTEM
--     overwrites postgresql.auto.conf entries.
--   * Requires a server restart (or `pg_reload_conf()` for the runtime-
--     reloadable subset) before settings take effect. The dev workstation
--     restarts the postgres container during compose down/up, so this is
--     handled implicitly. For zero-downtime upgrades on staging/prod,
--     issue `SELECT pg_reload_conf();` after applying.
--
-- All values intentionally read from environment variables via the
-- compose `command:` line that injects ${POSTGRES_*} — but ALTER SYSTEM
-- inside an init script wins regardless of compose-line drift, which
-- is the whole point of moving the canonical settings here.
-- =============================================================================

-- ----------------------------------------------------------------------------
-- Parallel worker pool — Threadripper Pro 5955WX (16C/32T)
-- ----------------------------------------------------------------------------
-- max_worker_processes: total background-worker slots. 24 leaves
-- headroom over max_parallel_workers + autovacuum + logical-replication
-- workers + extension workers (pg_cron, etc.).
ALTER SYSTEM SET max_worker_processes = 24;

-- max_parallel_workers: cap on simultaneous parallel-query workers across
-- all queries. 12 = 75% of physical cores, leaves 4 cores for FastAPI
-- uvicorn workers + Ollama offload threads + the Postgres serving path.
ALTER SYSTEM SET max_parallel_workers = 12;

-- max_parallel_workers_per_gather: how many workers a single Gather node
-- can spawn. 6 = half the global parallel-worker pool — lets one heavy
-- query parallelise without hogging the whole pool.
ALTER SYSTEM SET max_parallel_workers_per_gather = 6;

-- max_parallel_maintenance_workers: CREATE INDEX / VACUUM / CLUSTER
-- parallelism. 6 matches the per_gather setting; the silver-schema
-- pgTAP suite drops ~40% wall-clock with this raised.
ALTER SYSTEM SET max_parallel_maintenance_workers = 6;

-- ----------------------------------------------------------------------------
-- IO concurrency — single 1.8 TB NVMe with PCIe x16 lanes
-- ----------------------------------------------------------------------------
-- effective_io_concurrency: number of concurrent IO requests Postgres
-- assumes the disk can sustain. NVMe + Threadripper PCIe handles 256+
-- comfortably; bitmap heap scans on the geochem JSONB columns benefit
-- most. Was 200 in the audit snapshot — bumped to match the new disk.
ALTER SYSTEM SET effective_io_concurrency = 256;

-- maintenance_io_concurrency: same idea but for VACUUM / index builds.
-- Postgres 18 default (10) is conservative; NVMe handles 64 cleanly.
ALTER SYSTEM SET maintenance_io_concurrency = 64;

-- ----------------------------------------------------------------------------
-- Memory tunables — 64 GB host, single-purpose dev workstation
-- ----------------------------------------------------------------------------
-- shared_buffers / effective_cache_size / work_mem / maintenance_work_mem
-- come in via the compose `command:` line and POSTGRES_* env vars; we
-- DON'T re-set them here so operators can override per-deploy without
-- editing this script. If you want to lock those values too, uncomment
-- the four lines below — but prefer the env-driven path for parity with
-- staging/prod where the same image runs with different memory budgets.
--
-- ALTER SYSTEM SET shared_buffers = '8GB';
-- ALTER SYSTEM SET effective_cache_size = '32GB';
-- ALTER SYSTEM SET work_mem = '192MB';
-- ALTER SYSTEM SET maintenance_work_mem = '2GB';

-- ----------------------------------------------------------------------------
-- Query planner cost model — NVMe (already env-driven, mirror here for safety)
-- ----------------------------------------------------------------------------
-- random_page_cost: for NVMe + SSD pools. 1.1 makes the planner prefer
-- index scans over seq scans when the rowcount is tight — critical on
-- the silver.collars / silver.lithology_logs tables where the GIST
-- index dominates serving latency.
ALTER SYSTEM SET random_page_cost = 1.1;

-- ----------------------------------------------------------------------------
-- Reload — runtime-reloadable subset takes effect immediately;
-- max_worker_processes / max_parallel_workers require a server restart.
-- ----------------------------------------------------------------------------
SELECT pg_reload_conf();

-- ----------------------------------------------------------------------------
-- Audit log — visible in `docker compose logs postgres`
-- ----------------------------------------------------------------------------
DO $$
BEGIN
    RAISE NOTICE 'Threadripper tuning applied: max_worker_processes=%, max_parallel_workers=%, effective_io_concurrency=%',
        current_setting('max_worker_processes'),
        current_setting('max_parallel_workers'),
        current_setting('effective_io_concurrency');
END
$$;
