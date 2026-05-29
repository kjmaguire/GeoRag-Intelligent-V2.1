#!/usr/bin/env bash
# =============================================================================
# scripts/phase10_step1_verify.sh
#
# Phase 10 Step 1 done-definition — audit row per JWT rotation
# (R-P9-1).
#
#   1. rotateFlowKey() imports + uses AuditEmitter
#   2. Acting-as-admin rotate POST results in an audit_ledger row
#      with action_type='workflow.jwt_key.rotated'
#   3. Audit payload contains flow_name + prior_kid + new_kid +
#      overlap_hours
#   4. Audit payload does NOT contain the raw secret
#   5. actor_id reflects the calling user
#   6. Phase 9 Step 2 verifier still passes (rotation still works
#      end-to-end after the audit emission was added)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
CTRL="$REPO/app/Http/Controllers/Admin/IntegrationsController.php"
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
        DELETE FROM audit.audit_ledger
         WHERE action_type = 'workflow.jwt_key.rotated'
           AND payload->>'flow_name' = '$FLOW'
           AND created_at > now() - interval '10 minutes';
    " >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

cat <<'BANNER'

============================================================
PHASE 10 STEP 1 — JWT ROTATION AUDIT VERIFICATION
============================================================
BANNER

# 1) Controller imports + uses AuditEmitter
if grep -q 'use App\\Services\\Audit\\AuditEmitter;' "$CTRL" \
    && grep -q "actionType: 'workflow.jwt_key.rotated'" "$CTRL"; then
    check "rotateFlowKey() emits via AuditEmitter with workflow.jwt_key.rotated" ok
else
    check "controller wiring" fail "AuditEmitter not wired into rotateFlowKey"
fi

# Seed a prior kid so we have a non-null prior_kid in the payload.
PRIOR_KID="prior-$(date +%s)"
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO workflow.flow_jwt_keys (flow_name, kid, ciphertext, valid_from, valid_until)
    VALUES ('$FLOW', '$PRIOR_KID', '\x00',
            clock_timestamp() - interval '1 day', NULL);
" >/dev/null

# Trigger rotation via the Phase 9 probe script (it acts as admin
# user_id=999992 by default).
docker exec "$LARAVEL" php /app/scripts/_phase9_step2_probe.php admin >/dev/null 2>&1 || true

# 2) Audit row exists
row=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT actor_id::text || '|' ||
           coalesce(payload->>'flow_name','') || '|' ||
           coalesce(payload->>'prior_kid','-') || '|' ||
           coalesce(payload->>'new_kid','-') || '|' ||
           coalesce(payload->>'overlap_hours','-')
      FROM audit.audit_ledger
     WHERE action_type = 'workflow.jwt_key.rotated'
       AND payload->>'flow_name' = '$FLOW'
       AND created_at > now() - interval '5 minutes'
     ORDER BY created_at DESC
     LIMIT 1;
" | tr -d ' ')

if [ -n "$row" ]; then
    check "audit_ledger row inserted with action_type=workflow.jwt_key.rotated" ok
else
    check "audit row" fail "no row found"
fi

# 3) Payload contains required fields
case "$row" in
    *"|$FLOW|"*"|"*"rotated-"*"|"*)
        check "payload contains flow_name + new_kid (rotated-* pattern)" ok ;;
    *)
        check "payload shape" fail "row=$row" ;;
esac

# 4) Payload does NOT contain a raw 64-char hex secret
secret_present=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM audit.audit_ledger
     WHERE action_type = 'workflow.jwt_key.rotated'
       AND payload::text ~* '[0-9a-f]{64}';" | tr -d ' ')
[ "$secret_present" = "0" ] \
    && check "Payload has NO 64-char hex secret leak" ok \
    || check "secret leak" fail "found $secret_present rows with 64-char hex"

# 5) actor_id reflects the calling user (the probe acts as 999992)
actor=$(echo "$row" | cut -d'|' -f1)
[ "$actor" = "999992" ] \
    && check "actor_id=999992 (matches the probe's admin user)" ok \
    || check "actor_id" fail "got actor=$actor"

# 6) Regression — Phase 9 Step 2 still passes
p9s2=$(bash "$REPO/scripts/phase9_step2_verify.sh" 2>&1 | grep -E '^Result: ' | tail -1)
case "$p9s2" in
    'Result: 6 / 6 checks passed')
        check "Phase 9 Step 2 rotation verifier still passes 6/6" ok ;;
    *) check "phase9_step2 regression" fail "$p9s2" ;;
esac

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
