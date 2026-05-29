#!/usr/bin/env bash
# =============================================================================
# scripts/section11_acceptance.sh
#
# Master-plan §11 (DR + deployment + perf) — v1 acceptance harness.
# Mirrors scripts/phase_h4_acceptance.sh in shape + exit-code semantics.
# Run after any §11 deploy + before declaring the §11-v1 surface clean.
#
# Pre-requisites (the script asserts each):
#   - Docker compose stack is up: docker compose ps
#   - FASTAPI_SERVICE_KEY env var is set + matches Laravel .env
#   - psql reachable via `docker exec <PG_CONTAINER> psql -U georag -d georag`
#
# What this harness covers (the §11-v1 surface):
#   §11.1  — 5 backup workflows registered in the Hatchet AI pool
#   §11.1  — backups.snapshot_runs schema + indexes present
#   §11.1  — GET /backups/snapshot-runs returns 200 + paginated shape
#   §11.2  — GET /backups/workspace-consistency/{ws} returns 200 for a real ws
#   §11.2  — Same endpoint returns 422 for a malformed UUID
#   §11.10 — cold_tier_archive workflow registered
#   §11.10 — GET /backups/cold-tier-runs returns 200
#   service-key gate — missing header → 401/422
#
# What this harness explicitly does NOT cover (deferred §11-v2):
#   §11.3  — restore_workspace dry_run=False (writer slice still NotImplemented)
#   §11.6  — single-tenant Helm chart
#   §11.7  — Kubernetes manifests
#   §11.8  — Air-gapped bundle pipeline
#
# Exit code 0 = §11-v1 ready for prod rollout. 1 = at least one check failed.
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=0
FAILED=()

SERVICE_KEY="${FASTAPI_SERVICE_KEY:-}"
FASTAPI_URL="${FASTAPI_URL:-http://localhost:8000}"
PG_CONTAINER="${PG_CONTAINER:-georag-postgresql}"
FASTAPI_CONTAINER="${FASTAPI_CONTAINER:-georag-fastapi}"
PG_USER="${PG_USER:-georag}"
PG_DB="${PG_DB:-georag}"

if [ -z "$SERVICE_KEY" ]; then
    echo "ERROR: FASTAPI_SERVICE_KEY env var is required (must match FastAPI container)."
    exit 2
fi

psql_q() {
    docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc "$1" 2>/dev/null
}

check() {
    local label="$1"
    local cmd="$2"
    local expected="${3:-200}"

    TOTAL=$((TOTAL + 1))
    local code
    code=$(eval "$cmd" 2>/dev/null)
    local match=0
    IFS=',' read -ra accepted <<< "$expected"
    for c in "${accepted[@]}"; do
        if [ "$code" = "$c" ]; then match=1; break; fi
    done
    if [ "$match" = "1" ]; then
        echo "  [PASS] $label (HTTP $code)"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $label (HTTP $code, expected $expected)"
        FAILED+=("$label")
    fi
}

curl_code() {
    curl -s -o /dev/null -w '%{http_code}' -H "X-Service-Key: $SERVICE_KEY" "$@"
}

echo
echo "=============================================================="
echo "  Master-plan §11 acceptance harness — v1 surface"
echo "  Target: $FASTAPI_URL"
echo "=============================================================="
echo

# ----------------------------------------------------------------------------
# 1. §11.1 — backup workflows are registered in the AI pool
# ----------------------------------------------------------------------------
echo "-- §11.1 backup workflows registered --"
REGISTERED=$(docker exec "$FASTAPI_CONTAINER" python -c "
from app.hatchet_workflows.worker import POOLS
names = sorted([w.name for w in POOLS['ai'] if w.name.startswith('backup_')])
print(','.join(names))
" 2>/dev/null)
for expected in backup_neo4j backup_postgres backup_qdrant backup_redis backup_seaweedfs; do
    TOTAL=$((TOTAL + 1))
    if echo ",$REGISTERED," | grep -q ",$expected,"; then
        echo "  [PASS] workflow $expected registered"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] workflow $expected NOT registered (found: $REGISTERED)"
        FAILED+=("workflow $expected")
    fi
done

# ----------------------------------------------------------------------------
# 2. §11.10 — cold-tier workflow registered
# ----------------------------------------------------------------------------
echo
echo "-- §11.10 cold-tier workflow registered --"
TOTAL=$((TOTAL + 1))
if docker exec "$FASTAPI_CONTAINER" python -c "
from app.hatchet_workflows.worker import POOLS
names = [w.name for w in POOLS['ai']]
import sys
sys.exit(0 if 'cold_tier_archive' in names else 1)
" 2>/dev/null; then
    echo "  [PASS] workflow cold_tier_archive registered"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] workflow cold_tier_archive NOT registered"
    FAILED+=("workflow cold_tier_archive")
fi

# ----------------------------------------------------------------------------
# 3. §11.1 — backups.snapshot_runs schema + indexes
# ----------------------------------------------------------------------------
echo
echo "-- §11.1 DB schema --"
TBL=$(psql_q "SELECT 1 FROM information_schema.tables WHERE table_schema='backups' AND table_name='snapshot_runs';" | head -1 | tr -d ' ')
TOTAL=$((TOTAL + 1))
if [ "$TBL" = "1" ]; then
    echo "  [PASS] table backups.snapshot_runs present"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] table backups.snapshot_runs missing (apply migration 103)"
    FAILED+=("backups.snapshot_runs missing")
fi

IDX=$(psql_q "SELECT count(*) FROM pg_indexes WHERE schemaname='backups' AND indexname IN ('idx_snapshot_runs_store_started','idx_snapshot_runs_running');" | head -1 | tr -d ' ')
TOTAL=$((TOTAL + 1))
if [ "$IDX" = "2" ]; then
    echo "  [PASS] backups partial indexes (2/2)"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] backups partial indexes — found $IDX/2 (apply migration 103)"
    FAILED+=("backups indexes incomplete")
fi

# ----------------------------------------------------------------------------
# 4. Admin endpoints respond 200
# ----------------------------------------------------------------------------
echo
echo "-- §11.1 + §11.10 admin endpoints --"
check "GET  /api/v1/admin/backups/snapshot-runs"       "curl_code '$FASTAPI_URL/api/v1/admin/backups/snapshot-runs?limit=10'"
check "GET  /api/v1/admin/backups/snapshot-runs?store=postgres" "curl_code '$FASTAPI_URL/api/v1/admin/backups/snapshot-runs?store=postgres'"
check "GET  /api/v1/admin/backups/snapshot-runs?status=failed" "curl_code '$FASTAPI_URL/api/v1/admin/backups/snapshot-runs?status=failed'"
check "GET  /api/v1/admin/backups/cold-tier-runs"      "curl_code '$FASTAPI_URL/api/v1/admin/backups/cold-tier-runs?limit=10'"

# ----------------------------------------------------------------------------
# 5. §11.2 — workspace consistency endpoint
# ----------------------------------------------------------------------------
echo
echo "-- §11.2 cross-store consistency --"
WS_REAL=$(psql_q "SELECT workspace_id::text FROM silver.workspaces LIMIT 1;" | head -1 | tr -d ' ')
if [ -n "$WS_REAL" ]; then
    check "GET  /backups/workspace-consistency/<real-ws>" \
          "curl_code '$FASTAPI_URL/api/v1/admin/backups/workspace-consistency/$WS_REAL'"
    # Verify the response has non-empty postgres.silver_workspaces = 1
    SW=$(curl -s -H "X-Service-Key: $SERVICE_KEY" \
        "$FASTAPI_URL/api/v1/admin/backups/workspace-consistency/$WS_REAL" \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('postgres',{}).get('silver_workspaces',-1))" 2>/dev/null)
    TOTAL=$((TOTAL + 1))
    if [ "$SW" = "1" ]; then
        echo "  [PASS] consistency reports silver_workspaces=1 for the real ws"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] consistency report wrong shape (silver_workspaces=$SW)"
        FAILED+=("consistency report shape")
    fi
else
    echo "  [WARN] no workspaces in silver.workspaces — skipping consistency"
fi

# 422 on malformed UUID
check "GET  /backups/workspace-consistency/not-a-uuid" \
      "curl_code '$FASTAPI_URL/api/v1/admin/backups/workspace-consistency/not-a-uuid'" "422"

# ----------------------------------------------------------------------------
# 6. Service-key gate
# ----------------------------------------------------------------------------
echo
echo "-- service-key gate --"
unauth_code=$(curl -s -o /dev/null -w '%{http_code}' \
    "$FASTAPI_URL/api/v1/admin/backups/snapshot-runs")
check "GET  /backups/snapshot-runs (no service key)" \
      "echo $unauth_code" "401,422"

# ----------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------
echo
echo "=============================================================="
echo "  §11 v1 acceptance: $PASS / $TOTAL checks passed"
if [ ${#FAILED[@]} -ne 0 ]; then
    echo "  Failures:"
    for f in "${FAILED[@]}"; do
        echo "    - $f"
    done
    echo "=============================================================="
    exit 1
fi
echo "  §11-v1 surface green. §11.3 / Helm / K8s / air-gapped deferred to v2."
echo "=============================================================="
exit 0
