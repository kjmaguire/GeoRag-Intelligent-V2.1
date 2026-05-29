#!/usr/bin/env bash
# =============================================================================
# scripts/phase8_step2_verify.sh
#
# Phase 8 Step 2 done-definition — admin UI for per-flow JWT keys
# (R-P7-2).
#
#   1. IntegrationsController has a loadFlowJwtKeys() method
#   2. The /admin/integrations response includes a flow_jwt_keys prop
#      with the expected row shape
#   3. The prop reflects live workflow.flow_jwt_keys content (insert
#      a probe row, page sees it; clean up, page no longer sees it)
#   4. Inertia page TSX declares the FlowJwtKeyRow + panel
#   5. Phase 4 Step 5 verifier still passes (regression — same
#      controller, same page, prior Senders panel untouched)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=5
REPO="${REPO:-/home/georag/projects/georag}"
CTRL="$REPO/app/Http/Controllers/Admin/IntegrationsController.php"
TSX="$REPO/resources/js/Pages/Admin/Integrations.tsx"
LARAVEL="${LARAVEL_CONTAINER:-georag-laravel-octane}"

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
        DELETE FROM workflow.flow_jwt_keys WHERE kid LIKE 'p8s2-%';
    " >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

cat <<'BANNER'

============================================================
PHASE 8 STEP 2 — flow_jwt_keys ADMIN UI VERIFICATION
============================================================
BANNER

# 1) Controller method present
if grep -q 'private function loadFlowJwtKeys' "$CTRL"; then
    check "IntegrationsController::loadFlowJwtKeys() defined" ok
else
    check "controller method" fail "loadFlowJwtKeys() missing"
fi

# 2) Prop wired into the Inertia render
if grep -qE "'flow_jwt_keys' => " "$CTRL"; then
    check "flow_jwt_keys prop wired into Inertia::render" ok
else
    check "controller prop" fail "flow_jwt_keys not in Inertia call"
fi

# Seed a probe row so check 3 sees content.
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO workflow.flow_jwt_keys (flow_name, kid, ciphertext, valid_from, valid_until)
    VALUES ('phase2_smoke', 'p8s2-probe', '\x00',
            clock_timestamp() - interval '1 hour', NULL);
" >/dev/null

# 3) Acting-as-admin probe (delegated to a PHP script file — inline
# PHP via -r breaks on shell escaping).
probe=$(docker exec "$LARAVEL" php /app/scripts/_phase8_step2_probe.php 2>&1 | tail -3)
if echo "$probe" | grep -q 'found=p8s2-probe flow=phase2_smoke active=1'; then
    check "flow_jwt_keys prop reflects live DB row" ok
else
    check "live prop" fail "$probe"
fi

# 4) TSX panel
if grep -q 'interface FlowJwtKeyRow' "$TSX" \
    && grep -q 'Per-flow JWT keys' "$TSX"; then
    check "Integrations.tsx declares FlowJwtKeyRow + the panel" ok
else
    check "tsx panel" fail "FlowJwtKeyRow interface or panel header missing"
fi

# 5) Regression — Phase 4 Step 5 verifier still green
p4s5=$(bash "$REPO/scripts/phase4_step5_verify.sh" 2>&1 | grep -E '^Result: ' | tail -1)
case "$p4s5" in
    'Result: 5 / 5 checks passed')
        check "Phase 4 Step 5 verifier still passes 5/5 (no regression)" ok ;;
    *) check "phase4_step5 regression" fail "$p4s5" ;;
esac

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
