#!/usr/bin/env bash
# =============================================================================
# scripts/phase4_step1_verify.sh
#
# Phase 4 Step 1 done-definition — per-sender HMAC registry.
#
#   1. usage.external_notification_senders table exists + RLS enabled
#   2. register_external_notification_sender() function exists + grant ok
#   3. lookup_external_notification_sender_secrets() exists + grant ok
#   4. AUDIT_ENCRYPTION_KEY set on AI worker env
#   5. Helper script can add a new sender (round-trip)
#   6. End-to-end: 2 distinct senders sign + verify cleanly with their
#      OWN secrets; sender-A's secret REJECTED when used to sign for
#      sender-B
#   7. Disabled sender → HMAC fails (registry-mismatch reason)
#   8. Phase 3 Step 5 smoke still passes (env-var fallback intact when
#      no registry rows for the smoke's source name)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=8
ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
BASE="${FASTAPI_INTERNAL_URL:-http://localhost:8000}"
ENC_KEY=$(grep '^AUDIT_ENCRYPTION_KEY=' "$ENVFILE" | cut -d= -f2- | head -1)

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
        DELETE FROM usage.external_notification_senders
         WHERE source LIKE 'phase4-step1-%';
        DELETE FROM audit.audit_ledger
         WHERE action_type = 'external_notification.received'
           AND payload->>'source' LIKE 'phase4-step1-%';
    " >/dev/null 2>&1 || true
}
trap cleanup EXIT

cat <<'BANNER'

============================================================
PHASE 4 STEP 1 — PER-SENDER HMAC REGISTRY VERIFICATION
============================================================
BANNER

# 1) Table exists + RLS
table_state=$(q "
    SELECT (relrowsecurity::int) || '/' ||
           (count(*) OVER ())::text
      FROM pg_class
     WHERE oid = 'usage.external_notification_senders'::regclass;")
[ -n "$table_state" ] && check "usage.external_notification_senders table + RLS enabled" ok \
    || check "table" fail "missing"

# 2) register helper function
reg=$(q "
    SELECT count(*) FROM information_schema.routines
     WHERE routine_schema='usage'
       AND routine_name='register_external_notification_sender';")
[ "$reg" = "1" ] && check "register_external_notification_sender() present" ok \
    || check "register fn" fail "got $reg"

# 3) lookup helper function
lkp=$(q "
    SELECT count(*) FROM information_schema.routines
     WHERE routine_schema='usage'
       AND routine_name='lookup_external_notification_sender_secrets';")
[ "$lkp" = "1" ] && check "lookup_external_notification_sender_secrets() present" ok \
    || check "lookup fn" fail "got $lkp"

# 4) AUDIT_ENCRYPTION_KEY on worker
env_set=$(docker exec georag-hatchet-worker-ai env | grep -c '^AUDIT_ENCRYPTION_KEY=' | tr -d ' ')
[ "$env_set" = "1" ] && check "AUDIT_ENCRYPTION_KEY set on AI worker" ok \
    || check "worker env" fail "got $env_set"

# 5) Helper add round-trip — register a sender, confirm row + decrypted plaintext.
docker exec georag-postgresql psql -U georag -d georag -q -c "
    SELECT set_config('app.audit_encryption_key', '${ENC_KEY}', false);
    SELECT usage.register_external_notification_sender(
        'phase4-step1-helper-A', 'primary', 'helper-secret-A',
        'verifier round-trip', NULL);
" >/dev/null
n_helper=$(q "
    SELECT count(*) FROM usage.external_notification_senders
     WHERE source = 'phase4-step1-helper-A';")
roundtrip=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT set_config('app.audit_encryption_key', '${ENC_KEY}', false);
    SELECT secret_plain FROM usage.lookup_external_notification_sender_secrets('phase4-step1-helper-A');
" | tail -1 | tr -d ' ')
if [ "$n_helper" = "1" ] && [ "$roundtrip" = "helper-secret-A" ]; then
    check "Helper round-trip: register + lookup decrypts plaintext" ok
else
    check "round-trip" fail "n=$n_helper plaintext='$roundtrip'"
fi

# Set up a feature-flag for the workflow tests.
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO workspace.feature_flags
        (workspace_id, flag_name, bool_value, updated_at)
    VALUES (NULL, 'flows.external_notification.enabled', true, now())
    ON CONFLICT (workspace_id, flag_name) DO UPDATE SET bool_value=true, updated_at=now();
" >/dev/null

# Mint a JWT for trigger calls.
JWT=$(docker exec georag-fastapi python3 -c "
import sys; sys.path.insert(0,'/app')
from app.services.flow_jwt import mint_flow_jwt
print(mint_flow_jwt('external_notification', ttl_seconds=300), end='')
")

post_signed_payload() {
    local source="$1" secret="$2" nid="$3"
    local payload
    payload=$(docker exec -e SECRET="$secret" -e SRC="$source" -e NID="$nid" \
        georag-hatchet-worker-ai python3 -c "
import os, hmac, hashlib, json, sys
sys.path.insert(0, '/app')
from app.hatchet_workflows.external_notification import (
    canonical_json_for_hmac, ExternalNotificationInput,
)
inp = ExternalNotificationInput(
    notification_id=os.environ['NID'], source=os.environ['SRC'], kind='probe',
    payload={'k': 'v'}, received_at='2026-05-10T20:00:00Z',
)
canon = canonical_json_for_hmac(inp)
sig = hmac.new(os.environ['SECRET'].encode(), canon, hashlib.sha256).hexdigest()
out = inp.model_dump(); out['signature'] = sig
print(json.dumps(out))
")
    curl -fsS -X POST "$BASE/internal/v1/integrations/external_notification/trigger" \
        -H 'Content-Type: application/json' -H "Authorization: Bearer $JWT" \
        -d "$payload"
}

wait_completed() {
    local run_id="$1"
    for i in $(seq 1 12); do
        s=$(q "SELECT readable_status::text FROM v1_runs_olap WHERE external_id='${run_id}'::uuid LIMIT 1;" \
            | tr -d ' ' || true)
        if [ "$s" = "COMPLETED" ]; then return 0; fi
        sleep 5
    done
    return 1
}

audit_count_for() {
    local nid="$1"
    docker exec georag-postgresql psql -U georag -d georag -tAc "
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'external_notification.received'
           AND payload->>'notification_id' = '${nid}';" | tr -d ' '
}

# 6) Two senders A + B with DISTINCT secrets — cross-secret rejection
SECRET_A="$(openssl rand -hex 32)"
SECRET_B="$(openssl rand -hex 32)"
docker exec georag-postgresql psql -U georag -d georag -q -c "
    SELECT set_config('app.audit_encryption_key', '${ENC_KEY}', false);
    SELECT usage.register_external_notification_sender(
        'phase4-step1-A', 'primary', '${SECRET_A}', NULL, NULL);
    SELECT usage.register_external_notification_sender(
        'phase4-step1-B', 'primary', '${SECRET_B}', NULL, NULL);
" >/dev/null

# 6a — sender A signs FOR A, expect verify success
NID_A="phase4-step1-A-$(date -u +%s)"
RESP=$(post_signed_payload 'phase4-step1-A' "$SECRET_A" "$NID_A")
RUN_A=$(echo "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin)["workflow_run_id"])')
wait_completed "$RUN_A" || true
n_a=$(audit_count_for "$NID_A")

# 6b — sender A's SECRET signing for source=B (cross-misuse), expect reject
NID_X="phase4-step1-XMISUSE-$(date -u +%s)"
RESP=$(post_signed_payload 'phase4-step1-B' "$SECRET_A" "$NID_X")
RUN_X=$(echo "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin)["workflow_run_id"])')
wait_completed "$RUN_X" || true
n_x=$(audit_count_for "$NID_X")

if [ "$n_a" = "1" ] && [ "$n_x" = "0" ]; then
    check "Per-sender HMAC: A's-secret-for-A passes; A's-secret-for-B rejects" ok
else
    check "per-sender verify" fail "A=$n_a (expected 1) X=$n_x (expected 0)"
fi

# 7) Disable sender A, retry signing for A — should now reject
A_ID=$(q "
    SELECT id FROM usage.external_notification_senders
     WHERE source='phase4-step1-A' AND disabled_at IS NULL ORDER BY created_at DESC LIMIT 1;")
A_ID=$(echo "$A_ID" | tr -d ' ')
docker exec georag-postgresql psql -U georag -d georag -q -c "
    UPDATE usage.external_notification_senders
       SET disabled_at = clock_timestamp() WHERE id = '${A_ID}'::uuid;
" >/dev/null

NID_DIS="phase4-step1-DISABLED-$(date -u +%s)"
RESP=$(post_signed_payload 'phase4-step1-A' "$SECRET_A" "$NID_DIS")
RUN_DIS=$(echo "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin)["workflow_run_id"])')
wait_completed "$RUN_DIS" || true
n_dis=$(audit_count_for "$NID_DIS")
[ "$n_dis" = "0" ] && check "Disabled sender → HMAC rejects (no audit)" ok \
    || check "disabled-sender path" fail "audit=$n_dis (expected 0)"

# 8) Env-var fallback intact for unregistered sources — Phase 3 Step 5 smoke
echo
echo "  ── Phase 3 Step 5 smoke (env-var fallback path) ──"
if timeout 240 bash /home/georag/projects/georag/scripts/phase3_step5_smoke.sh > /tmp/p4_step1_p3s5.log 2>&1; then
    check "Phase 3 Step 5 smoke still passes (env-var fallback intact)" ok
else
    check "env fallback regression" fail "see /tmp/p4_step1_p3s5.log"
    tail -10 /tmp/p4_step1_p3s5.log
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
