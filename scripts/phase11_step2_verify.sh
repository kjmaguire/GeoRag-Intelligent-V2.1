#!/usr/bin/env bash
# =============================================================================
# scripts/phase11_step2_verify.sh
#
# Phase 11 Step 2 done-definition — golden-query test baseline.
#
#   1. docs/phase11_golden_baseline.md exists + non-trivial
#   2. Doc records both golden test files
#   3. Doc records the baseline pass count (lower bound)
#   4. Live pytest run still produces ≥ baseline passes
#      (regression guard — Phase 12+ shouldn't regress below the
#      Phase 11 baseline)
#   5. Both golden test files are pytest-collectible
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=5
REPO="${REPO:-/home/georag/projects/georag}"
DOC="$REPO/docs/phase11_golden_baseline.md"
LARAVEL_FA="${FASTAPI_CONTAINER:-georag-fastapi}"

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
PHASE 11 STEP 2 — GOLDEN-TEST BASELINE VERIFICATION
============================================================
BANNER

# 1) Doc exists + non-trivial
if [ -s "$DOC" ]; then
    lines=$(wc -l < "$DOC")
    if [ "$lines" -ge 50 ]; then
        check "Baseline doc present ($lines lines)" ok
    else
        check "doc length" fail "only $lines lines"
    fi
else
    check "doc exists" fail "missing"
fi

# 2) Doc references both test files
if grep -q 'test_golden_queries.py' "$DOC" \
    && grep -q 'test_public_geoscience_golden.py' "$DOC"; then
    check "Doc records both golden test files" ok
else
    check "test file refs" fail "missing one or both"
fi

# 3) Baseline lower-bound recorded
if grep -q 'Lower bound for Phase 12' "$DOC" \
    && grep -qE 'pass count must be . 2' "$DOC"; then
    check "Baseline lower bound (≥2 passes) recorded in doc" ok
else
    check "lower bound" fail "not recorded"
fi

# 4) Live pytest run still produces ≥2 passes
pytest_out=$(docker exec "$LARAVEL_FA" pytest --tb=no -q \
    /app/tests/test_golden_queries.py \
    /app/tests/test_public_geoscience_golden.py 2>&1 | tail -5)
echo "    pytest tail: $(echo "$pytest_out" | tr '\n' '|' | head -c 240)"
passed=$(echo "$pytest_out" | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')
if [ "${passed:-0}" -ge 2 ] 2>/dev/null; then
    check "Live golden run still produces ≥2 passes (got $passed)" ok
else
    check "live regression" fail "got $passed passed (expected ≥ 2)"
fi

# 5) Both files collectible
collected=$(docker exec "$LARAVEL_FA" pytest --collect-only -q \
    /app/tests/test_golden_queries.py \
    /app/tests/test_public_geoscience_golden.py 2>&1 \
    | grep -oE '[0-9]+ tests collected' | head -1 | awk '{print $1}')
if [ "${collected:-0}" -ge 30 ] 2>/dev/null; then
    check "Both golden files collectible by pytest ($collected tests)" ok
else
    check "collection" fail "got $collected collected"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
