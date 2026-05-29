#!/usr/bin/env bash
# =============================================================================
# scripts/phase10_step3_verify.sh
#
# Phase 10 Step 3 done-definition — sender registration form
# (admin UI).
#
#   1. IntegrationsController::registerSender() defined
#   2. POST /admin/integrations/senders route registered
#   3. Integrations.tsx has the RegisterSenderForm component +
#      one-shot secret banner
#   4. Admin probe: row lands in usage.external_notification_senders
#   5. Admin probe: flash bag carries the freshly-generated 64-char
#      hex secret
#   6. Audit ledger row has action_type='usage.external_notification_sender.registered'
#      AND its payload contains NO 64-char hex secret
#   7. Non-admin probe → AuthorizationException
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
REPO="${REPO:-/home/georag/projects/georag}"
CTRL="$REPO/app/Http/Controllers/Admin/IntegrationsController.php"
ROUTES="$REPO/routes/web.php"
TSX="$REPO/resources/js/Pages/Admin/Integrations.tsx"
LARAVEL="${LARAVEL_CONTAINER:-georag-laravel-octane}"
SRC="phase10-step3-$(date +%s)"

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
        DELETE FROM usage.external_notification_senders WHERE source LIKE 'phase10-step3-%';
        DELETE FROM audit.audit_ledger
         WHERE action_type = 'usage.external_notification_sender.registered'
           AND payload->>'source' LIKE 'phase10-step3-%'
           AND created_at > now() - interval '15 minutes';
    " >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

cat <<'BANNER'

============================================================
PHASE 10 STEP 3 — SENDER REGISTRATION UI VERIFICATION
============================================================
BANNER

# 1) Controller method
if grep -q 'public function registerSender' "$CTRL"; then
    check "IntegrationsController::registerSender() defined" ok
else
    check "controller" fail "registerSender missing"
fi

# 2) Route registered
if grep -q 'admin.integrations.senders.register' "$ROUTES"; then
    check "POST /admin/integrations/senders route registered" ok
else
    check "route" fail "missing"
fi

# 3) Inertia component + banner
if grep -q 'function RegisterSenderForm' "$TSX" \
    && grep -q 'Copy this secret now' "$TSX"; then
    check "RegisterSenderForm + one-shot secret banner in Integrations.tsx" ok
else
    check "tsx form" fail "component or banner missing"
fi

# 4-5) Acting-as-admin probe
admin_out=$(docker exec "$LARAVEL" php /app/scripts/_phase10_step3_probe.php admin "$SRC" 2>&1 | tail -5)
echo "    admin probe: $(echo "$admin_out" | tr '\n' '|')"

sender_count=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM usage.external_notification_senders
     WHERE source = '$SRC';" | tr -d ' ')
[ "$sender_count" = "1" ] \
    && check "Sender row landed in usage.external_notification_senders" ok \
    || check "sender row" fail "got count=$sender_count"

if echo "$admin_out" | grep -q 'SECRET_LEN=64'; then
    check "Flash bag carries the 64-char hex secret" ok
else
    check "flash secret" fail "$admin_out"
fi

# 6) Audit ledger row + no secret leak
audit_row=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT payload::text
      FROM audit.audit_ledger
     WHERE action_type = 'usage.external_notification_sender.registered'
       AND payload->>'source' = '$SRC'
     ORDER BY created_at DESC
     LIMIT 1;")
if [ -n "$audit_row" ]; then
    if echo "$audit_row" | grep -qE '[0-9a-f]{64}'; then
        check "audit ledger has NO 64-char hex secret leak" fail "secret leaked into payload"
    else
        check "Audit row inserted + payload has NO secret leak" ok
    fi
else
    check "audit row" fail "no row found"
fi

# 7) Non-admin denied
nonadmin_out=$(docker exec "$LARAVEL" php /app/scripts/_phase10_step3_probe.php nonadmin "${SRC}-no" 2>&1 | tail -3)
case "$nonadmin_out" in
    *AUTH_DENIED*) check "Non-admin register → AuthorizationException" ok ;;
    *) check "nonadmin gate" fail "$nonadmin_out" ;;
esac

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
