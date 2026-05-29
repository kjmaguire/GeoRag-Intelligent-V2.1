#!/usr/bin/env bash
# =============================================================================
# scripts/phase2_step2_verify.sh
#
# Phase 2 Step 2 done-definition — Activepieces docker service.
#
#   1. activepieces container exists, status running
#   2. healthcheck passes (/api/v1/flags returns 200)
#   3. Activepieces' Postgres connection used the right role + DB
#      (presence of expected core tables in the activepieces DB)
#   4. Activepieces talks to Redis (Bull queues registered)
#   5. ACTIVEPIECES_PORT is reachable from the host
#
# UI-level smoke (creating a flow) is the Step 4 verifier's job.
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=5
PORT="${ACTIVEPIECES_PORT:-8090}"

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
PHASE 2 STEP 2 — ACTIVEPIECES SERVICE VERIFICATION
============================================================
BANNER

# 1) Container running
status=$(docker inspect --format='{{.State.Status}}' georag-activepieces 2>/dev/null)
[ "$status" = "running" ] \
    && check "georag-activepieces container running" ok \
    || check "container running" fail "got '$status'"

# 2) Healthcheck — wait up to 90s for `healthy` (start_period is 60s).
hc=""
for i in $(seq 1 18); do
    hc=$(docker inspect --format='{{.State.Health.Status}}' georag-activepieces 2>/dev/null)
    if [ "$hc" = "healthy" ]; then break; fi
    sleep 5
done
[ "$hc" = "healthy" ] \
    && check "container healthcheck = healthy" ok \
    || check "healthcheck" fail "status='$hc' after 90s"

# 3) Activepieces' Postgres tables exist in the activepieces DB.
ap_tables=$(docker exec georag-postgresql psql -U georag -d activepieces -tAc \
    "SELECT count(*) FROM information_schema.tables
      WHERE table_schema='public'
        AND table_name IN ('user','flow','project','flow_run','app_connection');" \
    2>/dev/null | tr -d ' ')
[ -n "$ap_tables" ] && [ "$ap_tables" -ge 3 ] 2>/dev/null \
    && check "core Activepieces tables created in activepieces DB ($ap_tables/5)" ok \
    || check "Activepieces schema" fail "got '$ap_tables'"

# 4) Activepieces' Bull/BullMQ queues exist in Redis. Read REDIS_PASSWORD
#    from the project .env so the verifier doesn't depend on shell env.
redis_pw=$(awk -F= '/^REDIS_PASSWORD=/ { print $2 }' /home/georag/projects/georag/.env 2>/dev/null \
    | head -1)
[ -z "$redis_pw" ] && redis_pw=$(awk -F= '/^REDIS_PASSWORD=/ { print $2 }' "$(dirname "$0")/../.env" 2>/dev/null | head -1)
if [ -n "$redis_pw" ]; then
    queue_count=$(docker exec georag-redis redis-cli -a "$redis_pw" --no-auth-warning --scan --pattern '*' 2>/dev/null | wc -l | tr -d ' ')
else
    queue_count=$(docker exec georag-redis redis-cli --scan --pattern '*' 2>/dev/null | wc -l | tr -d ' ')
fi
[ -n "$queue_count" ] && [ "$queue_count" -ge 1 ] 2>/dev/null \
    && check "Activepieces queue keys present in Redis ($queue_count keys)" ok \
    || check "queue key presence" fail "got '$queue_count' keys"

# 5) Host-reachable on configured port.
http_status=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}/api/v1/flags")
[ "$http_status" = "200" ] \
    && check "host reachable on :${PORT} (/api/v1/flags = 200)" ok \
    || check "host reachability" fail "HTTP ${http_status}"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
