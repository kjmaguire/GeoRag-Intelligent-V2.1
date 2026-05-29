#!/usr/bin/env bash
# =============================================================================
# scripts/phase2_step5_verify.sh
#
# Phase 2 Step 5a done-definition — external_notification webhook bridge.
#
#   1. Workflow file imports cleanly + IO models declared
#   2. Hatchet engine knows external_notification (registered)
#   3. AI worker pool advertises it via --list
#   4. integrations registry contains external_notification
#   5. Feature flag activepieces.external_notification.enabled exists
#   6. Flag-disabled path returns skipped=true (workflow gate works)
#   7. End-to-end smoke (delegates) — covers happy path + idempotency
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
KEY=$(awk -F= '/^FASTAPI_SERVICE_KEY=/ { print $2 }' "$ENVFILE" 2>/dev/null | head -1)
BASE="${FASTAPI_INTERNAL_URL:-http://localhost:8000}"

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

cat <<'BANNER'

============================================================
PHASE 2 STEP 5a — external_notification VERIFICATION
============================================================
BANNER

# 1) Workflow imports cleanly
import_check=$(docker exec georag-hatchet-worker-ai python3 -c "
import sys; sys.path.insert(0,'/app')
from app.hatchet_workflows.external_notification import (
    external_notification, ExternalNotificationInput, ExternalNotificationOut,
)
print('OK' if external_notification and ExternalNotificationInput and ExternalNotificationOut else 'MISSING')
" 2>&1 | tail -1)
[ "$import_check" = "OK" ] && check "Workflow + IO models import cleanly" ok \
    || check "import" fail "$import_check"

# 2) Hatchet engine knows it
engine_check=$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc \
    "SELECT name FROM \"Workflow\" WHERE name='external_notification' AND \"deletedAt\" IS NULL LIMIT 1;" \
    2>/dev/null | tr -d ' ')
[ "$engine_check" = "external_notification" ] \
    && check "Hatchet engine knows external_notification" ok \
    || check "engine registration" fail "got '$engine_check'"

# 3) AI pool advertises it
pool_check=$(docker exec georag-hatchet-worker-ai python3 -m app.hatchet_workflows.worker --list 2>&1 \
    | grep -c '^external_notification$')
[ "$pool_check" = "1" ] \
    && check "AI worker pool advertises external_notification" ok \
    || check "pool advertisement" fail "got $pool_check"

# 4) integrations_trigger registry has it
flows=$(curl -fsS "$BASE/internal/v1/integrations/flows" -H "X-Service-Key: $KEY" 2>/dev/null)
case "$flows" in
    *external_notification*) check "integrations registry has external_notification" ok ;;
    *)                       check "registry entry" fail "got: $flows" ;;
esac

# 5) Feature flag seeded
flag_count=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM workspace.feature_flags
     WHERE workspace_id IS NULL
       AND flag_name = 'activepieces.external_notification.enabled';" \
    2>/dev/null | tr -d ' ')
[ "$flag_count" = "1" ] \
    && check "feature flag activepieces.external_notification.enabled seeded" ok \
    || check "feature flag" fail "got count=$flag_count"

# 6) Disabled-flag path returns skipped=true. Force flag false; trigger;
#    expect COMPLETED (workflow short-circuits) but no audit row.
NID="phase2-verify-skip-$(date -u +%s)"
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO workspace.feature_flags
        (workspace_id, flag_name, bool_value, updated_at)
    VALUES (NULL, 'activepieces.external_notification.enabled', false, now())
    ON CONFLICT (workspace_id, flag_name) DO UPDATE SET bool_value=false, updated_at=now();" >/dev/null
SKIP_RESP=$(curl -fsS -X POST "$BASE/internal/v1/integrations/external_notification/trigger" \
    -H 'Content-Type: application/json' -H "X-Service-Key: $KEY" \
    -d "{\"notification_id\":\"${NID}\",\"source\":\"verify-skip\",\"kind\":\"x\",\"payload\":{}}")
SKIP_RUN_ID=$(echo "$SKIP_RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("workflow_run_id",""))')
sleep 8
n_skip=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM audit.audit_ledger
     WHERE action_type = 'external_notification.received'
       AND payload->>'notification_id' = '${NID}';" | tr -d ' ')
status_skip=$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc \
    "SELECT readable_status::text FROM v1_runs_olap WHERE external_id='${SKIP_RUN_ID}'::uuid;" \
    2>/dev/null | tr -d ' ')
if [ "$status_skip" = "COMPLETED" ] && [ "$n_skip" = "0" ]; then
    check "Flag-disabled path: workflow COMPLETED with no audit row" ok
else
    check "flag gate" fail "status=$status_skip, audit=$n_skip"
fi

# 7) End-to-end smoke
echo
echo "  ── Running phase2_step5_smoke.sh ──"
if timeout 240 bash "$(dirname "$0")/phase2_step5_smoke.sh" > /tmp/step5_smoke.log 2>&1; then
    check "End-to-end external_notification smoke (happy + idempotency)" ok
else
    check "End-to-end smoke" fail "see /tmp/step5_smoke.log"
    tail -15 /tmp/step5_smoke.log
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
