#!/usr/bin/env bash
# =============================================================================
# scripts/phase3_step2_verify.sh
#
# Phase 3 Step 2 done-definition — Kestra docker service.
#
#   1. kestra container running
#   2. healthcheck = healthy (management server :8081/health)
#   3. Kestra Postgres tables created in the kestra DB
#   4. UI port reachable (HTTP 200/302/401 on / )
#   5. Auth required — /api/v1/main/flows/search returns 401 without basic-auth
#   6. Auth working — same endpoint returns 200 with the configured creds
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
PORT="${KESTRA_PORT:-8086}"
ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
USER=$(awk -F= '/^KESTRA_BASIC_AUTH_USER=/  { print $2 }' "$ENVFILE" 2>/dev/null | head -1)
PASSWORD=$(awk -F= '/^KESTRA_BASIC_AUTH_PASSWORD=/ { print $2 }' "$ENVFILE" 2>/dev/null | head -1)

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
PHASE 3 STEP 2 — KESTRA SERVICE VERIFICATION
============================================================
BANNER

# 1) Running
status=$(docker inspect --format='{{.State.Status}}' georag-kestra 2>/dev/null)
[ "$status" = "running" ] \
    && check "georag-kestra container running" ok \
    || check "container running" fail "got '$status'"

# 2) Healthcheck — wait up to 150s
hc=""
for i in $(seq 1 30); do
    hc=$(docker inspect --format='{{.State.Health.Status}}' georag-kestra 2>/dev/null)
    if [ "$hc" = "healthy" ]; then break; fi
    sleep 5
done
[ "$hc" = "healthy" ] \
    && check "container healthcheck = healthy" ok \
    || check "healthcheck" fail "status='$hc' after 150s"

# 3) Kestra tables exist in kestra DB
n_tables=$(docker exec georag-postgresql psql -U georag -d kestra -tAc \
    "SELECT count(*) FROM information_schema.tables
      WHERE table_schema='public'
        AND table_name IN ('flows','executions','triggers','queues','workers');" \
    2>/dev/null | tr -d ' ')
[ -n "$n_tables" ] && [ "$n_tables" -ge 4 ] 2>/dev/null \
    && check "Kestra core tables created in kestra DB ($n_tables/5 found)" ok \
    || check "Kestra schema" fail "got $n_tables"

# 4) UI port reachable
ui_status=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${PORT}/")
case "$ui_status" in
    200|301|302|307|308|401) check "host reachable on :${PORT}/ (HTTP $ui_status)" ok ;;
    *)                       check "host reachability" fail "HTTP $ui_status" ;;
esac

# 5) Auth required
noauth=$(curl -s -o /dev/null -w '%{http_code}' \
    "http://localhost:${PORT}/api/v1/main/flows/search?namespace=")
[ "$noauth" = "401" ] \
    && check "API requires basic-auth (no creds → 401)" ok \
    || check "auth gate" fail "got HTTP $noauth"

# 6) Auth working
if [ -z "$USER" ] || [ -z "$PASSWORD" ]; then
    check "API accepts configured basic-auth" fail "no KESTRA_BASIC_AUTH_USER/PASSWORD in .env"
else
    authd=$(curl -s -u "${USER}:${PASSWORD}" -o /dev/null -w '%{http_code}' \
        "http://localhost:${PORT}/api/v1/main/flows/search?namespace=")
    [ "$authd" = "200" ] \
        && check "API accepts configured basic-auth (200)" ok \
        || check "auth working" fail "got HTTP $authd"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
