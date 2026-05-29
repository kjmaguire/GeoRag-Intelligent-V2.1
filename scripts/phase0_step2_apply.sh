#!/usr/bin/env bash
# =============================================================================
# scripts/phase0_step2_apply.sh
#
# Applies the Phase 0 Step 2 schema in dependency order.
#   10  Layer A delta (workspace_memberships, workspace_roles)
#   20  Layer B audit_ledger + verification_runs (partman-partitioned)
#   30  Layer C workflow_runs + workflow_run_events
#   40  Layer D outbox.pending_propagations + propagation_attempts
#   50  Layer E operational contract (6 tables)
#   60  Layer F usage events + aggregates + ceilings
#   70  Layer G silver findings (3 tables)
#   80  Layer H integration_credentials_audit
#   90  audit hash-chain trigger + genesis row
#   95  RLS policies on workspace-scoped tables
#
# Idempotent. Safe to re-run.
# =============================================================================

set -euo pipefail

CONTAINER="${POSTGRES_CONTAINER:-georag-postgresql}"
DB="${POSTGRES_DB:-georag}"
USER="${POSTGRES_USER:-georag}"
SQL_DIR="${SQL_DIR:-/home/georag/projects/georag/database/raw/phase0}"

if [ ! -d "$SQL_DIR" ]; then
    echo "ERROR: $SQL_DIR not found. Set SQL_DIR or run from repo root." >&2
    exit 1
fi

cd "$SQL_DIR"
files=( $(ls *.sql | sort) )

echo "==> Applying Phase 0 Step 2 schema (${#files[@]} files) to ${CONTAINER}/${DB}"

for f in "${files[@]}"; do
    echo
    echo "--- [$f] ---"
    docker exec -i "$CONTAINER" psql -U "$USER" -d "$DB" -v ON_ERROR_STOP=1 < "$f" \
        | tail -20
done

echo
echo "==> All Step 2 SQL applied."
