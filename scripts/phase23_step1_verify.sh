#!/usr/bin/env bash
# =============================================================================
# scripts/phase23_step1_verify.sh
#
# Phase 23 Step 1 — cache rehydration investigation deliverables.
#
#   1. Investigation doc present + non-trivial
#   2. Doc identifies the missing candidates_reranked rehydration
#   3. Doc identifies vLLM 400 + UnboundLocalError cascade
#   4. Doc proposes paired Fix A + Fix B for Phase 24
#   5. Handoff doc lists R-P23-CACHE-REHYDRATE and R-P23-VLLM-400 carry-overs
#   6. No code changes shipped — orchestrator cache-hit logic still sets
#      _cache_hit=True on read (the original Phase 22 baseline)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
INV="$REPO/docs/phase23_cache_rehydration_investigation.md"
HANDOFF="$REPO/docs/phase23_handoff.md"
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
PHASE 23 STEP 1 — cache rehydration investigation
============================================================
BANNER

# 1) Investigation doc present + non-trivial
if [ -s "$INV" ]; then
    lines=$(wc -l < "$INV")
    [ "$lines" -ge 120 ] \
        && check "Investigation doc present ($lines lines)" ok \
        || check "doc length" fail "only $lines lines"
else
    check "doc exists" fail "missing"
fi

# 2) Missing rehydration identified
if grep -q 'candidates_reranked.*never read\|never built\|rehydration is unimplemented\|unimplemented' "$INV"; then
    check "Doc identifies missing candidates_reranked rehydration" ok
else
    check "rehydration finding" fail "phrase missing"
fi

# 3) vLLM 400 + UnboundLocalError documented
if grep -q '400' "$INV" && grep -q 'UnboundLocalError' "$INV"; then
    check "Doc identifies vLLM 400 + UnboundLocalError cascade" ok
else
    check "vLLM cascade" fail "phrase missing"
fi

# 4) Fix A + Fix B proposed
if grep -q 'Fix A' "$INV" && grep -q 'Fix B' "$INV"; then
    check "Doc proposes paired Fix A + Fix B for Phase 24" ok
else
    check "fix proposals" fail "Fix A or Fix B not named"
fi

# 5) Handoff carry-over names
if grep -q 'R-P23-CACHE-REHYDRATE' "$HANDOFF" && grep -q 'R-P23-VLLM-400' "$HANDOFF"; then
    check "Handoff lists R-P23-CACHE-REHYDRATE + R-P23-VLLM-400 carry-overs" ok
else
    check "carry-over names" fail "missing"
fi

# 6) No code change in orchestrator — _cache_hit = True still set on read.
# We grep for the original line that the reverted change had removed.
if grep -q '_cache_hit = True' "$ORCH"; then
    check "orchestrator cache-hit logic restored to Phase 22 baseline" ok
else
    check "code revert" fail "_cache_hit = True not set on cache read"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
