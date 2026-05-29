#!/usr/bin/env bash
# =============================================================================
# scripts/phase31_step1_verify.sh
#
# Phase 31 Step 1 — gq-006 stale-assertion fix (R-P31-STALE-AUDIT).
#
#   1. gq-006-completed-holes expects "19" (post-Phase-17 reality)
#   2. gq-006 must_not_contain switched from "10 completed" to "20 completed"
#   3. silver.collars actually has 19 Completed rows for the test project
#   4. gq-006 passes standalone against the updated assertion
#   5. Cold-run golden ≥ 29 — no regression vs Phase 30
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=5
REPO="${REPO:-/home/georag/projects/georag}"
TESTS="$REPO/src/fastapi/tests/test_golden_queries.py"
PG=georag-postgresql

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
PHASE 31 STEP 1 — gq-006 stale-assertion fix
============================================================
BANNER

# gq-006 spans ~15 source lines (id + query + expected_answer_contains +
# must_not_contain + ...). Extract the block via awk so the substring
# greps below are scoped to gq-006 only.
gq6_block=$(awk '/gq-006-completed-holes/,/^    },$/' "$TESTS")

# 1) "19" assertion (post-Phase-17 reality)
if echo "$gq6_block" | grep -q 'expected_answer_contains.*"19"'; then
    check "gq-006 expects \"19\" (post-Phase-17 Completed count)" ok
else
    check "gq-006 fix" fail "still \"9\" or missing"
fi

# 2) must_not_contain "20 completed"
if echo "$gq6_block" | grep -q 'must_not_contain.*"20 completed"'; then
    check "gq-006 must_not_contain switched from \"10 completed\" to \"20 completed\"" ok
else
    check "must_not" fail "not updated"
fi

# 3) Reality matches
n_completed=$(docker exec "$PG" psql -U georag -d georag -tAc \
    "SELECT count(*) FROM silver.collars WHERE project_id='019d74a1-fba8-7165-9ae6-a5bf93eef97d' AND status='Completed';" | tr -d ' ')
if [ "$n_completed" = "19" ]; then
    check "silver.collars has 19 Completed for test project (matches assertion)" ok
else
    check "data" fail "got $n_completed"
fi

# 4) gq-006 passes standalone
g6=$(docker exec georag-fastapi pytest --tb=no -q /app/tests/test_golden_queries.py -k gq-006 2>&1 | grep -oE '[0-9]+ passed' | head -1)
if echo "$g6" | grep -q '1 passed'; then
    check "gq-006-completed-holes passes standalone against new assertion" ok
else
    check "gq-006 standalone" fail "$g6"
fi

# 5) Cold-run no regression
docker restart georag-fastapi >/dev/null 2>&1
sleep 100
cold=$(docker exec georag-fastapi pytest --tb=no -q /app/tests/test_golden_queries.py 2>&1 | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')
# Phase 30 achieved 31/31 peak; gq-017 phrase-fragility means typical
# cold-run is 30/31. Threshold at 29 to give breathing room.
if [ "${cold:-0}" -ge 29 ] 2>/dev/null; then
    check "Cold-run golden ≥ 29 (got $cold; Phase 30 peak was 31, typical 30)" ok
else
    check "cold regression" fail "got $cold"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
