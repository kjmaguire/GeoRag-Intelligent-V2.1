#!/usr/bin/env bash
# =============================================================================
# scripts/phase3_step7_verify.sh
#
# Phase 3 Step 7 done-definition — Activepieces sunset.
#
#   1. activepieces logical DB does not exist
#   2. activepieces role does not exist
#   3. No `activepieces.*` feature flags in workspace.feature_flags
#   4. activepieces_cache docker volume removed
#   5. No `activepieces` service in docker-compose's `dev-data` profile
#   6. No `pgsql_activepieces` connection in Laravel config
#   7. No X-Service-Key fallback in integrations_trigger trigger path
#      (the legacy header is now rejected; only Bearer JWT is accepted)
#   8. Master regression sweep — Phase 1 + Phase 2 Step 6/7 + Phase 3
#      Steps 1–6 all pass
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=8

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
PHASE 3 STEP 7 — ACTIVEPIECES SUNSET VERIFICATION
============================================================
BANNER

# 1) DB
db_exists=$(docker exec georag-postgresql psql -U georag -d georag -tAc \
    "SELECT count(*) FROM pg_database WHERE datname='activepieces';" 2>/dev/null | tr -d ' ')
[ "$db_exists" = "0" ] && check "activepieces DB dropped" ok \
    || check "activepieces DB still exists" fail "count=$db_exists"

# 2) Role
role_exists=$(docker exec georag-postgresql psql -U georag -d georag -tAc \
    "SELECT count(*) FROM pg_roles WHERE rolname='activepieces';" 2>/dev/null | tr -d ' ')
[ "$role_exists" = "0" ] && check "activepieces role dropped" ok \
    || check "activepieces role still exists" fail "count=$role_exists"

# 3) Feature flags
flag_count=$(docker exec georag-postgresql psql -U georag -d georag -tAc \
    "SELECT count(*) FROM workspace.feature_flags WHERE flag_name LIKE 'activepieces.%';" \
    2>/dev/null | tr -d ' ')
[ "$flag_count" = "0" ] && check "activepieces.* flags removed" ok \
    || check "lingering flags" fail "count=$flag_count"

# 4) Volume
vol_count=$(docker volume ls --format '{{.Name}}' | grep -c activepieces || true)
[ "$vol_count" = "0" ] && check "activepieces_cache volume removed" ok \
    || check "lingering volume" fail "count=$vol_count"

# 5) No service in compose dev-data profile
service_present=$(docker compose -f /home/georag/projects/georag/docker-compose.yml \
    --profile dev-data config --services 2>/dev/null | grep -c '^activepieces$' || true)
[ "$service_present" = "0" ] && check "activepieces NOT in docker-compose dev-data profile" ok \
    || check "compose service" fail "still listed"

# 6) No pgsql_activepieces connection in Laravel config
pgsql_ap=$(docker exec georag-laravel-octane grep -c "'pgsql_activepieces'" /app/config/database.php 2>/dev/null || true)
[ "$pgsql_ap" = "0" ] && check "pgsql_activepieces connection removed from config" ok \
    || check "pgsql_activepieces" fail "still present (n=$pgsql_ap)"

# 7) X-Service-Key path no longer accepted on the trigger route.
#    Phase 3 Step 7 — Bearer JWT is the only auth.
ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
KEY=$(grep '^FASTAPI_SERVICE_KEY=' "$ENVFILE" | cut -d= -f2- | head -1)
http=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST 'http://localhost:8000/internal/v1/integrations/phase2_smoke/trigger' \
    -H 'Content-Type: application/json' \
    -H "X-Service-Key: $KEY" \
    -d '{"note":"step7"}')
[ "$http" = "401" ] && check "X-Service-Key fallback removed (legacy auth → 401)" ok \
    || check "legacy auth removed" fail "got HTTP $http (expected 401)"

# 8) Master regression sweep — exact N/N match. Phase 4 Step 6
# archived the phase1_step{4,5b,6} verifiers along with the
# silver.shadow_runs table; the rest survive.
echo
echo "  ── Regression sweep — Phase 1 Step 7 + Phase 2 Step 7 + Phase 3 ──"
fail=0
for s in phase1_step7 \
         phase2_step7 \
         phase3_step1 phase3_step2 phase3_step3 phase3_step4 phase3_step5 phase3_step6; do
    r=$(bash /home/georag/projects/georag/scripts/${s}_verify.sh 2>&1 | grep -E '^Result' | head -1)
    # Strict pass: line must end with "N / N checks passed"
    if [[ "$r" =~ Result:\ ([0-9]+)\ /\ ([0-9]+)\ checks\ passed ]]; then
        n_pass=${BASH_REMATCH[1]}
        n_total=${BASH_REMATCH[2]}
        if [ "$n_pass" = "$n_total" ]; then
            echo "    $s: $r"
        else
            echo "    $s: $r  [REGRESSION]"
            fail=$((fail+1))
        fi
    else
        echo "    $s: $r  [REGRESSION-no-result-line]"
        fail=$((fail+1))
    fi
done
[ "$fail" = "0" ] \
    && check "Master regression sweep — all upstream verifiers green" ok \
    || check "regression" fail "$fail verifier(s) failed"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
