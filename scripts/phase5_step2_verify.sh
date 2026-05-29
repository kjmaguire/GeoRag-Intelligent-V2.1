#!/usr/bin/env bash
# =============================================================================
# scripts/phase5_step2_verify.sh
#
# Phase 5 Step 2 done-definition — per-flow JWT signing keys (R-P4-4).
#
#   1. workflow.flow_registry has jwt_secret_kid + jwt_secret_ciphertext cols
#   2. set_flow_jwt_secret() + get_flow_jwt_secret() functions present
#   3. provision-key CLI subcommand writes a per-flow secret round-trip
#   4. mint of a flow WITHOUT per-flow key → token has NO kid claim
#   5. mint of a flow WITH per-flow key → token has matching kid claim
#   6. verify_flow_jwt_token accepts a token signed with the per-flow key
#   7. verify rejects a token whose kid doesn't match the registry
#   8. Existing env-var-signed tokens still verify (backward-compat)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=8
ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
BASE="${FASTAPI_INTERNAL_URL:-http://localhost:8000}"

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

cleanup() {
    docker exec georag-postgresql psql -U georag -d georag -q -c "
        UPDATE workflow.flow_registry
           SET jwt_secret_kid = NULL,
               jwt_secret_ciphertext = NULL
         WHERE flow_name = 'phase2_smoke';
    " >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Wait for fastapi readiness post-restart.
for _ in $(seq 1 30); do
    s=$(curl -s -o /dev/null -w '%{http_code}' "$BASE/health" 2>/dev/null || true)
    [ "$s" = "200" ] && break
    sleep 2
done

cat <<'BANNER'

============================================================
PHASE 5 STEP 2 — PER-FLOW JWT KEY VERIFICATION
============================================================
BANNER

# 1) Schema columns present
cols=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM information_schema.columns
     WHERE table_schema='workflow' AND table_name='flow_registry'
       AND column_name IN ('jwt_secret_kid','jwt_secret_ciphertext');" | tr -d ' ')
[ "$cols" = "2" ] && check "jwt_secret_kid + jwt_secret_ciphertext columns present" ok \
    || check "schema" fail "got $cols"

# 2) Functions present
fns=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(DISTINCT routine_name) FROM information_schema.routines
     WHERE routine_schema='workflow'
       AND routine_name IN ('set_flow_jwt_secret','get_flow_jwt_secret');" | tr -d ' ')
[ "$fns" = "2" ] && check "set/get_flow_jwt_secret() functions present" ok \
    || check "functions" fail "got $fns"

# 3) provision-key CLI round-trip
out=$(bash /home/georag/projects/georag/scripts/phase3_jwt_rotate.sh \
    provision-key phase2_smoke primary 2>&1)
SECRET_LINE=$(echo "$out" | grep -E "^  secret +:" || true)
if [ -n "$SECRET_LINE" ]; then
    check "provision-key CLI writes per-flow secret" ok
else
    check "CLI provision" fail "$out"
fi

# 4) Mint WITHOUT per-flow key — clear it first, then mint, then
#    inspect the unverified JWT header. Use a flow that hasn't been
#    provisioned: external_notification.
header_no_kid=$(docker exec georag-fastapi python3 -c "
import sys, jwt
sys.path.insert(0, '/app')
from app.services.flow_jwt import mint_flow_jwt, invalidate_per_flow_key_cache
invalidate_per_flow_key_cache('external_notification')
t = mint_flow_jwt('external_notification', ttl_seconds=120)
h = jwt.get_unverified_header(t)
print('kid' in h)
" 2>&1 | tail -1)
[ "$header_no_kid" = "False" ] \
    && check "Flow without per-flow key → mint omits kid claim" ok \
    || check "no-kid mint" fail "header.kid present? $header_no_kid"

# 5) Mint WITH per-flow key — invalidate cache, mint, kid should match.
mint_with_kid=$(docker exec georag-fastapi python3 -c "
import sys, jwt
sys.path.insert(0, '/app')
from app.services.flow_jwt import mint_flow_jwt, invalidate_per_flow_key_cache
invalidate_per_flow_key_cache('phase2_smoke')
t = mint_flow_jwt('phase2_smoke', ttl_seconds=120)
h = jwt.get_unverified_header(t)
print(h.get('kid'))
" 2>&1 | tail -1)
[ "$mint_with_kid" = "primary" ] \
    && check "Flow with per-flow key → mint sets kid='primary'" ok \
    || check "with-kid mint" fail "got kid=$mint_with_kid"

# 6) Verify-with-per-flow-key happy path: mint + verify in process.
verify_ok=$(docker exec georag-fastapi python3 -c "
import sys
sys.path.insert(0, '/app')
from app.services.flow_jwt import (
    mint_flow_jwt, verify_flow_jwt_token, invalidate_per_flow_key_cache,
)
invalidate_per_flow_key_cache()
t = mint_flow_jwt('phase2_smoke', ttl_seconds=120)
try:
    claims = verify_flow_jwt_token(t, 'phase2_smoke')
    print('OK', claims.get('scope'))
except Exception as e:
    print('FAIL', e)
" 2>&1 | tail -1)
case "$verify_ok" in
    OK*) check "verify_flow_jwt_token accepts per-flow-key-signed token" ok ;;
    *)   check "verify per-flow" fail "$verify_ok" ;;
esac

# 7) Tamper with kid: replace 'primary' with 'wrong' (uses jwt encode
#    + the old secret — but with a different kid header).
kid_mismatch=$(docker exec georag-fastapi python3 -c "
import sys, jwt
sys.path.insert(0, '/app')
from app.services.flow_jwt import (
    mint_flow_jwt, verify_flow_jwt_token, invalidate_per_flow_key_cache,
    ALGORITHM, ISSUER, AUDIENCE,
)
invalidate_per_flow_key_cache()
# Mint a normal token, then re-encode swapping the kid header.
import time
now = int(time.time())
# Use a random secret so verify will fail kid lookup AND signature.
secret = 'unknown-key-not-in-registry'
t = jwt.encode(
    {'iss': ISSUER, 'aud': AUDIENCE, 'sub': 'kestra',
     'scope': 'flow:phase2_smoke', 'iat': now, 'exp': now + 120},
    secret, algorithm=ALGORITHM, headers={'kid': 'rotation-not-provisioned'},
)
try:
    verify_flow_jwt_token(t, 'phase2_smoke')
    print('UNEXPECTED_PASS')
except Exception as e:
    print('REJECTED', type(e).__name__)
" 2>&1 | tail -1)
case "$kid_mismatch" in
    REJECTED*) check "Verify rejects token with kid not in registry" ok ;;
    *)         check "kid mismatch path" fail "$kid_mismatch" ;;
esac

# 8) Env-var-signed token still verifies for a flow WITHOUT per-flow key.
backcompat=$(docker exec georag-fastapi python3 -c "
import sys
sys.path.insert(0, '/app')
from app.services.flow_jwt import (
    mint_flow_jwt, verify_flow_jwt_token, invalidate_per_flow_key_cache,
)
invalidate_per_flow_key_cache('external_notification')
t = mint_flow_jwt('external_notification', ttl_seconds=120)
try:
    verify_flow_jwt_token(t, 'external_notification')
    print('OK')
except Exception as e:
    print('FAIL', e)
" 2>&1 | tail -1)
[ "$backcompat" = "OK" ] \
    && check "Env-var fallback path still verifies (backward-compat)" ok \
    || check "backward compat" fail "$backcompat"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
