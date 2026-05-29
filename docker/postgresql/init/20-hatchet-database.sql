-- =============================================================================
-- Hatchet engine — Postgres role + database
--
-- Hatchet (the orchestrator we use for durable multi-step workflows per
-- master plan §6) needs its own role + database for both schema durability
-- and message-queue durability (we run with SERVER_MSGQUEUE_KIND=postgres,
-- so RabbitMQ is not deployed). This script lives in
-- /docker-entrypoint-initdb.d/ for fresh installs and is also applied by
-- scripts/phase0_apply_extensions.sh against existing dev databases.
--
-- Idempotent. Password is a reasonable default; override via the
-- HATCHET_DB_PASSWORD env var consumed by the hatchet-lite compose service.
-- =============================================================================

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hatchet') THEN
    CREATE ROLE hatchet LOGIN PASSWORD 'hatchet';
  END IF;
END $$;

SELECT 'CREATE DATABASE hatchet OWNER hatchet'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'hatchet')\gexec

GRANT ALL PRIVILEGES ON DATABASE hatchet TO hatchet;
