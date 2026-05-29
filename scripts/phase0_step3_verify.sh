#!/usr/bin/env bash
# =============================================================================
# scripts/phase0_step3_verify.sh
#
# Phase 0 Step 3 done-definition (per georag-phase0-implementation-kickoff.md).
# Verifies the observability foundation:
#   - OTel collector receiving + healthy
#   - Tempo healthy + accepts spans + serves them by trace_id
#   - Langfuse reachable
#   - Prometheus scraping vLLM
#   - Workflow Run Dashboard route reachable (added 2026-05-09 once the
#     skeleton landed — see app/Http/Controllers/Admin/WorkflowRunController
#     and resources/js/Pages/Admin/WorkflowRuns.tsx)
# =============================================================================

set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=lib/phase0_env.sh
. "${HERE}/lib/phase0_env.sh"

PASS=0
TOTAL=6

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
PHASE 0 STEP 3 — DONE-DEFINITION VERIFICATION
============================================================
BANNER

# 1) OTel collector health endpoint
otel_health=$(curl -s -o /dev/null -w '%{http_code}' ${OTEL_HEALTH_URL}/)
[ "$otel_health" = "200" ] && check "OTel collector /health → 200" ok || check "OTel collector health" fail "http $otel_health"

# 2) Tempo readiness endpoint
tempo_ready=$(curl -s -o /dev/null -w '%{http_code}' ${TEMPO_URL}/ready)
[ "$tempo_ready" = "200" ] && check "Tempo /ready → 200" ok || check "Tempo ready" fail "http $tempo_ready"

# 3) Span round-trip: emit → wait → query Tempo by trace_id
TRACE_ID=$(bash "$(dirname "$0")/emit_test_span.sh")
sleep 6
tempo_resp=$(curl -s "${TEMPO_URL}/api/traces/${TRACE_ID}")
batches=$(echo "$tempo_resp" | python3 -c 'import sys,json
try:
    d = json.loads(sys.stdin.read())
    print(len(d.get("batches", d.get("traces", []))))
except Exception:
    print(0)' 2>/dev/null || echo 0)
if [ "${batches:-0}" -ge 1 ] 2>/dev/null; then
    check "Tempo received and serves test span (trace_id=${TRACE_ID:0:8}…, batches=$batches)" ok
else
    check "Tempo round-trip" fail "trace not found: ${tempo_resp:0:120}"
fi

# 4) Langfuse web reachable (port 3001 per existing compose).
lf_health=$(curl -s -o /dev/null -w '%{http_code}' http://localhost:3001/api/public/health 2>/dev/null || echo 000)
[ "$lf_health" = "200" ] || [ "$lf_health" = "401" ] && check "Langfuse web reachable (HTTP $lf_health)" ok || check "Langfuse" fail "http $lf_health"

# 5) Prometheus scraping vLLM — at least one vllm:* sample within last 5m
PROM_PORT="${PROMETHEUS_PORT:-9090}"
prom_query=$(curl -sG "http://localhost:${PROM_PORT}/api/v1/query" \
    --data-urlencode 'query=count({__name__=~"vllm:.+"})' 2>/dev/null \
    | python3 -c 'import sys,json
try:
    d = json.loads(sys.stdin.read())
    res = d.get("data",{}).get("result",[])
    if res and float(res[0]["value"][1]) > 0:
        print("ok");
    else:
        print("zero")
except Exception as e:
    print("err:" + str(e))' 2>/dev/null)
[ "$prom_query" = "ok" ] && check "Prometheus scraping vLLM (vllm:* samples present)" ok || check "Prometheus vLLM scrape" fail "$prom_query"

# 6) Workflow Run Dashboard route reachable. Octane-served Laravel app sits
#    behind /admin/workflow-runs which requires admin auth — so an
#    unauthenticated GET should redirect (302) to /login (Laravel's default
#    auth.failed handler for web requests). 200 is also valid in case a
#    session cookie is present in the runner's environment. 5xx → fail.
#    LARAVEL_BASE_URL overrides; default 8888 matches the docker-compose
#    APP_PORT default (port 8000 is FastAPI in this project).
LARAVEL_BASE_URL="${LARAVEL_BASE_URL:-http://localhost:8888}"
dashboard_code=$(curl -s -o /dev/null -w '%{http_code}' \
    --max-time 5 \
    "${LARAVEL_BASE_URL}/admin/workflow-runs" 2>/dev/null || echo 000)
case "$dashboard_code" in
    200|302|401|403)
        check "Workflow Run Dashboard route reachable (HTTP $dashboard_code at $LARAVEL_BASE_URL/admin/workflow-runs)" ok
        ;;
    *)
        check "Workflow Run Dashboard route" fail "http $dashboard_code at $LARAVEL_BASE_URL/admin/workflow-runs"
        ;;
esac

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"
echo

exit $((PASS == TOTAL ? 0 : 1))
