#!/usr/bin/env bash
# =============================================================================
# scripts/phase21_step1_verify.sh
#
# Phase 21 Step 1 — warm-state cache poison fix (R-P14-3.7).
#
#   1. orchestrator.py guards cache write on _cached_candidates non-empty
#   2. orchestrator.py guards cache write on partial_failures empty
#   3. Skip-write log lines are present (zero candidates / partial_failures)
#   4. Live: a refusal-shaped empty result does NOT poison the rag_cache.
#      (Probe a guaranteed-empty query under a throwaway project_id; verify
#       no rag_cache key materialises afterwards.)
#   5. Live: warm-run golden pass count matches cold-run within tolerance ±1.
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=5
REPO="${REPO:-/home/georag/projects/georag}"
ORCH="$REPO/src/fastapi/app/agent/orchestrator.py"
REDIS=georag-redis
REDIS_PWD='N2Wz3FdVExUkEs8AysiAmh4usppA8FZ'

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
PHASE 21 STEP 1 — warm-state cache poison fix (R-P14-3.7)
============================================================
BANNER

# 1) Empty-candidates guard. The skip-write log message is split across
# adjacent Python string literals; check the source lines independently.
if grep -q 'if not _cached_candidates:' "$ORCH" \
   && grep -q 'zero candidates' "$ORCH"; then
    check "orchestrator.py guards on _cached_candidates non-empty" ok
else
    check "empty guard" fail "missing"
fi

# 2) Partial-failures guard
if grep -q 'elif partial_failures:' "$ORCH" \
   && grep -q 'partial_failures present' "$ORCH"; then
    check "orchestrator.py guards on partial_failures empty" ok
else
    check "failures guard" fail "missing"
fi

# 3) Both skip-write logs present
n=$(grep -cE 'skipping cache write' "$ORCH")
if [ "${n:-0}" -ge 2 ] 2>/dev/null; then
    check "Two skip-write log lines present (got $n)" ok
else
    check "log lines" fail "got $n"
fi

# 4) No rag_cache keys after probe of empty-result query.
# We synthesise a "deliberately uncommon" query that no fixture matches.
# After the run, no rag_cache key should be written.
before=$(docker exec "$REDIS" redis-cli -a "$REDIS_PWD" --scan --pattern 'georag:rag_cache:*' 2>/dev/null | wc -l)
docker exec georag-fastapi curl -s -X POST http://localhost:8000/internal/queries \
    -H 'Content-Type: application/json' \
    -d '{"query":"phase21_verifier_probe_'"$(date +%s)"'_no_match","project_id":"019d74a1-fba8-7165-9ae6-a5bf93eef97d"}' \
    --max-time 60 >/dev/null 2>&1 || true
sleep 2
after=$(docker exec "$REDIS" redis-cli -a "$REDIS_PWD" --scan --pattern 'georag:rag_cache:*' 2>/dev/null | wc -l)
delta=$((after - before))
if [ "$delta" -le 0 ] 2>/dev/null; then
    check "Empty-result query did not write a rag_cache key (delta=$delta)" ok
else
    check "no poison cache" fail "$delta new rag_cache key(s)"
fi

# 5) Warm-run pass count matches cold-run within ±1.
# Run pytest twice; if pass counts agree within 1, the warm-state collapse
# is fixed. We only run the structured-data subset for speed.
echo "  (running cold + warm pytest pair — ~3 min)"
docker restart georag-fastapi >/dev/null 2>&1
sleep 90
cold_pass=$(docker exec georag-fastapi pytest --tb=no -q /app/tests/test_golden_queries.py 2>&1 | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')
warm_pass=$(docker exec georag-fastapi pytest --tb=no -q /app/tests/test_golden_queries.py 2>&1 | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')
delta=$(( cold_pass > warm_pass ? cold_pass - warm_pass : warm_pass - cold_pass ))
if [ "${delta:-99}" -le 1 ] 2>/dev/null && [ "${cold_pass:-0}" -ge 10 ] 2>/dev/null; then
    check "Warm-run pass count matches cold-run within ±1 (cold=$cold_pass warm=$warm_pass)" ok
else
    check "warm/cold parity" fail "cold=$cold_pass warm=$warm_pass delta=$delta"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
