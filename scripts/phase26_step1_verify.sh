#!/usr/bin/env bash
# =============================================================================
# scripts/phase26_step1_verify.sh
#
# Phase 26 Step 1 — proactive-insights gate + two test corrections.
#
#   1. orchestrator.py detects [PRE-COMPUTED SUMMARY] marker
#   2. orchestrator.py skips insights append on factoid responses
#   3. gq-005 expects "20" (post-Phase-17 fixture truth), not the stale "10"
#   4. gq-020 must_not_contain switched from "0" to "zero" (avoiding the
#      PLS-22-10 substring trap)
#   5. Cold-run golden peak ≥ 26 (above Phase 25's 25)
#   6. Cold/warm parity within ±2
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
ORCH="$REPO/src/fastapi/app/agent/orchestrator.py"
TESTS="$REPO/src/fastapi/tests/test_golden_queries.py"

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
PHASE 26 STEP 1 — factoid insights gate + test corrections
============================================================
BANNER

if grep -q 'is_factoid = "\[PRE-COMPUTED SUMMARY\]" in llm_text' "$ORCH"; then
    check "orchestrator.py detects [PRE-COMPUTED SUMMARY] marker" ok
else
    check "marker detection" fail "missing"
fi

if grep -q 'not _is_refusal(llm_text) and not is_factoid' "$ORCH" \
   && grep -q 'R-P26-FACTOID-INSIGHTS' "$ORCH"; then
    check "Insights append gated on factoid marker" ok
else
    check "gate logic" fail "missing"
fi

if grep -qE 'expected_answer_contains.*"20", "diamond"' "$TESTS"; then
    check "gq-005 expects \"20\" (fixture truth)" ok
else
    check "gq-005 fix" fail "still 10 or missing"
fi

if grep -qE 'must_not_contain.*"zero", "none", "no holes"' "$TESTS"; then
    check "gq-020 must_not_contain uses \"zero\" (not the digit trap)" ok
else
    check "gq-020 fix" fail "still 0 or missing"
fi

# Cold + warm pass
docker restart georag-fastapi >/dev/null 2>&1
sleep 90
cold=$(docker exec georag-fastapi pytest --tb=no -q /app/tests/test_golden_queries.py 2>&1 | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')
warm=$(docker exec georag-fastapi pytest --tb=no -q /app/tests/test_golden_queries.py 2>&1 | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')
delta=$(( cold > warm ? cold - warm : warm - cold ))

peak=$(( cold > warm ? cold : warm ))
if [ "${peak:-0}" -ge 26 ] 2>/dev/null; then
    check "Cold/warm peak ≥ 26 (cold=$cold warm=$warm peak=$peak; Phase 25 was 25)" ok
else
    check "peak" fail "cold=$cold warm=$warm peak=$peak"
fi

if [ "${delta:-99}" -le 2 ] 2>/dev/null && [ "${warm:-0}" -ge 24 ] 2>/dev/null; then
    check "Cold/warm within ±2 (cold=$cold warm=$warm)" ok
else
    check "parity" fail "cold=$cold warm=$warm delta=$delta"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
