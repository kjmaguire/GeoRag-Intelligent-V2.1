#!/usr/bin/env bash
# =============================================================================
# scripts/phase9_step2_verify.sh
#
# Phase 9 Step 2 done-definition — rotate-with-overlap button (R-P8-1).
#
#   1. IntegrationsController::rotateFlowKey() method present
#   2. routes/web.php registers admin.integrations.jwt-keys.rotate
#   3. Integrations.tsx has the RotateFlowKeyForm component
#   4. Acting-as-admin POST inserts a new kid in flow_jwt_keys AND
#      the prior active kid's valid_until is set ≈ now + overlap_hours
#   5. Non-admin POST → AuthorizationException
#   6. Phase 8 Step 2 verifier still passes (regression)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
CTRL="$REPO/app/Http/Controllers/Admin/IntegrationsController.php"
ROUTES="$REPO/routes/web.php"
TSX="$REPO/resources/js/Pages/Admin/Integrations.tsx"
LARAVEL="${LARAVEL_CONTAINER:-georag-laravel-octane}"
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
    " >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

cat <<'BANNER'

============================================================
PHASE 9 STEP 2 — ROTATE-WITH-OVERLAP BUTTON VERIFICATION
============================================================
BANNER

# 1) Controller method present
if grep -q 'public function rotateFlowKey' "$CTRL"; then
    check "IntegrationsController::rotateFlowKey() defined" ok
else
    check "controller method" fail "rotateFlowKey missing"
fi

# 2) Route registered
if grep -q 'admin.integrations.jwt-keys.rotate' "$ROUTES"; then
    check "POST /admin/integrations/jwt-keys/rotate route registered" ok
else
    check "route" fail "route name missing from web.php"
fi

# 3) TSX component
if grep -q 'function RotateFlowKeyForm' "$TSX" \
    && grep -q 'Rotate with overlap' "$TSX"; then
    check "RotateFlowKeyForm component declared in Integrations.tsx" ok
else
    check "tsx form" fail "component or button text missing"
fi

# Seed a prior kid so we can see the rotation extend its valid_until.
PRIOR_KID="prior-$(date +%s)"
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO workflow.flow_jwt_keys (flow_name, kid, ciphertext, valid_from, valid_until)
    VALUES ('$FLOW', '$PRIOR_KID', '\x00',
            clock_timestamp() - interval '1 day', NULL);
" >/dev/null

# 4) Admin probe
admin_out=$(docker exec "$LARAVEL" php /app/scripts/_phase9_step2_probe.php admin 2>&1 | tail -5)
if echo "$admin_out" | grep -q 'STATUS=302'; then
    # Inspect the new row + the retired prior kid
    new_kid=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
        SELECT kid FROM workflow.flow_jwt_keys
         WHERE flow_name='$FLOW' AND valid_until IS NULL
         ORDER BY valid_from DESC LIMIT 1;" | tr -d ' ')
    overlap_h=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
        SELECT round(EXTRACT(EPOCH FROM (valid_until - clock_timestamp())) / 3600)
          FROM workflow.flow_jwt_keys
         WHERE flow_name='$FLOW' AND kid='$PRIOR_KID';" | tr -d ' ')
    if [[ "$new_kid" =~ ^rotated- ]] \
        && [ "${overlap_h:-0}" -ge 11 ] 2>/dev/null \
        && [ "${overlap_h:-0}" -le 12 ] 2>/dev/null; then
        check "Admin rotate → new kid '$new_kid' active, prior kid retired in ${overlap_h}h" ok
    else
        check "rotate effect" fail "new_kid=$new_kid overlap_h=$overlap_h"
    fi
else
    check "admin rotate probe" fail "$admin_out"
fi

# 5) Non-admin denied
nonadmin_out=$(docker exec "$LARAVEL" php /app/scripts/_phase9_step2_probe.php nonadmin 2>&1 | tail -3)
case "$nonadmin_out" in
    *AUTH_DENIED*) check "Non-admin rotate → AuthorizationException" ok ;;
    *) check "nonadmin gate" fail "$nonadmin_out" ;;
esac

# 6) Phase 8 Step 2 regression
p8s2=$(bash "$REPO/scripts/phase8_step2_verify.sh" 2>&1 | grep -E '^Result: ' | tail -1)
case "$p8s2" in
    'Result: 5 / 5 checks passed')
        check "Phase 8 Step 2 admin UI still passes 5/5" ok ;;
    *) check "phase8_step2 regression" fail "$p8s2" ;;
esac

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
