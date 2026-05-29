-- =============================================================================
-- GeoRAG — Activate WAL Archiving for Point-in-Time Recovery
-- =============================================================================
-- Runs after init-postgis.sql + init-test-db.sh (Z_ prefix sorts alphabetical-
-- last in /docker-entrypoint-initdb.d/).
--
-- Architecture (matches docker/postgresql/wal-upload.sh):
--   1. PostgreSQL fills WAL segments and runs archive_command on each
--      completed segment.
--   2. archive_command copies the segment to /pg_wal_archive/ — a volume
--      shared between this container and georag-backup-agent.
--   3. Every 5 minutes, Ofelia invokes wal-upload.sh in georag-backup-agent
--      which `aws s3 sync`s /pg_wal_archive/ → s3://georag-backups/pg-wal/
--      and removes confirmed-uploaded segments from the local volume.
--
-- The archive_command pattern is the canonical idempotent local-copy form
-- documented in the PostgreSQL manual: test first to avoid overwriting on
-- retry, copy second.
--
-- Why ALTER SYSTEM (and not -c flags in compose):
--   ALTER SYSTEM writes to PGDATA/postgresql.auto.conf. This file survives
--   container recreation as long as the data volume persists, which means
--   activation is durable across `docker compose down/up`. Conversely,
--   -c flags would require modifying compose every time, and would have to
--   duplicate the existing 47 -c args.
--
-- Effect timing:
--   * archive_command — reloadable via SIGHUP (pg_reload_conf below)
--   * archive_timeout — reloadable
--   * archive_mode    — POSTMASTER-LEVEL: requires a server restart
--
-- On a FRESH-INIT volume this script runs during initial init; PostgreSQL
-- restarts cleanly via the docker entrypoint after init scripts finish, so
-- archive_mode is active by the time the container reports healthy.
--
-- On an EXISTING volume, /docker-entrypoint-initdb.d/ scripts are skipped
-- by the postgres entrypoint (PG_VERSION is already present in PGDATA).
-- Activate manually:
--
--   docker exec georag-postgresql psql -U georag -d georag -f \
--     /docker-entrypoint-initdb.d/Z_activate_wal_archiving.sql
--   docker compose restart postgresql   # required for archive_mode
--
-- =============================================================================

-- Activate the archiver process. Requires a postmaster restart.
ALTER SYSTEM SET archive_mode = 'on';

-- Idempotent local-copy archive_command. Writes to the shared
-- /pg_wal_archive volume. The `test ! -f` short-circuits if a previous
-- attempt already copied the segment — important for retry safety, since
-- archive_command non-zero exit causes WAL retention until success.
--
-- Single-quoting is delicate inside ALTER SYSTEM: the entire value is
-- single-quoted by SQL, and inner single quotes are escaped by doubling.
ALTER SYSTEM SET archive_command =
    'test ! -f /pg_wal_archive/%f && cp %p /pg_wal_archive/%f';

-- Force WAL switch every 60s if there's no write activity. Caps the data-
-- loss window for an idle database (without this, a quiet database can sit
-- on an unflushed WAL segment indefinitely; combined with 5-min Ofelia
-- sync, worst-case RPO becomes 60s + sync interval ≈ 6 min).
ALTER SYSTEM SET archive_timeout = '60';

-- Reload now so archive_command + archive_timeout take effect immediately.
-- archive_mode still requires the next postmaster start; the docker
-- entrypoint will provide that on fresh-init clusters.
SELECT pg_reload_conf();

-- Verification — emit current settings to log so the init output records
-- the activation. Operators reading the docker logs can confirm the
-- settings landed.
DO $$
DECLARE
    v_mode    TEXT;
    v_cmd     TEXT;
    v_timeout TEXT;
BEGIN
    SELECT setting INTO v_mode    FROM pg_settings WHERE name = 'archive_mode';
    SELECT setting INTO v_cmd     FROM pg_settings WHERE name = 'archive_command';
    SELECT setting INTO v_timeout FROM pg_settings WHERE name = 'archive_timeout';
    RAISE NOTICE 'WAL archiving activated:';
    RAISE NOTICE '  archive_mode    = %', v_mode;
    RAISE NOTICE '  archive_command = %', v_cmd;
    RAISE NOTICE '  archive_timeout = %', v_timeout;
    RAISE NOTICE 'archive_mode requires postmaster restart on existing volumes.';
END
$$;
