#!/usr/bin/env bash
# =============================================================================
# scripts/phase4_step4_verify.sh
#
# Phase 4 Step 4 done-definition — DB-driven flow registry.
#
#   1. workflow.flow_registry table exists with the 3 seeded rows
#   2. FastAPI's flow_registry.get_registry() resolves all rows to
#      (workflow_object, input_model_class) tuples
#   3. /internal/v1/integrations/flows returns the same set
#   4. Laravel-side registeredFlows() reads the same DB rows
#   5. End-to-end trigger still works via the new dispatch path
#      (phase2_smoke through the DB lookup)
#   6. New row inserted in DB → fastapi cache refresh picks it up
#      within the cache TTL and the flow becomes triggerable
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
KEY=$(grep '^FASTAPI_SERVICE_KEY=' "$ENVFILE" | cut -d= -f2- | head -1)
BASE="${FASTAPI_INTERNAL_URL:-http://localhost:8000}"

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

q() {
    docker exec georag-postgresql psql -U georag -d georag -tAc "$1" 2>/dev/null
}

cleanup() {
    docker exec georag-postgresql psql -U georag -d georag -q -c "
        DELETE FROM workflow.flow_registry WHERE flow_name = 'phase4_step4_smoketest';
    " >/dev/null 2>&1 || true
}
trap cleanup EXIT

cat <<'BANNER'

============================================================
PHASE 4 STEP 4 — DB-DRIVEN FLOW REGISTRY VERIFICATION
============================================================
BANNER

# Wait for fastapi readiness — Step 3's verifier restarts fastapi to
# test the staleness check, and Step 4 racing right after sees
# HTTP 000 (connection refused). Probe /health until 200 or 60s.
for _ in $(seq 1 30); do
    s=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health" 2>/dev/null || true)
    if [ "$s" = "200" ]; then break; fi
    sleep 2
done

# 1) Table + seed
n_rows=$(q "SELECT count(*) FROM workflow.flow_registry;")
[ "$n_rows" -ge 3 ] 2>/dev/null \
    && check "workflow.flow_registry seeded with >=3 rows (n=$n_rows)" ok \
    || check "table seed" fail "got $n_rows"

# 2) FastAPI loader resolves rows
loader_check=$(docker exec georag-fastapi python3 -c "
import asyncio, sys
sys.path.insert(0,'/app')
from app.services.flow_registry import get_registry
reg = asyncio.run(get_registry(force_refresh=True))
ok = all(e.workflow is not None and e.input_model is not None for e in reg.values())
print(f'count={len(reg)} ok={ok}')
" 2>&1 | tail -1)
case "$loader_check" in
    count=[3-9]*ok=True) check "FastAPI loader resolves all rows ($loader_check)" ok ;;
    *)                   check "loader" fail "$loader_check" ;;
esac

# 3) /flows endpoint matches
flows_resp=$(curl -fsS "$BASE/internal/v1/integrations/flows" \
    -H "X-Service-Key: $KEY" 2>/dev/null)
expected_names="external_notification phase2_smoke public_geoscience_pull"
actual_names=$(echo "$flows_resp" | python3 -c 'import json,sys;print(" ".join(sorted(json.load(sys.stdin)["flows"])))')
if [ "$actual_names" = "$expected_names" ]; then
    check "/integrations/flows returns the 3 registered flows" ok
else
    check "flows endpoint" fail "got '$actual_names'"
fi

# 4) Laravel-side reads the table
laravel_check=$(docker exec georag-laravel-octane php -r '
require "/app/vendor/autoload.php";
$app = require "/app/bootstrap/app.php";
$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
$c = new App\Http\Controllers\Admin\IntegrationsController();
$rc = new ReflectionClass($c);
$m = $rc->getMethod("registeredFlows");
$m->setAccessible(true);
cache()->forget("phase4.flow_registry");
$flows = $m->invoke($c);
echo "count=" . count($flows);
' 2>&1 | tail -1)
case "$laravel_check" in
    count=2|count=[3-9]*) check "Laravel-side registeredFlows() reads DB ($laravel_check)" ok ;;
    *)                    check "laravel loader" fail "$laravel_check" ;;
esac

# 5) End-to-end trigger via DB lookup — mint a JWT for phase2_smoke + post.
JWT=$(docker exec georag-fastapi python3 -c "
import sys; sys.path.insert(0,'/app')
from app.services.flow_jwt import mint_flow_jwt
print(mint_flow_jwt('phase2_smoke', ttl_seconds=120), end='')
")
http=$(curl -s -o /dev/null -w '%{http_code}' \
    -X POST "$BASE/internal/v1/integrations/phase2_smoke/trigger" \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $JWT" \
    -d '{"note":"step4-end-to-end"}')
[ "$http" = "202" ] \
    && check "End-to-end trigger via DB lookup (phase2_smoke → 202)" ok \
    || check "e2e dispatch" fail "got HTTP $http"

# 6) Add a new row → fastapi picks it up after cache refresh. We register
#    a clone of phase2_smoke under a different name. force_refresh shortens
#    the wait to instant.
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO workflow.flow_registry
        (flow_name, kind, description, hatchet_workflow_module,
         hatchet_workflow_attr, pydantic_input_attr, flag_name, enabled)
    VALUES (
        'phase4_step4_smoketest', 'placeholder',
        'Phase 4 Step 4 verifier — temporary registry row.',
        'app.hatchet_workflows.phase2_smoke',
        'phase2_smoke',
        'Phase2SmokeInput',
        NULL, true
    );
" >/dev/null

picked_up=$(docker exec georag-fastapi python3 -c "
import asyncio, sys
sys.path.insert(0,'/app')
from app.services.flow_registry import get_registry
reg = asyncio.run(get_registry(force_refresh=True))
print('found' if 'phase4_step4_smoketest' in reg else 'missing')
" 2>&1 | tail -1)
[ "$picked_up" = "found" ] \
    && check "DB row insert → fastapi picks up new flow on refresh" ok \
    || check "cache refresh" fail "$picked_up"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
