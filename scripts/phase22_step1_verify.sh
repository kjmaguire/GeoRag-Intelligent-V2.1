#!/usr/bin/env bash
# =============================================================================
# scripts/phase22_step1_verify.sh
#
# Phase 22 Step 1 — agent prompt + confidence-scoring tweaks
# (R-P20-PROMPT + R-P20-CONFIDENCE).
#
#   1. _SYSTEM_PROMPT_VERSION bumped to ≥10
#   2. GRAPH prompt (dash variant) coaches matched-entity property surfacing
#   3. GRAPH prompt (colon variant) coaches matched-entity property surfacing
#   4. GRAPH prompt includes the "What type of deposit is the Triple R?" example
#   5. _compute_confidence excludes zero-relevance tools from the average
#   6. Cold-run golden pass count ≥ 22 (vs Phase 21's 20)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
ORCH="$REPO/src/fastapi/app/agent/orchestrator.py"
RA="$REPO/src/fastapi/app/agent/response_assembler.py"

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
PHASE 22 STEP 1 — graph prompt + confidence tweaks
============================================================
BANNER

# 1) Prompt version bumped
v=$(grep -E '^_SYSTEM_PROMPT_VERSION\s*=' "$ORCH" | head -1 | awk '{print $3}')
if [ "${v:-0}" -ge 10 ] 2>/dev/null; then
    check "_SYSTEM_PROMPT_VERSION bumped to $v" ok
else
    check "prompt version" fail "got $v"
fi

# Phase 22's coaching text now lives in the Phase 34/35-migrated
# graph prompt modules. Build a union of (orchestrator + dash module
# + colon module) and assert on the union — same supersession-tolerant
# pattern used by phase14_step1 / phase15_step2 verifiers post-R-P15-1.
PROMPTS_DIR="${REPO:-/home/georag/projects/georag}/src/fastapi/app/agent/prompts"
GRAPH_DASH="$PROMPTS_DIR/orchestrator_graph_dash.py"
GRAPH_COLON="$PROMPTS_DIR/orchestrator_graph_colon.py"
GRAPH_SOURCES=("$ORCH")
[ -f "$GRAPH_DASH" ]  && GRAPH_SOURCES+=("$GRAPH_DASH")
[ -f "$GRAPH_COLON" ] && GRAPH_SOURCES+=("$GRAPH_COLON")

# 2) Dash GRAPH coaches matched-entity property surface — check the
# coaching trio (marker + property bag + VERBATIM) exists somewhere
# in the union (orch or the dash graph prompt module).
has_marker=0
has_propbag=0
has_verbatim=0
for f in "${GRAPH_SOURCES[@]}"; do
    grep -q '◉ (matched entity)' "$f" && has_marker=1
    grep -q 'property bag' "$f" && has_propbag=1
    grep -q 'VERBATIM' "$f" && has_verbatim=1
done
if [ "$has_marker" = "1" ] && [ "$has_propbag" = "1" ] && [ "$has_verbatim" = "1" ]; then
    check "GRAPH (dash) coaches matched-entity property surface (across orch+modules)" ok
else
    check "dash coaching" fail "marker=$has_marker propbag=$has_propbag verbatim=$has_verbatim"
fi

# 3) Colon variant has the same coaching — count markers across the
# union (each of dash+colon graph prompts should carry one).
n=0
for f in "${GRAPH_SOURCES[@]}"; do
    c=$(grep -c '◉ (matched entity)' "$f")
    n=$((n + c))
done
if [ "${n:-0}" -ge 2 ] 2>/dev/null; then
    check "GRAPH dash + colon both carry matched-entity coaching (markers=$n)" ok
else
    check "colon coaching" fail "only $n marker(s)"
fi

# 4) Triple R deposit-type example present in both graph variants.
n=0
for f in "${GRAPH_SOURCES[@]}"; do
    c=$(grep -c 'What type of deposit is the Triple R' "$f")
    n=$((n + c))
done
if [ "${n:-0}" -ge 2 ] 2>/dev/null; then
    check "GRAPH includes Triple R deposit-type example in both variants (count=$n across orch+modules)" ok
else
    check "example" fail "count=$n"
fi

# 5) confidence calc skips zeros
if grep -q 'non_zero = \[r for r in relevances if r > 0\]' "$RA" \
   && grep -q 'R-P20-CONFIDENCE' "$RA"; then
    check "_compute_confidence excludes zero-relevance tools" ok
else
    check "confidence fix" fail "non-zero filter missing"
fi

# 6) Cold-run pass ≥ 22
docker restart georag-fastapi >/dev/null 2>&1
sleep 90
out=$(docker exec georag-fastapi pytest --tb=no -q /app/tests/test_golden_queries.py 2>&1 | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')
if [ "${out:-0}" -ge 22 ] 2>/dev/null; then
    check "Cold-run golden ≥ 22 (got $out)" ok
else
    check "golden pass count" fail "got $out"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
