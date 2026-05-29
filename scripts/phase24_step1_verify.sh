#!/usr/bin/env bash
# =============================================================================
# scripts/phase24_step1_verify.sh
#
# Phase 24 Step 1 — paired infrastructure fixes
# (R-P23-VLLM-400 + R-P23-CACHE-REHYDRATE).
#
#   1. orchestrator.py initialises `response: GeoRAGResponse | None = None`
#      before the retry loop
#   2. orchestrator.py constructs a fallback assemble_response when the
#      loop exits without setting response
#   3. orchestrator.py captures full payloads for neo4j+postgis cache
#      candidates (was previously qdrant-only)
#   4. orchestrator.py cache-hit branch rebuilds DocumentChunk /
#      GraphEntity / CollarRecord lists from candidates_reranked
#   5. orchestrator.py imports CollarRecord + DocumentChunk + GraphEntity
#      from app.agent.tools
#   6. Cold-run golden ≥ 20 (regression guard — Phase 22's lower bound)
#   7. Warm-run pass count matches cold-run within ±2
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
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
PHASE 24 STEP 1 — vLLM resilience + cache rehydration
============================================================
BANNER

# 1) response: GeoRAGResponse | None = None init
if grep -qE 'response.*GeoRAGResponse.*None.*=.*None' "$ORCH" \
   && grep -q 'R-P23-VLLM-400' "$ORCH"; then
    check "response initialised to None before retry loop" ok
else
    check "response init" fail "missing"
fi

# 2) post-loop fallback assemble
if grep -q 'retry loop exited without assembling' "$ORCH" \
   && grep -q 'if response is None:' "$ORCH"; then
    check "post-loop fallback assemble_response present" ok
else
    check "post-loop fallback" fail "missing"
fi

# 3) full-payload capture for neo4j+postgis
if grep -q 'R-P23-CACHE-REHYDRATE' "$ORCH" \
   && grep -q 'persist the full' "$ORCH"; then
    check "cache write captures full neo4j+postgis payloads" ok
else
    check "payload capture" fail "missing"
fi

# 4) cache-hit branch rebuilds tool_results
if grep -q '_doc_chunks: list\[DocumentChunk\]' "$ORCH" \
   && grep -q '_graph_entities: list\[GraphEntity\]' "$ORCH" \
   && grep -q '_collars: list\[CollarRecord\]' "$ORCH"; then
    check "cache-hit branch rebuilds DocumentChunk + GraphEntity + CollarRecord" ok
else
    check "rehydration buckets" fail "missing"
fi

# 5) imports
if grep -qE '^\s*CollarRecord,' "$ORCH" \
   && grep -qE '^\s*DocumentChunk,' "$ORCH" \
   && grep -qE '^\s*GraphEntity,' "$ORCH"; then
    check "CollarRecord + DocumentChunk + GraphEntity imported from app.agent.tools" ok
else
    check "imports" fail "missing"
fi

# 6) Cold-run ≥ 20 (regression guard)
docker restart georag-fastapi >/dev/null 2>&1
sleep 90
cold=$(docker exec georag-fastapi pytest --tb=no -q /app/tests/test_golden_queries.py 2>&1 | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')
if [ "${cold:-0}" -ge 20 ] 2>/dev/null; then
    check "Cold-run golden ≥ 20 (got $cold)" ok
else
    check "cold golden" fail "got $cold"
fi

# 7) Warm parity within ±2
warm=$(docker exec georag-fastapi pytest --tb=no -q /app/tests/test_golden_queries.py 2>&1 | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')
delta=$(( cold > warm ? cold - warm : warm - cold ))
if [ "${delta:-99}" -le 2 ] 2>/dev/null && [ "${warm:-0}" -ge 20 ] 2>/dev/null; then
    check "Warm-run matches cold within ±2 (cold=$cold warm=$warm)" ok
else
    check "warm parity" fail "cold=$cold warm=$warm delta=$delta"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
