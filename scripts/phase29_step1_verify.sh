#!/usr/bin/env bash
# =============================================================================
# scripts/phase29_step1_verify.sh
#
# Phase 29 Step 1 — downhole cache bypass.
#
#   1. orchestrator.py bypasses cache shortcut on categories.downhole=True
#   2. orchestrator.py carries R-P28-VARIANCE marker
#   3. Cold-run golden ≥ 29 (gq-015 stable at peak; ±1 variance band tolerated)
#
# The three populate_neo4j.py checks that used to open this file were
# removed on 2026-09-15. Neo4j left the stack 2026-07-28 and the script
# they inspected does not exist anywhere in the tree, so all three could
# only ever report FAIL and took the whole verifier red with them — which
# is how a real regression in the downhole checks below would have gone
# unnoticed. The Report.title unique_title fix they guarded is moot: there
# is no graph to write a Report node into.
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=3
REPO="${REPO:-/home/georag/projects/georag}"
ORCH="$REPO/src/fastapi/app/agent/orchestrator.py"

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
PHASE 29 STEP 1 — downhole cache bypass
============================================================
BANNER

# Accept either the Phase 29 bypass OR the Phase 30 supersession.
# Phase 30 R-P29-DOWNHOLE-CACHE removed the bypass and wired
# DownholeLogsResult into the cache pipeline properly; the
# verifier now passes on either state so re-running Phase 29
# verification against a Phase 30+ tree doesn't false-fail.
if (grep -q 'if categories.get("downhole"):' "$ORCH" \
    && grep -q 'cache hit ignored' "$ORCH") \
   || grep -q 'Block E: cache-read rehydration\|Block A: RRF list\|R-P29-DOWNHOLE-CACHE' "$ORCH"; then
    check "orchestrator.py covers downhole — bypass (P29) OR cache pipeline (P30+)" ok
else
    check "downhole coverage" fail "neither P29 bypass nor P30 cache pipeline present"
fi

# Same supersession story for the R-P28-VARIANCE marker — Phase 30
# replaced its comment block, but the orchestrator may still carry
# the marker in adjacent comments or git history. Accept either
# the original marker or the Phase 30 R-P29-DOWNHOLE-CACHE marker
# (the work that obsoleted R-P28-VARIANCE).
if grep -qE 'R-P28-VARIANCE|R-P29-DOWNHOLE-CACHE' "$ORCH"; then
    check "orchestrator.py carries R-P28-VARIANCE or R-P29-DOWNHOLE-CACHE marker" ok
else
    check "variance/downhole marker" fail "neither marker present"
fi

# Cold-run golden ≥ 29
docker restart georag-fastapi >/dev/null 2>&1
sleep 100
cold=$(docker exec georag-fastapi pytest --tb=no -q /app/tests/test_golden_queries.py 2>&1 | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')
if [ "${cold:-0}" -ge 29 ] 2>/dev/null; then
    check "Cold-run golden ≥ 29 (got $cold; Phase 28 peak was 30)" ok
else
    check "cold peak" fail "got $cold"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
