#!/usr/bin/env bash
# =============================================================================
# scripts/phase29_step1_verify.sh
#
# Phase 29 Step 1 — populate_neo4j Report.title fix + downhole cache bypass.
#
#   1. populate_neo4j.py uses report_id-suffixed unique_title
#   2. populate_neo4j.py carries R-P19-POPULATE marker
#   3. orchestrator.py bypasses cache shortcut on categories.downhole=True
#   4. orchestrator.py carries R-P28-VARIANCE marker
#   5. populate_neo4j.py runs end-to-end with no constraint violations
#   6. Cold-run golden ≥ 29 (gq-015 stable at peak; ±1 variance band tolerated)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
POP="$REPO/src/fastapi/scripts/populate_neo4j.py"
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
PHASE 29 STEP 1 — populate_neo4j fix + downhole cache bypass
============================================================
BANNER

if grep -q "unique_title = f\"{r\\['title'\\]} ({r\\['report_id'\\]\\[:8\\]})\"" "$POP"; then
    check "populate_neo4j.py uses report_id-suffixed unique_title" ok
else
    check "unique_title" fail "missing"
fi

if grep -q 'R-P19-POPULATE' "$POP"; then
    check "populate_neo4j.py carries R-P19-POPULATE marker" ok
else
    check "marker" fail "missing"
fi

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

# Run populate_neo4j.py — expect zero exceptions
out=$(docker exec -e DATABASE_URL="postgresql://georag_app:georag-app-dev-2026@pgbouncer:6432/georag" \
    georag-fastapi python /app/scripts/populate_neo4j.py 2>&1 | tail -5)
if echo "$out" | grep -q "Done\."; then
    check "populate_neo4j.py runs end-to-end ('Done.' emitted)" ok
else
    check "populate run" fail "did not emit Done. ($(echo "$out" | head -1))"
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
