#!/usr/bin/env bash
# =============================================================================
# scripts/phase1_step7_verify.sh
#
# Phase 1 Step 7 done-definition verifier — Hatchet Worker Dashboard.
#
#   1. HatchetWorkersController class loads
#   2. /admin/hatchet-workers route registered
#   3. pgsql_hatchet connection reachable + reads "Worker"
#   4. Pool rollup non-empty + sees both ingestion + ai pools live
#   5. Inertia page TSX present
#   6. Recent workflow runs queryable (last 24h)
#
# UI rendering correctness depends on `npm run build` / dev — that's a
# developer concern, not part of this verifier's responsibility.
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

cat <<'BANNER'

============================================================
PHASE 1 STEP 7 — HATCHET WORKER DASHBOARD VERIFICATION
============================================================
BANNER

# 1) + 2) + 3) + 4) Probe
out=$(docker exec georag-laravel-octane php /app/scripts/_phase1_step7_check.php 2>/dev/null)

ctrl=$(echo "$out" | grep -m1 'controller_class=' | cut -d= -f2)
[ "$ctrl" = "App\\Http\\Controllers\\Admin\\HatchetWorkersController" ] \
    && check "HatchetWorkersController loads" ok \
    || check "controller load" fail "got '$ctrl'"

route_count=$(echo "$out" | grep -m1 'route_count=' | cut -d= -f2)
[ "$route_count" = "1" ] \
    && check "admin/hatchet-workers route registered" ok \
    || check "route count" fail "got $route_count / 1"

worker_total=$(echo "$out" | grep -m1 'hatchet_worker_total=' | cut -d= -f2-)
case "$worker_total" in
    ''|*ERROR*) check "pgsql_hatchet reachable + Worker queryable" fail "$worker_total" ;;
    *)          check "pgsql_hatchet reachable + Worker queryable (count=$worker_total)" ok ;;
esac

pool_count=$(echo "$out" | grep -m1 'pool_count=' | cut -d= -f2)
[ -n "$pool_count" ] && [ "$pool_count" -ge 2 ] 2>/dev/null \
    && check "Pool rollup non-empty (pools=$pool_count)" ok \
    || check "pool rollup" fail "got $pool_count"

# 5) Inertia page TSX present
inertia_present=$(docker exec georag-laravel-octane bash -c '
    [ -f /app/resources/js/Pages/Admin/HatchetWorkers.tsx ] && echo 1 || echo 0
')
[ "$inertia_present" = "1" ] \
    && check "Inertia page TSX present (HatchetWorkers.tsx)" ok \
    || check "TSX file" fail "missing"

# 6) Recent runs query smoke — directly hit hatchet DB, last 24h
recent_count=$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc \
    'SELECT count(*) FROM "WorkflowRun" WHERE "createdAt" > now() - interval '"'"'24 hours'"'"';' \
    2>/dev/null | tr -d ' ')
case "$recent_count" in
    ''|*ERROR*) check "Recent WorkflowRuns queryable (last 24h)" fail "$recent_count" ;;
    *)          check "Recent WorkflowRuns queryable (last 24h count=$recent_count)" ok ;;
esac

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"
echo
echo 'NOTE: UI rendering depends on npm run build / npm run dev.'
echo

exit $((PASS == TOTAL ? 0 : 1))
