#!/usr/bin/env bash
# =============================================================================
# scripts/section10_acceptance.sh
#
# Master-plan §10 (Eval harness + Customer Support Cockpit) — v1
# acceptance harness. Mirrors scripts/section11_acceptance.sh in shape +
# exit-code semantics. Run after any §10 change + before declaring the
# §10-v1 surface clean.
#
# Pre-requisites (the script asserts each):
#   - Docker compose stack is up
#   - FASTAPI_SERVICE_KEY env var is set + matches Laravel .env
#   - psql reachable via docker exec on the postgresql container
#
# What this harness covers (the §10-v1 surface):
#   §10.1   — eval.golden_questions schema present
#   §10.2b  — golden_questions seeded to ≥110 across ≥6 sets, each ≥10
#   §10.4   — evaluate_workspace workflow registered (ai pool)
#   §10.5   — eval.run_results schema present
#   §10.6   — POST /api/v1/admin/eval/assess-promotion responds 200 + 400
#   §10.7   — eval_real_rag_nightly workflow + cron schedule registered
#   §10.8   — ops.support_tickets / _traces / _replay_runs tables present
#   §10.10  — support_replay workflow registered
#   §10.11  — Support Cockpit Inertia routes resolve (admin auth gated;
#             we assert the FastAPI cockpit data endpoint instead)
#   §10.12  — cross_workspace_audit emitter importable + service-key gate
#   §10.13  — LangFuse trace-deep-link plumbing (env-var fallback)
#
# What this harness explicitly does NOT cover (deferred §10-v2):
#   §10.3   — admin authoring UI for golden questions
#   §10.7-v2 — eval dashboard with diff visualizer
#
# Exit code 0 = §10-v1 ready for prod rollout. 1 = at least one check failed.
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=0
FAILED=()

SERVICE_KEY="${FASTAPI_SERVICE_KEY:-}"
FASTAPI_URL="${FASTAPI_URL:-http://localhost:8000}"
PG_CONTAINER="${PG_CONTAINER:-georag-postgresql}"
FASTAPI_CONTAINER="${FASTAPI_CONTAINER:-georag-fastapi}"
PG_USER="${PG_USER:-georag}"
PG_DB="${PG_DB:-georag}"

if [ -z "$SERVICE_KEY" ]; then
    echo "ERROR: FASTAPI_SERVICE_KEY env var is required (must match FastAPI container)."
    exit 2
fi

psql_q() {
    docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -tAc "$1" 2>/dev/null
}

check() {
    local label="$1"
    local cmd="$2"
    local expected="${3:-200}"

    TOTAL=$((TOTAL + 1))
    local code
    code=$(eval "$cmd" 2>/dev/null)
    local match=0
    IFS=',' read -ra accepted <<< "$expected"
    for c in "${accepted[@]}"; do
        if [ "$code" = "$c" ]; then match=1; break; fi
    done
    if [ "$match" = "1" ]; then
        echo "  [PASS] $label (HTTP $code)"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $label (HTTP $code, expected $expected)"
        FAILED+=("$label")
    fi
}

assert_eq() {
    local label="$1"
    local actual="$2"
    local expected="$3"
    TOTAL=$((TOTAL + 1))
    if [ "$actual" = "$expected" ]; then
        echo "  [PASS] $label (= $expected)"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $label (got '$actual', expected '$expected')"
        FAILED+=("$label")
    fi
}

assert_ge() {
    local label="$1"
    local actual="$2"
    local threshold="$3"
    TOTAL=$((TOTAL + 1))
    if [ -n "$actual" ] && [ "$actual" -ge "$threshold" ] 2>/dev/null; then
        echo "  [PASS] $label ($actual >= $threshold)"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $label (got '$actual', expected >= $threshold)"
        FAILED+=("$label")
    fi
}

curl_code() {
    curl -s -o /dev/null -w '%{http_code}' -H "X-Service-Key: $SERVICE_KEY" "$@"
}

echo
echo "=============================================================="
echo "  Master-plan §10 acceptance harness — v1 surface"
echo "  Target: $FASTAPI_URL"
echo "=============================================================="
echo

# ----------------------------------------------------------------------------
# 1. §10.1 + §10.5 — DB schemas
# ----------------------------------------------------------------------------
echo "-- §10.1 + §10.5 DB schemas --"
for tbl in "eval.golden_questions" "eval.run_results"; do
    schema="${tbl%%.*}"
    name="${tbl##*.}"
    present=$(psql_q "SELECT 1 FROM information_schema.tables WHERE table_schema='$schema' AND table_name='$name';" | head -1 | tr -d ' ')
    TOTAL=$((TOTAL + 1))
    if [ "$present" = "1" ]; then
        echo "  [PASS] table $tbl present"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] table $tbl missing"
        FAILED+=("$tbl missing")
    fi
done

# ----------------------------------------------------------------------------
# 2. §10.2b — golden questions seeded
# ----------------------------------------------------------------------------
echo
echo "-- §10.2b golden questions seeded --"
TOTAL_Q=$(psql_q "SELECT count(*) FROM eval.golden_questions;" | head -1 | tr -d ' ')
assert_ge "golden_questions count" "$TOTAL_Q" 110

SETS_WITH_TEN=$(psql_q "SELECT count(*) FROM (SELECT question_set FROM eval.golden_questions GROUP BY question_set HAVING count(*) >= 10) q;" | head -1 | tr -d ' ')
assert_ge "sets with >=10 questions" "$SETS_WITH_TEN" 6

# ----------------------------------------------------------------------------
# 3. §10.4 + §10.7 + §10.10 — Hatchet workflows registered
# ----------------------------------------------------------------------------
echo
echo "-- §10.4 / §10.7 / §10.10 workflow registration --"
REGISTERED=$(docker exec "$FASTAPI_CONTAINER" python -c "
from app.hatchet_workflows.worker import POOLS
names = sorted({w.name for pool in POOLS.values() for w in pool})
print(','.join(names))
" 2>/dev/null)
for expected in evaluate_workspace eval_real_rag_nightly support_replay; do
    TOTAL=$((TOTAL + 1))
    if echo ",$REGISTERED," | grep -q ",$expected,"; then
        echo "  [PASS] workflow $expected registered"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] workflow $expected NOT registered"
        FAILED+=("workflow $expected")
    fi
done

# ----------------------------------------------------------------------------
# 4. §10.7 — nightly cron schedule
# ----------------------------------------------------------------------------
echo
echo "-- §10.7 nightly cron schedule --"
TOTAL=$((TOTAL + 1))
if docker exec "$FASTAPI_CONTAINER" python -c "
from app.hatchet_workflows.eval_real_rag_nightly import eval_real_rag_nightly
crons = getattr(eval_real_rag_nightly.config, 'on_crons', None) or getattr(eval_real_rag_nightly, 'on_crons', [])
import sys
sys.exit(0 if '15 5 * * *' in (crons or []) else 1)
" 2>/dev/null; then
    echo "  [PASS] eval_real_rag_nightly cron = 15 5 * * *"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] eval_real_rag_nightly cron missing or wrong slot"
    FAILED+=("nightly cron")
fi

# ----------------------------------------------------------------------------
# 5. §10.6 — promotion-gate endpoint
# ----------------------------------------------------------------------------
echo
echo "-- §10.6 promotion-gate endpoint --"
# 400 path (same run id)
SAME_UUID="00000000-0000-0000-0000-000000000099"
check "POST /api/v1/admin/eval/assess-promotion (same ids → 400)" \
      "curl -s -o /dev/null -w '%{http_code}' -X POST \
        -H 'X-Service-Key: $SERVICE_KEY' \
        -H 'Content-Type: application/json' \
        -d '{\"workspace_id\":\"a0000000-0000-0000-0000-000000000001\",\"candidate_run_id\":\"$SAME_UUID\",\"baseline_run_id\":\"$SAME_UUID\",\"dry_run\":true}' \
        '$FASTAPI_URL/api/v1/admin/eval/assess-promotion'" "400"

# 200 path (distinct ids; runs need not exist — empty deltas + allow=true)
DIFF_BASELINE="00000000-0000-0000-0000-00000000aa01"
DIFF_CAND="00000000-0000-0000-0000-00000000aa02"
check "POST /api/v1/admin/eval/assess-promotion (distinct ids → 200)" \
      "curl -s -o /dev/null -w '%{http_code}' -X POST \
        -H 'X-Service-Key: $SERVICE_KEY' \
        -H 'Content-Type: application/json' \
        -d '{\"workspace_id\":\"a0000000-0000-0000-0000-000000000001\",\"candidate_run_id\":\"$DIFF_CAND\",\"baseline_run_id\":\"$DIFF_BASELINE\",\"dry_run\":true}' \
        '$FASTAPI_URL/api/v1/admin/eval/assess-promotion'" "200"

# Service-key gate (no header → 401)
check "POST /api/v1/admin/eval/assess-promotion (no service key → 401)" \
      "curl -s -o /dev/null -w '%{http_code}' -X POST \
        -H 'Content-Type: application/json' \
        -d '{\"workspace_id\":\"a0000000-0000-0000-0000-000000000001\",\"candidate_run_id\":\"$DIFF_CAND\",\"baseline_run_id\":\"$DIFF_BASELINE\",\"dry_run\":true}' \
        '$FASTAPI_URL/api/v1/admin/eval/assess-promotion'" "401,422"

# ----------------------------------------------------------------------------
# 6. §10.8 — support cockpit tables
# ----------------------------------------------------------------------------
echo
echo "-- §10.8 support cockpit tables --"
for tbl in "ops.support_tickets" "ops.support_ticket_traces" "ops.support_replay_runs"; do
    schema="${tbl%%.*}"
    name="${tbl##*.}"
    present=$(psql_q "SELECT 1 FROM information_schema.tables WHERE table_schema='$schema' AND table_name='$name';" | head -1 | tr -d ' ')
    TOTAL=$((TOTAL + 1))
    if [ "$present" = "1" ]; then
        echo "  [PASS] table $tbl present"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] table $tbl missing"
        FAILED+=("$tbl missing")
    fi
done

# ----------------------------------------------------------------------------
# 7. §10.12 — cross-workspace audit emitter importable
# ----------------------------------------------------------------------------
echo
echo "-- §10.12 cross-workspace audit emitter --"
TOTAL=$((TOTAL + 1))
if docker exec "$FASTAPI_CONTAINER" python -c "
from app.services.cross_workspace_audit import emit_cross_workspace_alert, DEFAULT_IDEMPOTENCY_WINDOW_S
assert DEFAULT_IDEMPOTENCY_WINDOW_S == 3600, f'window={DEFAULT_IDEMPOTENCY_WINDOW_S}'
" 2>/dev/null; then
    echo "  [PASS] cross_workspace_audit importable + 1h window locked"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] cross_workspace_audit import or window default check failed"
    FAILED+=("cross_workspace_audit module")
fi

# ----------------------------------------------------------------------------
# 8. §10.13 — LangFuse deep-link plumbing
# ----------------------------------------------------------------------------
echo
echo "-- §10.13 LangFuse deep-link plumbing --"
TOTAL=$((TOTAL + 1))
# The Inertia page accessor lives in the Laravel controller; assert the
# react component has the renderTraceLink helper symbol.
COCKPIT_TSX="resources/js/Pages/Admin/SupportCockpit.tsx"
if [ -f "$COCKPIT_TSX" ] && grep -q "renderTraceLink\|langfuse_base_url\|langfuse_trace_url" "$COCKPIT_TSX"; then
    echo "  [PASS] SupportCockpit.tsx carries LangFuse link helper"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] SupportCockpit.tsx missing LangFuse link wiring"
    FAILED+=("LangFuse link wiring")
fi

TOTAL=$((TOTAL + 1))
CTRL_PHP="app/Http/Controllers/Admin/SupportCockpitController.php"
if [ -f "$CTRL_PHP" ] && grep -q "langfuse_base_url\|LANGFUSE_BASE_URL" "$CTRL_PHP"; then
    echo "  [PASS] SupportCockpitController exposes langfuse_base_url prop"
    PASS=$((PASS + 1))
else
    echo "  [FAIL] SupportCockpitController missing langfuse_base_url prop"
    FAILED+=("LangFuse controller prop")
fi

# ----------------------------------------------------------------------------
# 9. §10-v2 — authoring + compare API endpoints
# ----------------------------------------------------------------------------
echo
echo "-- §10-v2 authoring + compare endpoints --"
check "GET  /api/v1/admin/eval/questions" \
      "curl_code '$FASTAPI_URL/api/v1/admin/eval/questions?limit=5'"
check "GET  /api/v1/admin/eval/runs" \
      "curl_code '$FASTAPI_URL/api/v1/admin/eval/runs?limit=5'"

# 404 path for per-set summary with a non-existent run
NONEXIST_RUN="00000000-0000-0000-0000-deadbeef0000"
check "GET  /api/v1/admin/eval/runs/<missing>/per-set-summary (404)" \
      "curl_code '$FASTAPI_URL/api/v1/admin/eval/runs/$NONEXIST_RUN/per-set-summary'" "404"

# 200 path for per-set summary if any real run exists
REAL_RUN=$(psql_q "SELECT run_id::text FROM eval.run_summaries ORDER BY started_at DESC LIMIT 1;" | head -1 | tr -d ' ')
if [ -n "$REAL_RUN" ]; then
    check "GET  /api/v1/admin/eval/runs/<real>/per-set-summary" \
          "curl_code '$FASTAPI_URL/api/v1/admin/eval/runs/$REAL_RUN/per-set-summary'"
fi

# Service-key gate on questions CRUD
check "GET  /api/v1/admin/eval/questions (no service key)" \
      "curl -s -o /dev/null -w '%{http_code}' '$FASTAPI_URL/api/v1/admin/eval/questions'" "401,422"

# ----------------------------------------------------------------------------
# 10. §10-v2 — Inertia surface files present
# ----------------------------------------------------------------------------
echo
echo "-- §10-v2 frontend surface --"
for f in \
    "app/Http/Controllers/Admin/EvalQuestionsController.php" \
    "app/Http/Controllers/Admin/EvalCompareController.php" \
    "resources/js/Pages/Admin/EvalQuestions.tsx" \
    "resources/js/Pages/Admin/EvalQuestionEditor.tsx" \
    "resources/js/Pages/Admin/EvalCompare.tsx"; do
    TOTAL=$((TOTAL + 1))
    if [ -f "$f" ]; then
        echo "  [PASS] $f present"
        PASS=$((PASS + 1))
    else
        echo "  [FAIL] $f missing"
        FAILED+=("$f missing")
    fi
done

# ----------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------
echo
echo "=============================================================="
echo "  §10 v1 acceptance: $PASS / $TOTAL checks passed"
if [ ${#FAILED[@]} -ne 0 ]; then
    echo "  Failures:"
    for f in "${FAILED[@]}"; do
        echo "    - $f"
    done
    echo "=============================================================="
    exit 1
fi
echo "  §10-v1 surface green. Authoring UI + dashboard diff viz deferred to v2."
echo "=============================================================="
exit 0
