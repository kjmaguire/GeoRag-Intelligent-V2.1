#!/usr/bin/env bash
# =============================================================================
# scripts/phase11_step4_verify.sh
#
# Phase 11 Step 4 done-definition — golden-query smoke wired into
# the master sweep.
#
# Runs the existing golden suites end-to-end inside the fastapi
# container and asserts the pass count meets the Phase 11 baseline
# (≥2). This is the regression gate for Phase 12+: future commits
# can ADD passing tests, but must NEVER drop the pass count below
# the recorded floor.
#
#   1. Pytest collection succeeds (≥30 golden tests collected)
#   2. Pytest run completes within 5 minutes
#   3. Pass count meets the Phase 11 baseline (≥2)
#   4. No tests error out (errors are different from failures —
#      errors mean the test harness itself broke)
#   5. The baseline doc still records the floor
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=5
REPO="${REPO:-/home/georag/projects/georag}"
LARAVEL_FA="${FASTAPI_CONTAINER:-georag-fastapi}"
BASELINE_DOC="$REPO/docs/phase11_golden_baseline.md"
BASELINE_FLOOR=2

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
PHASE 11 STEP 4 — GOLDEN SMOKE IN MASTER SWEEP
============================================================
BANNER

# 1) Collection
collected=$(docker exec "$LARAVEL_FA" pytest --collect-only -q \
    /app/tests/test_golden_queries.py \
    /app/tests/test_public_geoscience_golden.py 2>&1 \
    | grep -oE '[0-9]+ tests collected' | head -1 | awk '{print $1}')
if [ "${collected:-0}" -ge 30 ] 2>/dev/null; then
    check "Pytest collects $collected golden tests" ok
else
    check "collection" fail "got $collected (expected ≥30)"
fi

# 2) Run within 5 minutes
start_ts=$(date +%s)
pytest_out=$(docker exec "$LARAVEL_FA" pytest --tb=no -q \
    /app/tests/test_golden_queries.py \
    /app/tests/test_public_geoscience_golden.py 2>&1)
end_ts=$(date +%s)
elapsed=$((end_ts - start_ts))
if [ "$elapsed" -le 300 ]; then
    check "Golden suite ran in ${elapsed}s (≤300s budget)" ok
else
    check "elapsed budget" fail "took ${elapsed}s"
fi

# 3) Pass count meets baseline
passed=$(echo "$pytest_out" | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')
if [ "${passed:-0}" -ge "$BASELINE_FLOOR" ] 2>/dev/null; then
    check "Pass count ≥ baseline floor (got $passed, floor $BASELINE_FLOOR)" ok
else
    check "regression" fail "got $passed < $BASELINE_FLOOR"
fi

# 4) No errors (different from failures — errors mean harness broke)
errors=$(echo "$pytest_out" | grep -oE '[0-9]+ error' | head -1 | awk '{print $1}')
if [ -z "$errors" ] || [ "$errors" = "0" ]; then
    check "No pytest errors (only legitimate test failures)" ok
else
    check "harness errors" fail "got $errors errors"
fi

# 5) Baseline doc still records the floor
if grep -qE 'pass count must be . 2' "$BASELINE_DOC"; then
    check "Baseline doc still records the ≥2 floor" ok
else
    check "baseline doc" fail "floor reference missing"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
