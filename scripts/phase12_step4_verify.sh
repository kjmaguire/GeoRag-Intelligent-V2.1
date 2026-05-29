#!/usr/bin/env bash
# =============================================================================
# scripts/phase12_step4_verify.sh
#
# Phase 12 Step 4 done-definition — sender HMAC rotate + rotation
# history panel (R-P10-1 + R-P10-2).
#
#   1. IntegrationsController has rotateSenderHmac() + loadRotationHistory()
#   2. POST /admin/integrations/senders/{id}/rotate-hmac route registered
#   3. Integrations.tsx declares RotationHistoryRow + the Rotation history panel
#   4. SenderRowView has a Rotate HMAC button (active senders only)
#   5. Live admin probe: rotation creates new sender row + disables prior
#   6. Audit ledger emits 'usage.external_notification_sender.hmac_rotated'
#      WITHOUT the secret in the payload
#   7. rotation_history prop reflects the new event
#   8. Non-admin POST → AuthorizationException
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=8
REPO="${REPO:-/home/georag/projects/georag}"
CTRL="$REPO/app/Http/Controllers/Admin/IntegrationsController.php"
ROUTES="$REPO/routes/web.php"
TSX="$REPO/resources/js/Pages/Admin/Integrations.tsx"
LARAVEL="${LARAVEL_CONTAINER:-georag-laravel-octane}"
ENC_KEY=$(grep '^AUDIT_ENCRYPTION_KEY=' "$REPO/.env" | cut -d= -f2- | head -1)

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
        DELETE FROM usage.external_notification_senders
         WHERE source LIKE 'phase12-step4-%';
        DELETE FROM audit.audit_ledger
         WHERE action_type IN (
             'usage.external_notification_sender.registered',
             'usage.external_notification_sender.hmac_rotated'
         )
           AND payload->>'source' LIKE 'phase12-step4-%'
           AND created_at > now() - interval '15 minutes';
    " >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

cat <<'BANNER'

============================================================
PHASE 12 STEP 4 — SENDER HMAC ROTATE + HISTORY PANEL
============================================================
BANNER

# 1) Controller methods present
if grep -q 'public function rotateSenderHmac' "$CTRL" \
    && grep -q 'private function loadRotationHistory' "$CTRL"; then
    check "Controller has rotateSenderHmac() + loadRotationHistory()" ok
else
    check "controller" fail "methods missing"
fi

# 2) Route registered
if grep -q 'admin.integrations.senders.rotate-hmac' "$ROUTES"; then
    check "rotate-hmac route registered" ok
else
    check "route" fail "missing"
fi

# 3) TSX panel + interface
if grep -q 'interface RotationHistoryRow' "$TSX" \
    && grep -q 'Rotation history' "$TSX"; then
    check "Integrations.tsx declares RotationHistoryRow + the panel" ok
else
    check "tsx panel" fail "interface or panel missing"
fi

# 4) Rotate HMAC button in SenderRowView
if grep -q 'Rotate HMAC' "$TSX" \
    && grep -q '/rotate-hmac' "$TSX"; then
    check "SenderRowView has a Rotate HMAC button" ok
else
    check "rotate button" fail "missing"
fi

# Seed a sender that we can rotate.
SOURCE="phase12-step4-rotate-$(date +%s)"
sender_id=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT set_config('app.audit_encryption_key', '$ENC_KEY', false);
    SELECT usage.register_external_notification_sender(
        '$SOURCE', 'primary',
        '$(openssl rand -hex 32)',
        'phase12 step4 rotate probe seed',
        NULL
    )::text;
" 2>&1 | tail -1 | tr -d ' ')

if [ -z "$sender_id" ] || ! [[ "$sender_id" =~ ^[0-9a-f-]{36}$ ]]; then
    check "seed sender" fail "could not seed; got id=$sender_id"
    # Skip dependent checks
    check "rotation effect" fail "no seed sender"
    check "audit emission" fail "no seed sender"
    check "rotation_history prop" fail "no seed sender"
    check "nonadmin gate" fail "no seed sender"
else
    # 5) Acting-as-admin rotate
    admin_out=$(docker exec "$LARAVEL" php /app/scripts/_phase12_step4_probe.php admin "$sender_id" 2>&1 | tail -5)
    echo "    probe: $(echo "$admin_out" | tr '\n' '|')"

    new_sender=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
        SELECT id::text || '|' || secret_kid
          FROM usage.external_notification_senders
         WHERE source = '$SOURCE' AND disabled_at IS NULL
         LIMIT 1;" | tr -d ' ')
    prior_disabled=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
        SELECT count(*) FROM usage.external_notification_senders
         WHERE id = '$sender_id'::uuid AND disabled_at IS NOT NULL;" | tr -d ' ')
    if [ -n "$new_sender" ] && [[ "$new_sender" == *'|rotated-'* ]] \
        && [ "$prior_disabled" = "1" ]; then
        check "Rotation created new active sender + disabled prior" ok
    else
        check "rotation effect" fail "new=$new_sender prior_disabled=$prior_disabled"
    fi

    # 6) Audit row + no secret leak
    audit_payload=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
        SELECT payload::text FROM audit.audit_ledger
         WHERE action_type = 'usage.external_notification_sender.hmac_rotated'
           AND payload->>'source' = '$SOURCE'
         ORDER BY created_at DESC LIMIT 1;")
    if [ -n "$audit_payload" ]; then
        if echo "$audit_payload" | grep -qE '[0-9a-f]{64}'; then
            check "audit secret leak" fail "64-char hex in payload"
        else
            check "Audit row emits hmac_rotated event with no secret leak" ok
        fi
    else
        check "audit row" fail "no row found"
    fi

    # 7) rotation_history prop reflects the new event
    history_out=$(docker exec "$LARAVEL" php -r "
require '/app/vendor/autoload.php';
\$app = require '/app/bootstrap/app.php';
\$app->make(\Illuminate\Contracts\Console\Kernel::class)->bootstrap();
use App\Http\Controllers\Admin\IntegrationsController;
use App\Models\User;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Auth;
\$u = new User(); \$u->id = 999996; \$u->is_admin = true; Auth::login(\$u);
\$req = Request::create('/admin/integrations', 'GET');
\$req->headers->set('X-Inertia', 'true');
\$resp = (new IntegrationsController())->index(\$req);
\$payload = json_decode(\$resp->toResponse(\$req)->getContent(), true);
\$rh = \$payload['props']['rotation_history'] ?? [];
foreach (\$rh as \$row) {
    if ((\$row['source'] ?? '') === '$SOURCE') {
        echo 'found:' . \$row['action_type'] . PHP_EOL;
        exit;
    }
}
echo 'NOT_FOUND count=' . count(\$rh) . PHP_EOL;
" 2>&1 | tail -2)
    if echo "$history_out" | grep -q 'found:usage.external_notification_sender.hmac_rotated'; then
        check "rotation_history prop reflects the new event" ok
    else
        check "history prop" fail "$history_out"
    fi

    # 8) Non-admin denied
    nonadmin_out=$(docker exec "$LARAVEL" php /app/scripts/_phase12_step4_probe.php nonadmin "$sender_id" 2>&1 | tail -3)
    case "$nonadmin_out" in
        *AUTH_DENIED*) check "Non-admin rotate → AuthorizationException" ok ;;
        *) check "nonadmin gate" fail "$nonadmin_out" ;;
    esac
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
