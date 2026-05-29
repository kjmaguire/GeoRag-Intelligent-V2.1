#!/usr/bin/env bash
# =============================================================================
# scripts/phase14_step2_verify.sh
#
# Phase 14 Step 2 — HMAC rotation overlap window (R-P12-l6-overlap-hmac).
#
#   1. Controller accepts overlap_hours form field
#   2. Default (no overlap_hours field) still cuts immediately —
#      matches Phase 12 Step 4 behaviour
#   3. With overlap_hours=24, prior sender's disabled_at lands ~24h
#      in the future
#   4. Audit payload records overlap_hours
#   5. overlap_hours > 168 (the cap) gets rejected by Laravel validation
#   6. Phase 12 Step 4 verifier still passes (regression)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
CTRL="$REPO/app/Http/Controllers/Admin/IntegrationsController.php"
LARAVEL="${LARAVEL_CONTAINER:-georag-laravel-octane}"
ENC_KEY=$(grep '^AUDIT_ENCRYPTION_KEY=' "$REPO/.env" | cut -d= -f2- | head -1)
SOURCE_PREFIX="phase14-step2-$(date +%s)"

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
         WHERE source LIKE 'phase14-step2-%';
        DELETE FROM audit.audit_ledger
         WHERE action_type = 'usage.external_notification_sender.hmac_rotated'
           AND payload->>'source' LIKE 'phase14-step2-%'
           AND created_at > now() - interval '15 minutes';
    " >/dev/null 2>&1 || true
}
trap cleanup EXIT
cleanup

cat <<'BANNER'

============================================================
PHASE 14 STEP 2 — HMAC ROTATION OVERLAP VERIFICATION
============================================================
BANNER

# 1) Controller has the overlap_hours validation block
if grep -q "'overlap_hours' => \['sometimes', 'integer'" "$CTRL"; then
    check "Controller validates overlap_hours form field" ok
else
    check "validation block" fail "missing"
fi

# Seed a sender for the immediate-cut probe
sender_id_a=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT set_config('app.audit_encryption_key', '$ENC_KEY', false);
    SELECT usage.register_external_notification_sender(
        '${SOURCE_PREFIX}-default', 'primary',
        '$(openssl rand -hex 32)', 'phase14 immediate-cut probe', NULL
    )::text;" | tail -1 | tr -d ' ')

# 2) Default overlap=0 → immediate cut (disabled_at ≤ now())
docker exec "$LARAVEL" php /app/scripts/_phase14_step2_probe.php "$sender_id_a" 0 >/dev/null 2>&1
default_delta=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT round(EXTRACT(EPOCH FROM (disabled_at - clock_timestamp())))
      FROM usage.external_notification_senders
     WHERE id = '$sender_id_a'::uuid;" | tr -d ' ')
if [ -n "$default_delta" ] && [ "$default_delta" -le 5 ] 2>/dev/null; then
    check "Default overlap=0 cuts immediately (delta=${default_delta}s)" ok
else
    check "default cut" fail "got delta=$default_delta"
fi

# 3) overlap_hours=24 → disabled_at ≈ +24h
sender_id_b=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT set_config('app.audit_encryption_key', '$ENC_KEY', false);
    SELECT usage.register_external_notification_sender(
        '${SOURCE_PREFIX}-overlap', 'primary',
        '$(openssl rand -hex 32)', 'phase14 overlap probe', NULL
    )::text;" | tail -1 | tr -d ' ')

docker exec "$LARAVEL" php /app/scripts/_phase14_step2_probe.php "$sender_id_b" 24 >/dev/null 2>&1
overlap_hrs=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT round(EXTRACT(EPOCH FROM (disabled_at - clock_timestamp())) / 3600.0)
      FROM usage.external_notification_senders
     WHERE id = '$sender_id_b'::uuid;" | tr -d ' ')
if [ "$overlap_hrs" = "24" ]; then
    check "overlap_hours=24 → prior disabled_at ≈ now()+24h" ok
else
    check "overlap window" fail "got delta=${overlap_hrs}h"
fi

# 4) Audit payload records overlap_hours
audit_overlap=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT payload->>'overlap_hours'
      FROM audit.audit_ledger
     WHERE action_type = 'usage.external_notification_sender.hmac_rotated'
       AND payload->>'source' = '${SOURCE_PREFIX}-overlap'
     ORDER BY created_at DESC LIMIT 1;" | tr -d ' ')
[ "$audit_overlap" = "24" ] \
    && check "Audit payload records overlap_hours=24" ok \
    || check "audit overlap" fail "got '$audit_overlap'"

# 5) overlap_hours > 168 rejected
sender_id_c=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT set_config('app.audit_encryption_key', '$ENC_KEY', false);
    SELECT usage.register_external_notification_sender(
        '${SOURCE_PREFIX}-toobig', 'primary',
        '$(openssl rand -hex 32)', 'phase14 too-big probe', NULL
    )::text;" | tail -1 | tr -d ' ')
bigout=$(docker exec "$LARAVEL" php /app/scripts/_phase14_step2_probe.php "$sender_id_c" 9999 2>&1 | tail -3)
if echo "$bigout" | grep -qE 'ERR.*ValidationException|ERR.*Illuminate.*Validation'; then
    check "overlap_hours>168 rejected by validation" ok
else
    check "validation cap" fail "$(echo "$bigout" | tr '\n' '|')"
fi

# 6) Phase 12 Step 4 regression
p12s4=$(bash "$REPO/scripts/phase12_step4_verify.sh" 2>&1 | grep -E '^Result: ' | tail -1)
case "$p12s4" in
    'Result: 8 / 8 checks passed')
        check "Phase 12 Step 4 still passes 8/8 (no regression)" ok ;;
    *) check "phase12_step4 regression" fail "$p12s4" ;;
esac

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
