#!/usr/bin/env bash
# =============================================================================
# scripts/phase13_step4_verify.sh
#
# Phase 13 Step 4 — golden-test baseline re-anchored.
#
#   1. Baseline doc records the Phase 13 peak observation
#   2. Doc mentions "+11 unlocked tests vs Phase 11 baseline"
#   3. Doc captures the Phase 13 carry-over for the intermittent
#      refusal path (R-P13-1)
#   4. The fixture row count still matches expectations (collars
#      didn't get wiped between Phase 13 Step 3 and now)
#   5. Live golden run still produces ≥ conservative floor (2 passes)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=5
REPO="${REPO:-/home/georag/projects/georag}"
BASELINE="$REPO/docs/phase11_golden_baseline.md"
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
PHASE 13 STEP 4 — GOLDEN BASELINE RE-ANCHOR
============================================================
BANNER

# 1) Phase 13 peak observation recorded
if grep -q 'Phase 13 peak: 13 passing' "$BASELINE"; then
    check "Baseline doc records the Phase 13 peak (13/35)" ok
else
    check "peak recorded" fail "missing"
fi

# 2) Improvement noted
if grep -q '+11 unlocked tests' "$BASELINE"; then
    check "Doc captures the +11-test unlock vs Phase 11 baseline" ok
else
    check "improvement note" fail "missing"
fi

# 3) Refusal-path investigation flagged as Phase 14 carry-over
if grep -q 'R-P13-1' "$BASELINE"; then
    check "Doc flags R-P13-1 (intermittent refusal investigation) for Phase 14" ok
else
    check "carry-over" fail "R-P13-1 not flagged"
fi

# 4) Fixture rows still in place
PROJ='019d74a1-fba8-7165-9ae6-a5bf93eef97d'
n_pls=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM silver.collars
     WHERE project_id = '$PROJ' AND hole_id LIKE 'PLS-%';" | tr -d ' ')
[ "$n_pls" = "10" ] \
    && check "PLS-* fixture still present (10 rows)" ok \
    || check "fixture intact" fail "got $n_pls"

# 5) Live regression — conservative floor at 2
pytest_out=$(docker exec "$LARAVEL_FA" pytest --tb=no -q \
    /app/tests/test_golden_queries.py \
    /app/tests/test_public_geoscience_golden.py 2>&1)
passed=$(echo "$pytest_out" | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')
echo "    live golden run: ${passed} passed"
if [ "${passed:-0}" -ge 2 ] 2>/dev/null; then
    check "Live golden run still produces ≥2 passes (got $passed)" ok
else
    check "regression" fail "got $passed < 2"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
