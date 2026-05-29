#!/usr/bin/env bash
# =============================================================================
# scripts/phase3_step3_verify.sh
#
# Phase 3 Step 3 done-definition — generic flow trigger + per-flow JWT.
#
#   1. flow_jwt module imports cleanly inside fastapi
#   2. KESTRA_FLOW_JWT_SECRET is configured
#   3. POST without any auth                          → 401
#   4. POST with X-Service-Key (legacy)               → 202   (co-existence)
#   5. POST with right-flow JWT                       → 202   (Phase 3 happy path)
#   6. POST with wrong-flow JWT                       → 403   (scope mismatch)
#   7. POST with expired JWT                          → 401
#   8. POST with garbage JWT                          → 401
#   9. Flag rename migration applied (flows.* rows present)
#  10. Hatchet workflows now read flows.<flow>.enabled (not activepieces.*)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=10
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
PHASE 3 STEP 3 — PER-FLOW JWT VERIFICATION
============================================================
BANNER

# Wait for fastapi readiness — prior verifiers may have restarted it.
for _ in $(seq 1 30); do
    s=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health" 2>/dev/null || true)
    if [ "$s" = "200" ]; then break; fi
    sleep 2
done

# 1) Module imports
import_check=$(docker exec georag-fastapi python3 -c "
import sys; sys.path.insert(0, '/app')
from app.services.flow_jwt import mint_flow_jwt, verify_flow_jwt_token, ISSUER, AUDIENCE
print('OK' if (ISSUER == 'georag-kestra' and AUDIENCE == 'georag-fastapi-flows') else 'BAD')
" 2>&1 | tail -1)
[ "$import_check" = "OK" ] && check "flow_jwt module imports + constants correct" ok \
    || check "module import" fail "$import_check"

# 2) Secret configured
secret_ok=$(docker exec georag-fastapi python3 -c "
import sys; sys.path.insert(0, '/app')
from app.config import settings
print('OK' if (getattr(settings, 'KESTRA_FLOW_JWT_SECRET', '') and len(settings.KESTRA_FLOW_JWT_SECRET) >= 32) else 'BAD')
" 2>&1 | tail -1)
[ "$secret_ok" = "OK" ] && check "KESTRA_FLOW_JWT_SECRET configured (>=32 bytes)" ok \
    || check "secret config" fail "$secret_ok"

# Mint test JWTs
JWT_RIGHT=$(docker exec georag-fastapi python3 -c "
import sys; sys.path.insert(0, '/app')
from app.services.flow_jwt import mint_flow_jwt
print(mint_flow_jwt('phase2_smoke', ttl_seconds=300), end='')
")
JWT_WRONG=$(docker exec georag-fastapi python3 -c "
import sys; sys.path.insert(0, '/app')
from app.services.flow_jwt import mint_flow_jwt
print(mint_flow_jwt('public_geoscience_pull', ttl_seconds=300), end='')
")
JWT_EXPIRED=$(docker exec georag-fastapi python3 -c "
import sys; sys.path.insert(0, '/app')
from app.services.flow_jwt import mint_flow_jwt
print(mint_flow_jwt('phase2_smoke', ttl_seconds=-60), end='')
")

URL="$BASE/internal/v1/integrations/phase2_smoke/trigger"
BODY='{"note":"step3-verify"}'

post() {
    curl -s -o /dev/null -w '%{http_code}' -X POST "$URL" \
        -H 'Content-Type: application/json' "$@" -d "$BODY"
}

# 3) No auth → 401
http=$(post)
[ "$http" = "401" ] && check "no auth → 401" ok || check "no-auth gate" fail "got $http"

# 4) X-Service-Key → 401 (Phase 3 Step 7 removed the legacy fallback;
#    only Bearer JWT is accepted on the trigger route).
http=$(post -H "X-Service-Key: $KEY")
[ "$http" = "401" ] && check "X-Service-Key (legacy) → 401 (sunset)" ok || check "legacy auth removed" fail "got $http"

# 5) Right-flow JWT → 202
http=$(post -H "Authorization: Bearer $JWT_RIGHT")
[ "$http" = "202" ] && check "right-flow JWT → 202" ok || check "right JWT" fail "got $http"

# 6) Wrong-flow JWT → 403
http=$(post -H "Authorization: Bearer $JWT_WRONG")
[ "$http" = "403" ] && check "wrong-flow JWT → 403 (scope mismatch)" ok || check "wrong JWT scope" fail "got $http"

# 7) Expired JWT → 401
http=$(post -H "Authorization: Bearer $JWT_EXPIRED")
[ "$http" = "401" ] && check "expired JWT → 401" ok || check "expired JWT" fail "got $http"

# 8) Garbage JWT → 401
http=$(post -H 'Authorization: Bearer not.a.jwt')
[ "$http" = "401" ] && check "garbage JWT → 401" ok || check "garbage JWT" fail "got $http"

# 9) Flag rename migration applied
n_new=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM workspace.feature_flags
     WHERE flag_name LIKE 'flows.%.enabled';" 2>/dev/null | tr -d ' ')
[ "$n_new" -ge 2 ] 2>/dev/null \
    && check "flag rename migration applied (flows.* rows=$n_new)" ok \
    || check "flag rename" fail "got $n_new"

# 10) Workflows read the new flag — grep the source
new_in_src=$(docker exec georag-hatchet-worker-ai bash -c "
    grep -l 'flows.public_geoscience_pull.enabled' /app/app/hatchet_workflows/public_geoscience_pull.py >/dev/null 2>&1 \
    && grep -l 'flows.external_notification.enabled' /app/app/hatchet_workflows/external_notification.py >/dev/null 2>&1 \
    && echo OK || echo MISSING
")
[ "$new_in_src" = "OK" ] && check "Workflows read flows.<flow>.enabled (not activepieces.*)" ok \
    || check "workflow flag-name source" fail "$new_in_src"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
