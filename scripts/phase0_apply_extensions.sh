#!/usr/bin/env bash
# =============================================================================
# scripts/phase0_apply_extensions.sh
#
# One-shot: applies Phase 0 Step 1 extensions and Step 2 schema namespaces
# to the *existing* dev database. The init script
# (docker/postgresql/init/10-phase0-extensions-and-schemas.sql) only fires on
# first DB init; for a database that already exists we have to apply the same
# DDL via psql against the running container.
#
# Run from inside WSL after `docker compose build postgresql` and
# `docker compose up -d --force-recreate postgresql` have completed.
#
# Idempotent — safe to re-run.
# =============================================================================

set -euo pipefail

CONTAINER="${POSTGRES_CONTAINER:-georag-postgresql}"
DB="${POSTGRES_DB:-georag}"
USER="${POSTGRES_USER:-georag}"

echo "==> Applying Phase 0 extensions + schemas to ${CONTAINER} (db=${DB})"

# Sanity: container reachable
docker exec "${CONTAINER}" pg_isready -U "${USER}" -d "${DB}" >/dev/null

# Apply the same SQL the init script would run. We run it as the superuser
# (postgres) inside the container so CREATE EXTENSION succeeds for those
# that require superuser (auto_explain, pg_repack, etc.).
docker exec -i "${CONTAINER}" psql -U postgres -d "${DB}" -v ON_ERROR_STOP=1 \
    < "$(dirname "$0")/../docker/postgresql/init/10-phase0-extensions-and-schemas.sql"

echo
echo "==> Verifying — kickoff Step 1 done definition"
docker exec "${CONTAINER}" psql -U "${USER}" -d "${DB}" -c "
  SELECT extname, extversion FROM pg_extension WHERE extname IN (
    'postgis', 'pg_trgm', 'pg_stat_statements', 'auto_explain', 'h3',
    'hypopg', 'pg_stat_kcache', 'pg_partman', 'pg_repack', 'pg_ivm'
  ) ORDER BY extname;
"

echo
echo "==> Verifying — kickoff Step 1 schema namespaces"
docker exec "${CONTAINER}" psql -U "${USER}" -d "${DB}" -c "
  SELECT nspname FROM pg_namespace
  WHERE nspname IN ('audit','usage','silver','gold','public_geoscience','outbox','workflow','workspace')
  ORDER BY nspname;
"

echo
echo "==> Phase 0 extensions + schemas applied."
