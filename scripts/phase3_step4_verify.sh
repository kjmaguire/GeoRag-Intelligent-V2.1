#!/usr/bin/env bash
# =============================================================================
# scripts/phase3_step4_verify.sh
#
# Phase 3 Step 4 done-definition — public_geoscience_pull on Kestra.
#
#   1. Kestra YAML flow file present + parses as YAML
#   2. Required tasks present (fetch_upstream + upload_to_bronze + trigger_hatchet)
#   3. Schedule trigger declared (cron) and DISABLED by default
#   4. Kestra service knows about the flow (via /api/v1/main/flows/{ns}/{id})
#      OR can validate it via /api/v1/main/flows/validate
#   5. End-to-end smoke (delegates to phase3_step4_smoke.sh) — proves the
#      receiving FastAPI + Hatchet side works with per-flow JWT auth
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=5
ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
KESTRA_PORT=$(awk -F= '/^KESTRA_PORT=/         { print $2 }' "$ENVFILE" 2>/dev/null | head -1)
KESTRA_PORT="${KESTRA_PORT:-8086}"
KESTRA_USER=$(awk -F= '/^KESTRA_BASIC_AUTH_USER=/      { print $2 }' "$ENVFILE" 2>/dev/null | head -1)
KESTRA_PASS=$(awk -F= '/^KESTRA_BASIC_AUTH_PASSWORD=/  { print $2 }' "$ENVFILE" 2>/dev/null | head -1)
REPO="${REPO_ROOT:-/home/georag/projects/georag}"
FLOW_FILE="$REPO/kestra/flows/georag/public_geoscience_pull.yaml"

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
PHASE 3 STEP 4 — Kestra public_geoscience_pull VERIFICATION
============================================================
BANNER

# 1) File present + parses as YAML
if [ ! -f "$FLOW_FILE" ]; then
    check "Kestra YAML file present" fail "missing $FLOW_FILE"
elif python3 -c "import yaml; yaml.safe_load(open('$FLOW_FILE'))" 2>/dev/null; then
    check "Kestra YAML file parses ($(wc -l < "$FLOW_FILE") lines)" ok
else
    check "Kestra YAML parses" fail "invalid YAML"
fi

# 2) Required tasks present
tasks_ok=$(python3 -c "
import yaml
d = yaml.safe_load(open('$FLOW_FILE'))
ids = [t['id'] for t in d.get('tasks', [])]
required = {'fetch_upstream', 'upload_to_bronze', 'trigger_hatchet'}
missing = required - set(ids)
print('OK' if not missing else f'MISSING:{sorted(missing)}')
" 2>/dev/null)
[ "$tasks_ok" = "OK" ] && check "All required tasks declared" ok \
    || check "tasks" fail "$tasks_ok"

# 3) Schedule trigger DISABLED by default
sched_ok=$(python3 -c "
import yaml
d = yaml.safe_load(open('$FLOW_FILE'))
trigs = d.get('triggers', [])
sched = [t for t in trigs if t.get('type','').endswith('Schedule')]
ok = bool(sched) and sched[0].get('disabled') is True and sched[0].get('cron')
print('OK' if ok else 'BAD')
" 2>/dev/null)
[ "$sched_ok" = "OK" ] && check "Schedule trigger declared, disabled by default" ok \
    || check "schedule" fail "$sched_ok"

# 4) Validate via Kestra API. Kestra v1.2.x: POST /api/v1/main/flows/validate
#    with the YAML body returns 200 on parse success or details on failure.
if [ -z "$KESTRA_PASS" ]; then
    check "Kestra validates flow YAML" fail "no KESTRA_BASIC_AUTH_PASSWORD"
else
    body=$(curl -s -u "${KESTRA_USER}:${KESTRA_PASS}" \
        -X POST "http://localhost:${KESTRA_PORT}/api/v1/main/flows/validate" \
        -H 'Content-Type: application/x-yaml' \
        --data-binary @"$FLOW_FILE")
    # Kestra returns either an array of validation results or a 200 OK + body.
    # Look for "constraints" of any error severity.
    if echo "$body" | python3 -c "
import json, sys
data = json.loads(sys.stdin.read())
# Kestra returns either: list of {constraints, deprecationPaths, infos}
# or a single object. Either way we want to surface ERROR-level constraints.
def has_errors(item):
    cs = item.get('constraints') or []
    return any(c.get('message') for c in cs) if isinstance(cs, list) else False
items = data if isinstance(data, list) else [data]
errs = [i for i in items if has_errors(i)]
sys.exit(1 if errs else 0)
" 2>/dev/null; then
        check "Kestra validates flow YAML (no constraint errors)" ok
    else
        check "Kestra YAML validation" fail "$(echo "$body" | head -c 300)"
    fi
fi

# 5) End-to-end smoke
echo
echo "  ── Running phase3_step4_smoke.sh ──"
if timeout 240 bash "$(dirname "$0")/phase3_step4_smoke.sh" > /tmp/p3_step4_smoke.log 2>&1; then
    check "End-to-end public_geoscience_pull smoke (JWT auth)" ok
else
    check "End-to-end smoke" fail "see /tmp/p3_step4_smoke.log"
    tail -15 /tmp/p3_step4_smoke.log
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
