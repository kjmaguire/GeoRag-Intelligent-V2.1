#!/usr/bin/env bash
# =============================================================================
# scripts/phase5_step1_verify.sh
#
# Phase 5 Step 1 done-definition — per-sender rate limit (R-P4-1).
#
#   1. rate_limit_per_minute column present on senders table + check constraint
#   2. check_rate_limit() helper importable + returns (allowed, count)
#   3. Below-limit POSTs succeed (audit rows land)
#   4. Above-limit POSTs short-circuit with `reason='rate_limited:...'` and
#      DO NOT write audit rows
#   5. Sender-specific: A's limit doesn't affect B
#   6. Unregistered source (env-var fallback path) is NOT rate-limited
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
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

cleanup() {
    docker exec georag-postgresql psql -U georag -d georag -q -c "
        DELETE FROM usage.external_notification_senders
         WHERE source LIKE 'phase5-step1-%';
        DELETE FROM audit.audit_ledger
         WHERE action_type = 'external_notification.received'
           AND payload->>'source' LIKE 'phase5-step1-%';
    " >/dev/null 2>&1 || true
    # Wipe rate-limit buckets so reruns don't leak counts.
    docker exec georag-redis redis-cli -a "$(grep '^REDIS_PASSWORD=' "$ENVFILE" | cut -d= -f2-)" --no-auth-warning \
        --scan --pattern 'rl:external_notification:phase5-step1-*' 2>/dev/null \
        | xargs -r -n1 docker exec georag-redis redis-cli -a "$(grep '^REDIS_PASSWORD=' "$ENVFILE" | cut -d= -f2-)" --no-auth-warning DEL >/dev/null 2>&1 || true
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
PHASE 5 STEP 1 — PER-SENDER RATE LIMIT VERIFICATION
============================================================
BANNER

# 1) Column + constraint present
col_check=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM information_schema.columns
     WHERE table_schema='usage' AND table_name='external_notification_senders'
       AND column_name='rate_limit_per_minute';" | tr -d ' ')
constr_check=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM pg_constraint
     WHERE conname = 'external_notification_senders_rate_limit_check';" | tr -d ' ')
if [ "$col_check" = "1" ] && [ "$constr_check" = "1" ]; then
    check "rate_limit_per_minute column + range check present" ok
else
    check "schema" fail "col=$col_check constraint=$constr_check"
fi

# 2) Helper importable + returns expected shape
helper=$(docker exec georag-hatchet-worker-ai python3 -c "
import asyncio, sys
sys.path.insert(0, '/app')
from app.hatchet_workflows.external_notification import check_rate_limit
result = asyncio.run(check_rate_limit('phase5-step1-helper-probe', 3))
print('shape:', type(result).__name__, 'allowed:', result[0], 'count:', result[1])
" 2>&1 | tail -1)
case "$helper" in
    *allowed:*True*count:*) check "check_rate_limit() returns (allowed, count) tuple" ok ;;
    *) check "helper" fail "$helper" ;;
esac

# Seed senders A + B with HMAC secrets + small rate limit.
SECRET_A="$(openssl rand -hex 32)"
SECRET_B="$(openssl rand -hex 32)"
LIMIT_A=3
LIMIT_B=10
docker exec georag-postgresql psql -U georag -d georag -q -c "
    SELECT set_config('app.audit_encryption_key', '${ENC_KEY}', false);
    SELECT usage.register_external_notification_sender(
        'phase5-step1-A', 'primary', '${SECRET_A}', NULL, NULL);
    SELECT usage.register_external_notification_sender(
        'phase5-step1-B', 'primary', '${SECRET_B}', NULL, NULL);
    UPDATE usage.external_notification_senders
       SET rate_limit_per_minute = ${LIMIT_A} WHERE source = 'phase5-step1-A';
    UPDATE usage.external_notification_senders
       SET rate_limit_per_minute = ${LIMIT_B} WHERE source = 'phase5-step1-B';
" >/dev/null

# Ensure flag enabled.
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO workspace.feature_flags
        (workspace_id, flag_name, bool_value, updated_at)
    VALUES (NULL, 'flows.external_notification.enabled', true, now())
    ON CONFLICT (workspace_id, flag_name) DO UPDATE SET bool_value=true, updated_at=now();
" >/dev/null

# Mint JWT.
JWT=$(docker exec georag-fastapi python3 -c "
import sys; sys.path.insert(0,'/app')
from app.services.flow_jwt import mint_flow_jwt
print(mint_flow_jwt('external_notification', ttl_seconds=600), end='')
")

post_signed() {
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
    for _ in $(seq 1 8); do
        s=$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc \
            "SELECT readable_status::text FROM v1_runs_olap WHERE external_id='${run_id}'::uuid LIMIT 1;" \
            2>/dev/null | tr -d ' ')
        [ "$s" = "COMPLETED" ] && return 0
        sleep 3
    done
    return 1
}

audit_count_for() {
    docker exec georag-postgresql psql -U georag -d georag -tAc "
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'external_notification.received'
           AND payload->>'notification_id' = '$1';" | tr -d ' '
}

# 3+4) Phase 10 Step 2 (R-P9-3) — rate-limit checks were flaky when
# sequential `wait_completed` calls pushed test execution across a
# UTC-minute boundary (the rate limiter is a fixed minute window;
# bucket rollover gave the 4th send a fresh count=1). Fix: align to
# the start of a fresh minute, then POST all 4 sender-A requests in
# a tight burst BEFORE waiting on any of them. The 4 INCRs land
# inside one bucket; the wait_completeds happen afterwards.
sec_remaining=$((60 - $(date -u +%-S)))
if [ "$sec_remaining" -lt 15 ]; then
    # Less than 15s left in the current minute — sleep to the top.
    sleep "$sec_remaining"
fi
TS=$(date -u +%s)
declare -a A_NIDS=(
    "phase5-step1-A-ok-${TS}-1"
    "phase5-step1-A-ok-${TS}-2"
    "phase5-step1-A-ok-${TS}-3"
)
NID_OVER="phase5-step1-A-over-${TS}"
declare -a A_RUNS=()
for nid in "${A_NIDS[@]}" "$NID_OVER"; do
    RESP=$(post_signed 'phase5-step1-A' "$SECRET_A" "$nid")
    A_RUNS+=("$(echo "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin)["workflow_run_id"])')")
done
# All 4 posted within the same minute window; now wait for each to
# settle so the audit rows (if any) are visible.
for run in "${A_RUNS[@]}"; do
    wait_completed "$run" || true
done

ok_count=0
for nid in "${A_NIDS[@]}"; do
    [ "$(audit_count_for "$nid")" = "1" ] && ok_count=$((ok_count + 1))
done
[ "$ok_count" = "3" ] \
    && check "Below limit (3/3 sender-A audited)" ok \
    || check "below-limit" fail "got $ok_count / 3 audited"

over_audit=$(audit_count_for "$NID_OVER")
[ "$over_audit" = "0" ] \
    && check "Above limit → rejected, no audit row" ok \
    || check "above-limit" fail "audit=$over_audit (expected 0)"

# 5) Sender-specific: sender B still passes (limit=10, fresh bucket).
NID_B="phase5-step1-B-ok-${TS}"
RESP=$(post_signed 'phase5-step1-B' "$SECRET_B" "$NID_B")
RUN=$(echo "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin)["workflow_run_id"])')
wait_completed "$RUN" || true
b_audit=$(audit_count_for "$NID_B")
[ "$b_audit" = "1" ] \
    && check "Sender-specific: B's bucket unaffected by A's limit" ok \
    || check "per-sender isolation" fail "B audit=$b_audit"

# 6) Unregistered source: env-var fallback path should NOT be rate-limited.
# Use the Phase 3 Step 5 smoke fixture name (no registry entry).
JWT_SMOKE=$(docker exec georag-fastapi python3 -c "
import sys; sys.path.insert(0,'/app')
from app.services.flow_jwt import mint_flow_jwt
print(mint_flow_jwt('external_notification', ttl_seconds=600), end='')
")
HMAC_SECRET=$(grep '^EXTERNAL_NOTIFICATION_HMAC_SECRET=' "$ENVFILE" | cut -d= -f2- | head -1)
unreg_count=0
for i in 1 2; do
    NID="phase5-step1-unregistered-${TS}-${i}"
    PAYLOAD=$(docker exec -e SECRET="$HMAC_SECRET" -e SRC='phase5-step1-unregistered' -e NID="$NID" \
        georag-hatchet-worker-ai python3 -c "
import os, hmac, hashlib, json, sys
sys.path.insert(0, '/app')
from app.hatchet_workflows.external_notification import (
    canonical_json_for_hmac, ExternalNotificationInput,
)
inp = ExternalNotificationInput(
    notification_id=os.environ['NID'], source=os.environ['SRC'], kind='probe',
    payload={}, received_at='2026-05-10T20:00:00Z',
)
canon = canonical_json_for_hmac(inp)
sig = hmac.new(os.environ['SECRET'].encode(), canon, hashlib.sha256).hexdigest()
out = inp.model_dump(); out['signature'] = sig
print(json.dumps(out))
")
    RESP=$(curl -fsS -X POST "$BASE/internal/v1/integrations/external_notification/trigger" \
        -H 'Content-Type: application/json' -H "Authorization: Bearer $JWT_SMOKE" -d "$PAYLOAD")
    RUN=$(echo "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin)["workflow_run_id"])')
    wait_completed "$RUN" || true
    [ "$(audit_count_for "$NID")" = "1" ] && unreg_count=$((unreg_count + 1))
done
[ "$unreg_count" = "2" ] \
    && check "Unregistered source: env-fallback path NOT rate-limited" ok \
    || check "env-fallback bypass" fail "got $unreg_count / 2 audited"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
