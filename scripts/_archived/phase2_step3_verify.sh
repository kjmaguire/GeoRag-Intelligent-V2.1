#!/usr/bin/env bash
# =============================================================================
# scripts/phase2_step3_verify.sh
#
# Phase 2 Step 3 done-definition — `/internal/v1/integrations/...` route.
#
#   1. Route registered + reachable (GET /flows lists phase2_smoke)
#   2. POST without X-Service-Key returns 401
#   3. POST with wrong X-Service-Key returns 401
#   4. POST against unknown flow_name returns 404
#   5. POST against phase2_smoke returns 202 with a workflow_run_id
#   6. The dispatched workflow actually appears in the Hatchet engine
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6

ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
KEY=$(awk -F= '/^FASTAPI_SERVICE_KEY=/ { print $2 }' "$ENVFILE" 2>/dev/null | head -1)
BASE="${FASTAPI_INTERNAL_URL:-http://localhost:8000}"

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
PHASE 2 STEP 3 — INTEGRATIONS_TRIGGER ROUTE VERIFICATION
============================================================
BANNER

if [ -z "$KEY" ]; then
    echo "  [FAIL] could not read FASTAPI_SERVICE_KEY from $ENVFILE"
    exit 1
fi

# 1) GET /flows lists phase2_smoke
flows=$(curl -fsS "$BASE/internal/v1/integrations/flows" -H "X-Service-Key: $KEY" 2>/dev/null)
case "$flows" in
    *phase2_smoke*) check "GET /flows lists phase2_smoke" ok ;;
    *)              check "GET /flows" fail "got: $flows" ;;
esac

# 2) Auth required — no key
nokey=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST "$BASE/internal/v1/integrations/phase2_smoke/trigger" \
    -H 'Content-Type: application/json' -d '{"note":"x"}')
[ "$nokey" = "401" ] \
    && check "POST without X-Service-Key returns 401" ok \
    || check "no-key auth" fail "got HTTP $nokey"

# 3) Auth required — wrong key
wrong=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST "$BASE/internal/v1/integrations/phase2_smoke/trigger" \
    -H 'Content-Type: application/json' -H 'X-Service-Key: bogus' \
    -d '{"note":"x"}')
[ "$wrong" = "401" ] \
    && check "POST with wrong X-Service-Key returns 401" ok \
    || check "wrong-key auth" fail "got HTTP $wrong"

# 4) Unknown flow returns 404
unk=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST "$BASE/internal/v1/integrations/no_such_flow/trigger" \
    -H 'Content-Type: application/json' -H "X-Service-Key: $KEY" \
    -d '{}')
[ "$unk" = "404" ] \
    && check "Unknown flow_name returns 404" ok \
    || check "unknown-flow handling" fail "got HTTP $unk"

# 5) Happy path returns 202 with workflow_run_id
ok_resp=$(curl -fsS -X POST "$BASE/internal/v1/integrations/phase2_smoke/trigger" \
    -H 'Content-Type: application/json' -H "X-Service-Key: $KEY" \
    -d '{"note":"phase2-step3-verify"}' 2>&1)
run_id=$(echo "$ok_resp" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("workflow_run_id",""))' 2>/dev/null)
if [ -n "$run_id" ]; then
    check "POST phase2_smoke returns 202 + workflow_run_id (id=${run_id:0:8}…)" ok
else
    check "happy-path dispatch" fail "no workflow_run_id in: $ok_resp"
fi

# 6) The dispatched workflow appears in the Hatchet V1 engine OLAP table.
#    aio_run_no_wait() returns external_id from v1_runs_olap (the V1 engine
#    fronts what was the Workflow/WorkflowRun table pair in V0).
if [ -n "$run_id" ]; then
    sleep 2
    found=$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc \
        "SELECT count(*) FROM v1_runs_olap WHERE external_id = '${run_id}'::uuid;" \
        2>/dev/null | tr -d ' ')
    [ "$found" = "1" ] \
        && check "v1_runs_olap row visible (external_id=${run_id:0:8}…)" ok \
        || check "engine-side persistence" fail "got count=$found"
else
    check "engine-side persistence" fail "(skipped — no run_id from previous step)"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
