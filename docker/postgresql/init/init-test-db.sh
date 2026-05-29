#!/bin/bash
# ============================================================================
# GeoRAG — provision the `georag_test` database on first PG boot.
#
# The postgres entrypoint runs every executable in /docker-entrypoint-initdb.d/
# in filename order. This script runs AFTER init-postgis.sql (alphabetical:
# init-postgis.sql → init-test-db.sh) so the main `georag` DB has already
# been fully provisioned.
#
# This script is idempotent: skipping DB creation if it already exists, and
# using IF NOT EXISTS for extensions and schemas so a re-run is safe.
#
# ⚠️  Note: init scripts only run on FIRST boot against a fresh volume. If
# you've already initialised the PG volume, apply the same steps manually:
#
#   docker exec georag-postgresql psql -U georag -d georag \\
#     -c "CREATE DATABASE georag_test OWNER georag;"
#   docker exec georag-postgresql psql -U georag -d georag_test \\
#     -c "CREATE EXTENSION postgis; ... (see below)"
#
# See docs/RUNBOOK.md → "Test environment gotchas" for when/why this DB is
# used (phpunit.pgsql.xml against the Collar + PG-migration feature tests).
# ============================================================================

set -euo pipefail

echo "[init-test-db] creating georag_test database (idempotent)"

psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" <<-'EOSQL'
    SELECT 'CREATE DATABASE georag_test OWNER georag'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'georag_test')\gexec
EOSQL

echo "[init-test-db] installing extensions + schemas in georag_test"

psql -v ON_ERROR_STOP=1 -U "${POSTGRES_USER}" -d georag_test <<-'EOSQL'
    -- Extensions (mirrors init-postgis.sql for the main georag DB).
    -- pg_stat_statements is cluster-wide via shared_preload_libraries and
    -- doesn't need a per-database CREATE EXTENSION.
    CREATE EXTENSION IF NOT EXISTS postgis;
    CREATE EXTENSION IF NOT EXISTS postgis_topology;
    CREATE EXTENSION IF NOT EXISTS pg_trgm;
    CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

    -- Medallion schemas (mirrors init-postgis.sql). Application migrations
    -- create additional schemas inside this DB when run with RefreshDatabase.
    CREATE SCHEMA IF NOT EXISTS bronze;
    CREATE SCHEMA IF NOT EXISTS silver;
    CREATE SCHEMA IF NOT EXISTS gold;
    CREATE SCHEMA IF NOT EXISTS index;

    -- Default search path matches the main DB so migrations resolve the
    -- silver schema first without each Laravel migration needing to qualify.
    ALTER DATABASE georag_test SET search_path TO silver, bronze, gold, index, public;
EOSQL

echo "[init-test-db] done"
