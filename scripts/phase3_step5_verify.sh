#!/usr/bin/env bash
# =============================================================================
# scripts/phase3_step5_verify.sh
#
# Phase 3 Step 5 done-definition — external_notification + HMAC.
#
#   1. Kestra YAML flow file present + parses
#   2. forward_to_fastapi task + Webhook trigger declared
#   3. Kestra validates flow YAML (no constraint errors)
#   4. external_notification workflow imports cleanly
#   5. canonical_json_for_hmac is deterministic + UTF-8
#   6. AI worker has EXTERNAL_NOTIFICATION_HMAC_SECRET set
#   7. End-to-end smoke (4 cases — valid / tampered / missing / replay)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
KESTRA_PORT=$(awk -F= '/^KESTRA_PORT=/         { print $2 }' "$ENVFILE" 2>/dev/null | head -1)
KESTRA_PORT="${KESTRA_PORT:-8086}"
KESTRA_USER=$(awk -F= '/^KESTRA_BASIC_AUTH_USER=/      { print $2 }' "$ENVFILE" 2>/dev/null | head -1)
KESTRA_PASS=$(awk -F= '/^KESTRA_BASIC_AUTH_PASSWORD=/  { print $2 }' "$ENVFILE" 2>/dev/null | head -1)
REPO="${REPO_ROOT:-/home/georag/projects/georag}"
FLOW_FILE="$REPO/kestra/flows/georag/external_notification.yaml"

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
PHASE 3 STEP 5 — Kestra external_notification + HMAC
============================================================
BANNER

# 1) YAML present + parses
if [ ! -f "$FLOW_FILE" ]; then
    check "Kestra YAML present" fail "missing"
elif python3 -c "import yaml; yaml.safe_load(open('$FLOW_FILE'))" 2>/dev/null; then
    check "Kestra YAML parses ($(wc -l < "$FLOW_FILE") lines)" ok
else
    check "YAML parse" fail "invalid"
fi

# 2) Required tasks + webhook trigger
struct_ok=$(python3 -c "
import yaml
d = yaml.safe_load(open('$FLOW_FILE'))
ids = [t['id'] for t in d.get('tasks', [])]
trigs = d.get('triggers', [])
has_task = 'forward_to_fastapi' in ids
has_webhook = any('Webhook' in t.get('type','') for t in trigs)
print('OK' if (has_task and has_webhook) else 'BAD')
" 2>/dev/null)
[ "$struct_ok" = "OK" ] && check "forward_to_fastapi task + Webhook trigger declared" ok \
    || check "structure" fail "$struct_ok"

# 3) Kestra validates the YAML
if [ -z "$KESTRA_PASS" ]; then
    check "Kestra validates YAML" fail "no basic-auth pw"
else
    body=$(curl -s -u "${KESTRA_USER}:${KESTRA_PASS}" \
        -X POST "http://localhost:${KESTRA_PORT}/api/v1/main/flows/validate" \
        -H 'Content-Type: application/x-yaml' --data-binary @"$FLOW_FILE")
    if echo "$body" | python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
items = data if isinstance(data, list) else [data]
errs = [i for i in items if i.get('constraints')]
sys.exit(1 if errs else 0)
" 2>/dev/null; then
        check "Kestra validates flow YAML (no constraint errors)" ok
    else
        check "Kestra YAML validation" fail "$(echo "$body" | head -c 300)"
    fi
fi

# 4) Workflow imports
import_check=$(docker exec georag-hatchet-worker-ai python3 -c "
import sys; sys.path.insert(0,'/app')
from app.hatchet_workflows.external_notification import (
    external_notification, ExternalNotificationInput, ExternalNotificationOut,
    canonical_json_for_hmac, verify_hmac_signature,
)
print('OK')
" 2>&1 | tail -1)
[ "$import_check" = "OK" ] && check "workflow + HMAC helpers import" ok \
    || check "import" fail "$import_check"

# 5) canonical_json_for_hmac determinism + UTF-8
det=$(docker exec georag-hatchet-worker-ai python3 -c "
import sys; sys.path.insert(0,'/app')
from app.hatchet_workflows.external_notification import (
    canonical_json_for_hmac, ExternalNotificationInput,
)
i1 = ExternalNotificationInput(notification_id='n', source='s', kind='k',
                                payload={'b': 1, 'a': 2}, received_at='2026-05-10T00:00:00Z')
i2 = ExternalNotificationInput(notification_id='n', source='s', kind='k',
                                payload={'a': 2, 'b': 1}, received_at='2026-05-10T00:00:00Z')
b1, b2 = canonical_json_for_hmac(i1), canonical_json_for_hmac(i2)
print('OK' if b1 == b2 and b1.startswith(b'{') else f'BAD b1={b1!r} b2={b2!r}')
" 2>&1 | tail -1)
[ "$det" = "OK" ] && check "canonical_json_for_hmac deterministic across key order" ok \
    || check "canonicalisation" fail "$det"

# 6) AI worker has the secret
hmac_set=$(docker exec georag-hatchet-worker-ai env | grep -c '^EXTERNAL_NOTIFICATION_HMAC_SECRET=' | tr -d ' ')
[ "$hmac_set" = "1" ] && check "AI worker has EXTERNAL_NOTIFICATION_HMAC_SECRET set" ok \
    || check "HMAC secret env" fail "got $hmac_set"

# 7) End-to-end smoke
echo
echo "  ── Running phase3_step5_smoke.sh ──"
if timeout 240 bash "$(dirname "$0")/phase3_step5_smoke.sh" > /tmp/p3_step5_smoke.log 2>&1; then
    check "End-to-end HMAC smoke (valid + tampered + missing + replay)" ok
else
    check "End-to-end smoke" fail "see /tmp/p3_step5_smoke.log"
    tail -20 /tmp/p3_step5_smoke.log
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
