#!/usr/bin/env bash
# =============================================================================
# scripts/phase3_step5_smoke.sh
#
# Phase 3 Step 5 smoke — exercises the external_notification webhook
# bridge end-to-end with HMAC sender authentication. Bypasses the
# Kestra UI; we mint a JWT, compute HMAC-SHA256 over canonical JSON,
# and POST directly to FastAPI's trigger endpoint.
#
# Cases:
#   1. Valid signature                  → COMPLETED + audit row written
#   2. Tampered payload (sig stale)     → COMPLETED + skipped (no audit)
#   3. Missing signature                → COMPLETED + skipped (no audit)
#   4. Replay (same notification_id)    → COMPLETED + idempotent skip
# =============================================================================

set -uo pipefail

ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
BASE="${FASTAPI_INTERNAL_URL:-http://localhost:8000}"
NOTIFICATION_ID="phase3-step5-$(date -u +%Y%m%dT%H%M%S)"

cleanup() {
    docker exec georag-postgresql psql -U georag -d georag -q -c "
        UPDATE workspace.feature_flags
           SET bool_value = false, updated_at = now()
         WHERE workspace_id IS NULL
           AND flag_name = 'flows.external_notification.enabled';
        DELETE FROM audit.audit_ledger
         WHERE action_type = 'external_notification.received'
           AND payload->>'notification_id' = '${NOTIFICATION_ID}';
    " >/dev/null
}
trap cleanup EXIT

cat <<BANNER

============================================================
PHASE 3 STEP 5 — external_notification (Kestra) SMOKE
============================================================
notification_id : ${NOTIFICATION_ID}
============================================================
BANNER

# Mint JWT.
JWT=$(docker exec georag-fastapi python3 -c "
import sys; sys.path.insert(0,'/app')
from app.services.flow_jwt import mint_flow_jwt
print(mint_flow_jwt('external_notification', ttl_seconds=300), end='')
")
[ -z "$JWT" ] && { echo "  [FAIL] could not mint JWT"; exit 1; }

# Enable flag.
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO workspace.feature_flags
        (workspace_id, flag_name, bool_value, updated_at)
    VALUES (NULL, 'flows.external_notification.enabled', true, now())
    ON CONFLICT (workspace_id, flag_name) DO UPDATE
        SET bool_value = EXCLUDED.bool_value, updated_at = now();
" >/dev/null
echo "  flag enabled, JWT minted"

# Read HMAC secret from .env (cut is more shell-portable than awk's $2).
HMAC_SECRET=$(grep '^EXTERNAL_NOTIFICATION_HMAC_SECRET=' "$ENVFILE" | cut -d= -f2- | head -1)
[ -z "$HMAC_SECRET" ] && { echo "  [FAIL] HMAC secret not in .env"; exit 1; }
echo "  HMAC secret loaded (${#HMAC_SECRET} bytes)"

# Build signed payload by invoking the workflow's OWN canonical_json_for_hmac
# helper inside the AI worker container — single source of truth for the
# canonicalisation. The signature computed here MUST match what
# verify_hmac_signature() will compute on the receiving side.
build_payload() {
    local note="$1"
    docker exec -e EXTERNAL_NOTIFICATION_HMAC_SECRET="$HMAC_SECRET" \
        -e NOTE="$note" -e NID="$NOTIFICATION_ID" \
        georag-hatchet-worker-ai python3 -c "
import os, hmac, hashlib, json, sys
sys.path.insert(0, '/app')
from app.hatchet_workflows.external_notification import (
    canonical_json_for_hmac, ExternalNotificationInput,
)
inp = ExternalNotificationInput(
    notification_id=os.environ['NID'],
    source='phase3-step5-smoke',
    kind='report_filed',
    payload={'report_url': 'https://example.test/r/123', 'note': os.environ['NOTE']},
    received_at='2026-05-10T20:00:00Z',
)
canon = canonical_json_for_hmac(inp)
sig = hmac.new(os.environ['EXTERNAL_NOTIFICATION_HMAC_SECRET'].encode(), canon, hashlib.sha256).hexdigest()
out = inp.model_dump()
out['signature'] = sig
print(json.dumps(out))
"
}

post() {
    curl -fsS -X POST "$BASE/internal/v1/integrations/external_notification/trigger" \
        -H 'Content-Type: application/json' \
        -H "Authorization: Bearer $JWT" \
        -d "$1"
}

wait_for_status() {
    local run_id="$1"
    for i in $(seq 1 12); do
        s=$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc \
            "SELECT readable_status::text FROM v1_runs_olap WHERE external_id='${run_id}'::uuid LIMIT 1;" \
            2>/dev/null | tr -d ' ')
        if [ "$s" = "COMPLETED" ]; then return 0; fi
        case "$s" in FAILED|CANCELLED|EVICTED) return 1 ;; esac
        sleep 5
    done
    return 1
}

audit_count() {
    docker exec georag-postgresql psql -U georag -d georag -tAc "
        SELECT count(*) FROM audit.audit_ledger
         WHERE action_type = 'external_notification.received'
           AND payload->>'notification_id' = '${NOTIFICATION_ID}';" | tr -d ' '
}

# ---------------------------------------------------------------------------
# Case 1 — valid signature → COMPLETED + audit row
# ---------------------------------------------------------------------------
echo
echo "--- Case 1: valid signature ---"
PAYLOAD_OK=$(build_payload "case1-valid")
echo "  payload (first 100 chars): ${PAYLOAD_OK:0:100}"
echo "  payload length: ${#PAYLOAD_OK}"
RESP=$(post "$PAYLOAD_OK")
RUN_ID=$(echo "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("workflow_run_id",""))')
[ -z "$RUN_ID" ] && { echo "  [FAIL] no run_id"; exit 1; }
wait_for_status "$RUN_ID" || { echo "  [FAIL] never COMPLETED"; exit 1; }
n=$(audit_count)
[ "$n" = "1" ] && echo "  [PASS] valid sig → audit row landed" || { echo "  [FAIL] audit=$n"; exit 1; }

# ---------------------------------------------------------------------------
# Case 2 — tampered payload (signature is for the original payload, but
# we modify a field after signing). Must short-circuit, no new audit.
# ---------------------------------------------------------------------------
echo
echo "--- Case 2: tampered payload (signature for stale data) ---"
PAYLOAD_TAMPERED=$(echo "$PAYLOAD_OK" | python3 -c 'import json,sys;d=json.load(sys.stdin);d["payload"]["report_url"]="https://attacker.test/x";print(json.dumps(d))')
RESP=$(post "$PAYLOAD_TAMPERED")
RUN_ID=$(echo "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("workflow_run_id",""))')
wait_for_status "$RUN_ID" || { echo "  [FAIL] never COMPLETED"; exit 1; }
n2=$(audit_count)
[ "$n2" = "1" ] && echo "  [PASS] tampered → COMPLETED + no new audit (still 1)" \
    || { echo "  [FAIL] audit=$n2 (expected still 1)"; exit 1; }

# ---------------------------------------------------------------------------
# Case 3 — missing signature → skipped, no audit
# ---------------------------------------------------------------------------
echo
echo "--- Case 3: missing signature ---"
PAYLOAD_NOSIG=$(echo "$PAYLOAD_OK" | python3 -c 'import json,sys;d=json.load(sys.stdin);d.pop("signature",None);d["notification_id"]="'${NOTIFICATION_ID}'-nosig";print(json.dumps(d))')
RESP=$(post "$PAYLOAD_NOSIG")
RUN_ID=$(echo "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("workflow_run_id",""))')
wait_for_status "$RUN_ID" || { echo "  [FAIL] never COMPLETED"; exit 1; }
nosig_audit=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM audit.audit_ledger
     WHERE payload->>'notification_id' = '${NOTIFICATION_ID}-nosig';" | tr -d ' ')
[ "$nosig_audit" = "0" ] && echo "  [PASS] missing sig → no audit" \
    || { echo "  [FAIL] audit=$nosig_audit (expected 0)"; exit 1; }

# ---------------------------------------------------------------------------
# Case 4 — replay (same notification_id, same valid sig). Should be
# idempotent: COMPLETED but no second audit row.
# ---------------------------------------------------------------------------
echo
echo "--- Case 4: replay (idempotent) ---"
RESP=$(post "$PAYLOAD_OK")
RUN_ID=$(echo "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("workflow_run_id",""))')
wait_for_status "$RUN_ID" || { echo "  [FAIL] never COMPLETED"; exit 1; }
n4=$(audit_count)
[ "$n4" = "1" ] && echo "  [PASS] replay → COMPLETED + idempotent (still 1 audit)" \
    || { echo "  [FAIL] audit=$n4 (expected still 1)"; exit 1; }

echo
echo "============================================================"
echo "PHASE 3 STEP 5 — SMOKE PASSED (4 / 4 cases)"
echo "============================================================"
