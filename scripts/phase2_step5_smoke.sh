#!/usr/bin/env bash
# =============================================================================
# scripts/phase2_step5_smoke.sh
#
# Phase 2 Step 5a smoke — exercises the external_notification webhook
# bridge end-to-end WITHOUT depending on the Activepieces UI:
#
#   1. Sets activepieces.external_notification.enabled = true
#   2. POSTs /internal/v1/integrations/external_notification/trigger with a
#      synthetic payload (notification_id=phase2-step5-<ts>)
#   3. Waits for the workflow to complete (v1_runs_olap status COMPLETED)
#   4. Asserts audit.audit_ledger has 'external_notification.received' with
#      the matching notification_id
#   5. POSTs the SAME notification_id again and asserts the second run
#      classifies as 'skipped' (idempotency by notification_id)
#   6. Cleanup on EXIT — flag back to false, audit rows deleted
# =============================================================================

set -uo pipefail

ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
KEY=$(awk -F= '/^FASTAPI_SERVICE_KEY=/ { print $2 }' "$ENVFILE" 2>/dev/null | head -1)
BASE="${FASTAPI_INTERNAL_URL:-http://localhost:8000}"

NOTIFICATION_ID="phase2-step5-$(date -u +%Y%m%dT%H%M%S)"

cleanup() {
    docker exec georag-postgresql psql -U georag -d georag -q -c "
        UPDATE workspace.feature_flags
           SET bool_value = false, updated_at = now()
         WHERE workspace_id IS NULL
           AND flag_name = 'activepieces.external_notification.enabled';
        DELETE FROM audit.audit_ledger
         WHERE action_type = 'external_notification.received'
           AND payload->>'notification_id' = '${NOTIFICATION_ID}';
    " >/dev/null
}
trap cleanup EXIT

cat <<BANNER

============================================================
PHASE 2 STEP 5a — external_notification SMOKE
============================================================
notification_id : ${NOTIFICATION_ID}
============================================================
BANNER

# 1. Enable flag.
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO workspace.feature_flags
        (workspace_id, flag_name, bool_value, updated_at)
    VALUES (NULL, 'activepieces.external_notification.enabled', true, now())
    ON CONFLICT (workspace_id, flag_name) DO UPDATE
        SET bool_value = EXCLUDED.bool_value, updated_at = now();
" >/dev/null
echo "  flag enabled"

post_trigger() {
    curl -fsS -X POST "$BASE/internal/v1/integrations/external_notification/trigger" \
        -H 'Content-Type: application/json' \
        -H "X-Service-Key: $KEY" \
        -d "$1"
}

wait_for_status() {
    local run_id="$1"
    local target="$2"
    for i in $(seq 1 12); do
        s=$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc \
            "SELECT readable_status::text FROM v1_runs_olap WHERE external_id='${run_id}'::uuid LIMIT 1;" \
            2>/dev/null | tr -d ' ')
        if [ "$s" = "$target" ]; then return 0; fi
        case "$s" in FAILED|CANCELLED|EVICTED) return 1 ;; esac
        sleep 5
    done
    return 1
}

# 2. First-delivery POST.
echo
echo "--- POST #1 (first delivery) ---"
RESP1=$(post_trigger "{
    \"notification_id\": \"${NOTIFICATION_ID}\",
    \"source\": \"phase2-step5-smoke\",
    \"kind\": \"report_filed\",
    \"payload\": {\"report_url\": \"https://example.test/r/123\", \"company\": \"Acme\"},
    \"received_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"
}")
echo "  $RESP1"
RUN1=$(echo "$RESP1" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("workflow_run_id",""))')

if ! wait_for_status "$RUN1" "COMPLETED"; then
    echo "  [FAIL] first-delivery workflow never reached COMPLETED"
    exit 1
fi
echo "  [PASS] first-delivery COMPLETED"

# 3. Audit row landed.
n_audit=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM audit.audit_ledger
     WHERE action_type = 'external_notification.received'
       AND payload->>'notification_id' = '${NOTIFICATION_ID}';" | tr -d ' ')
echo "  audit rows for notification_id: ${n_audit}"
[ "$n_audit" = "1" ] || { echo "  [FAIL] expected 1 audit row, got $n_audit"; exit 1; }
echo "  [PASS] audit landed"

# 4. Idempotency — second delivery of same notification_id MUST NOT add
#    a second audit row. The workflow short-circuits inside its task.
echo
echo "--- POST #2 (duplicate notification_id) ---"
RESP2=$(post_trigger "{
    \"notification_id\": \"${NOTIFICATION_ID}\",
    \"source\": \"phase2-step5-smoke\",
    \"kind\": \"report_filed\",
    \"payload\": {\"replay\": true},
    \"received_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"
}")
echo "  $RESP2"
RUN2=$(echo "$RESP2" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("workflow_run_id",""))')
if ! wait_for_status "$RUN2" "COMPLETED"; then
    echo "  [FAIL] second-delivery workflow never reached COMPLETED"
    exit 1
fi

n_audit2=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM audit.audit_ledger
     WHERE action_type = 'external_notification.received'
       AND payload->>'notification_id' = '${NOTIFICATION_ID}';" | tr -d ' ')
echo "  audit rows after duplicate: ${n_audit2}"
if [ "$n_audit2" = "1" ]; then
    echo "  [PASS] idempotent — no second audit row"
else
    echo "  [FAIL] expected 1 audit row, got $n_audit2 (idempotency broken)"
    exit 1
fi

echo
echo "============================================================"
echo "PHASE 2 STEP 5a — SMOKE PASSED"
echo "============================================================"
