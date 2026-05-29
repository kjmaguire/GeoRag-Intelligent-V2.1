#!/usr/bin/env bash
# =============================================================================
# scripts/phase6_step3_verify.sh
#
# Phase 6 Step 3 done-definition — multi-kid JWT rotation overlap
# (R-P5-2).
#
#   1. workflow.flow_jwt_keys table present + indexed
#   2. workflow.get_flow_jwt_keys() function present + returns rows
#   3. set_flow_jwt_secret() now takes overlap_hours; the prior kid
#      gets valid_until set when overlap > 0
#   4. provision-key CLI accepts 4th overlap_hours arg + the schema
#      records both kids
#   5. Token minted under kid=A still verifies AFTER rotation to kid=B
#      with overlap > 0 (the actual rotation guarantee)
#   6. Token minted under kid=B (the new active kid) also verifies
#   7. provision-key WITHOUT overlap (default 0) retires the prior
#      kid immediately — token minted under the previous kid REJECTS
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
FLOW="phase2_smoke"

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
        DELETE FROM workflow.flow_jwt_keys WHERE flow_name = '$FLOW';
        UPDATE workflow.flow_registry
           SET jwt_secret_kid = NULL,
               jwt_secret_ciphertext = NULL
         WHERE flow_name = '$FLOW';
    " >/dev/null 2>&1 || true
}
trap cleanup EXIT

# Ensure clean baseline before we start.
cleanup

# Wait for fastapi readiness post-restart.
for _ in $(seq 1 30); do
    s=$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:8000/health" 2>/dev/null || true)
    [ "$s" = "200" ] && break
    sleep 2
done

cat <<'BANNER'

============================================================
PHASE 6 STEP 3 — MULTI-KID JWT ROTATION VERIFICATION
============================================================
BANNER

# 1) flow_jwt_keys table
tbl=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM information_schema.tables
     WHERE table_schema='workflow' AND table_name='flow_jwt_keys';" | tr -d ' ')
idx=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM pg_indexes
     WHERE schemaname='workflow' AND tablename='flow_jwt_keys'
       AND indexname IN ('flow_jwt_keys_flow_active_idx','flow_jwt_keys_flow_window_idx');" | tr -d ' ')
if [ "$tbl" = "1" ] && [ "$idx" = "2" ]; then
    check "workflow.flow_jwt_keys table + both indexes present" ok
else
    check "schema" fail "tbl=$tbl indexes=$idx"
fi

# 2) get_flow_jwt_keys() function present
fn=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM information_schema.routines
     WHERE routine_schema='workflow' AND routine_name='get_flow_jwt_keys';" | tr -d ' ')
[ "$fn" = "1" ] \
    && check "workflow.get_flow_jwt_keys() function present" ok \
    || check "function" fail "got $fn"

# 3) Provision kid=alpha + rotate to kid=beta with 24h overlap.
#    Then inspect: there should be TWO rows for $FLOW, alpha has a
#    non-NULL valid_until, beta has NULL.
bash /home/georag/projects/georag/scripts/phase3_jwt_rotate.sh \
    provision-key "$FLOW" alpha 0 >/dev/null
bash /home/georag/projects/georag/scripts/phase3_jwt_rotate.sh \
    provision-key "$FLOW" beta 24 >/dev/null
state=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT kid || ':' || (valid_until IS NOT NULL)::text
      FROM workflow.flow_jwt_keys WHERE flow_name = '$FLOW' ORDER BY valid_from;")
state_clean=$(echo "$state" | tr -d ' ' | tr '\n' '|')
case "$state_clean" in
    *alpha:true*beta:false*)
        check "Rotation with overlap_hours=24 retired alpha (valid_until set) + activated beta (valid_until NULL)" ok ;;
    *)
        check "rotation state" fail "got rows=[$state_clean]" ;;
esac

# 4) CLI fourth-arg overlap recorded in the row
overlap_hours_diff=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT round(EXTRACT(EPOCH FROM (valid_until - clock_timestamp())) / 3600)
      FROM workflow.flow_jwt_keys
     WHERE flow_name = '$FLOW' AND kid = 'alpha';" | tr -d ' ')
if [ -n "$overlap_hours_diff" ] && [ "$overlap_hours_diff" -ge 23 ] 2>/dev/null && [ "$overlap_hours_diff" -le 24 ] 2>/dev/null; then
    check "CLI overlap_hours=24 → alpha.valid_until ≈ now()+24h" ok
else
    check "overlap window" fail "got delta=${overlap_hours_diff}h"
fi

# 5) Token minted under alpha (the rotated-out kid) still verifies
#    while the overlap window is open.
mint_alpha=$(docker exec georag-fastapi python3 -c "
import sys, jwt, time
sys.path.insert(0, '/app')
from app.services.flow_jwt import (
    verify_flow_jwt_token, invalidate_per_flow_key_cache,
    ALGORITHM, ISSUER, AUDIENCE, _get_per_flow_keys,
)
invalidate_per_flow_key_cache()
keys = _get_per_flow_keys('$FLOW')
# Find the alpha secret. _get_per_flow_keys returns most-recent first,
# so alpha is the second (rotated-out but still in overlap).
alpha_secret = None
for kid, secret in keys:
    if kid == 'alpha':
        alpha_secret = secret
        break
if alpha_secret is None:
    print('FAIL alpha not in valid set; keys=', [k for k,_ in keys])
else:
    now = int(time.time())
    t = jwt.encode(
        {'iss': ISSUER, 'aud': AUDIENCE, 'sub': 'kestra',
         'scope': 'flow:$FLOW', 'iat': now, 'exp': now + 120},
        alpha_secret, algorithm=ALGORITHM, headers={'kid': 'alpha'},
    )
    try:
        verify_flow_jwt_token(t, '$FLOW')
        print('OK')
    except Exception as e:
        print('FAIL', e)
" 2>&1 | tail -1)
[ "$mint_alpha" = "OK" ] \
    && check "Token minted under rotated-out kid 'alpha' still verifies during overlap" ok \
    || check "alpha verify" fail "$mint_alpha"

# 6) Token minted under beta (the new active kid) verifies.
mint_beta=$(docker exec georag-fastapi python3 -c "
import sys
sys.path.insert(0, '/app')
from app.services.flow_jwt import (
    mint_flow_jwt, verify_flow_jwt_token, invalidate_per_flow_key_cache,
)
invalidate_per_flow_key_cache()
t = mint_flow_jwt('$FLOW', ttl_seconds=120)
try:
    claims = verify_flow_jwt_token(t, '$FLOW')
    # Confirm it was beta (the mint kid)
    import jwt as _jwt
    h = _jwt.get_unverified_header(t)
    print('OK', h.get('kid'))
except Exception as e:
    print('FAIL', e)
" 2>&1 | tail -1)
case "$mint_beta" in
    'OK beta') check "Fresh mint signs with new kid 'beta' + verify passes" ok ;;
    *) check "beta mint" fail "$mint_beta" ;;
esac

# 7) Rotate with overlap=0 — the prior kid (beta here) should be cut
#    off immediately. A token signed with beta's secret should now
#    REJECT, since beta is no longer in the valid set.
beta_secret=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT set_config('app.audit_encryption_key',
        '$(grep '^AUDIT_ENCRYPTION_KEY=' "$ENVFILE" | cut -d= -f2-)', false);
    SELECT encode(pgp_sym_decrypt(ciphertext,
        '$(grep '^AUDIT_ENCRYPTION_KEY=' "$ENVFILE" | cut -d= -f2-)')::bytea, 'escape')
      FROM workflow.flow_jwt_keys WHERE flow_name = '$FLOW' AND kid = 'beta';
" 2>&1 | tail -1 | tr -d ' ')
bash /home/georag/projects/georag/scripts/phase3_jwt_rotate.sh \
    provision-key "$FLOW" gamma 0 >/dev/null
reject=$(docker exec -e BETA_SECRET="$beta_secret" georag-fastapi python3 -c "
import os, sys, jwt, time
sys.path.insert(0, '/app')
from app.services.flow_jwt import (
    verify_flow_jwt_token, invalidate_per_flow_key_cache,
    ALGORITHM, ISSUER, AUDIENCE,
)
invalidate_per_flow_key_cache()
secret = os.environ.get('BETA_SECRET', '')
now = int(time.time())
t = jwt.encode(
    {'iss': ISSUER, 'aud': AUDIENCE, 'sub': 'kestra',
     'scope': 'flow:$FLOW', 'iat': now, 'exp': now + 120},
    secret, algorithm=ALGORITHM, headers={'kid': 'beta'},
)
try:
    verify_flow_jwt_token(t, '$FLOW')
    print('UNEXPECTED_PASS')
except Exception as e:
    print('REJECTED', type(e).__name__)
" 2>&1 | tail -1)
case "$reject" in
    REJECTED*) check "After overlap=0 rotation, prior kid 'beta' rejects" ok ;;
    *)         check "no-overlap rotation" fail "$reject" ;;
esac

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
