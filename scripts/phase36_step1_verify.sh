#!/usr/bin/env bash
# =============================================================================
# scripts/phase36_step1_verify.sh
#
# Phase 36 Step 1 — fourth (final) slice of R-P15-1. Cleanup + audit closure.
#
#   1. Audit doc marked as RESOLVED at Phase 36 close
#   2. Audit doc lists the migration phase mapping (33-36)
#   3. _select_system_prompt docstring references the prompts/ tree
#   4. orchestrator.py imports all 10 prompt variants from prompts/
#   5. orchestrator.py defines zero remaining `_SYSTEM_PROMPT_*` triple-quoted blocks
#   6. _version_registry has ≥10 R-P15-1 entries (5 dash + 5 colon)
#   7. Cold-run golden ≥ 29 — no regression
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
REPO="${REPO:-/home/georag/projects/georag}"
AUDIT="$REPO/docs/phase15_orchestrator_prompts_audit.md"
ORCH="$REPO/src/fastapi/app/agent/orchestrator.py"
REG="$REPO/src/fastapi/app/agent/prompts/_version_registry.py"
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
PHASE 36 STEP 1 — R-P15-1 cleanup + audit closure (slice 4)
============================================================
BANNER

# 1) Audit doc marked RESOLVED
if grep -q 'RESOLVED at Phase 36 close' "$AUDIT"; then
    check "Audit doc marked RESOLVED at Phase 36 close" ok
else
    check "audit resolved" fail "marker missing"
fi

# 2) Audit lists migration phase mapping
if grep -q 'Phase 33: dash shared preamble' "$AUDIT" \
   && grep -q 'Phase 34: 4 dash task profiles' "$AUDIT" \
   && grep -q 'Phase 35: colon shared preamble' "$AUDIT" \
   && grep -q 'Phase 36:' "$AUDIT"; then
    check "Audit doc lists the migration phase mapping (33-36)" ok
else
    check "phase mapping" fail "incomplete"
fi

# 3) _select_system_prompt docstring references prompts/ tree
if grep -q 'imported from the .prompts/. tree' "$ORCH" \
   || grep -q 'prompts/orchestrator_.*_{dash,colon}.py' "$ORCH"; then
    check "_select_system_prompt docstring references the prompts/ tree" ok
else
    check "dispatch docstring" fail "doesn't mention prompts/ tree"
fi

# 4) Orchestrator imports all 10 prompt variants
ok4=1
for m in orchestrator_shared_preamble_dash orchestrator_default_dash \
         orchestrator_numeric_dash orchestrator_narrative_dash \
         orchestrator_graph_dash orchestrator_shared_preamble_colon \
         orchestrator_default_colon orchestrator_numeric_colon \
         orchestrator_narrative_colon orchestrator_graph_colon; do
    grep -q "from app.agent.prompts.$m import" "$ORCH" || ok4=0
done
if [ "$ok4" = "1" ]; then
    check "orchestrator.py imports all 10 prompt variants from prompts/" ok
else
    check "imports" fail "at least one missing"
fi

# 5) Zero remaining inline `_SYSTEM_PROMPT_*` triple-quoted blocks
inline_triple=$(grep -cE '^_SYSTEM_PROMPT_[A-Z_]+ *= *"""' "$ORCH" || true)
inline_concat=$(grep -cE '^_SYSTEM_PROMPT_[A-Z_]+ *= *_SYSTEM_PROMPT_SHARED_PREAMBLE.* \+ *"""' "$ORCH" || true)
inline_total=$((inline_triple + inline_concat))
if [ "${inline_total:-0}" = "0" ]; then
    check "orchestrator.py defines zero remaining _SYSTEM_PROMPT_* inline blocks" ok
else
    check "inline residue" fail "$inline_total still present"
fi

# 6) Registry has ≥10 R-P15-1 entries
r_count=$(grep -cE '"orchestrator_(shared_preamble|default|numeric|narrative|graph)_(dash|colon)"' "$REG" || true)
if [ "${r_count:-0}" -ge 10 ] 2>/dev/null; then
    check "_version_registry has all 10 R-P15-1 entries (got $r_count)" ok
else
    check "registry count" fail "got $r_count"
fi

# 7) Cold-run no regression
docker restart "$LARAVEL_FA" >/dev/null 2>&1
sleep 100
cold=$(docker exec "$LARAVEL_FA" pytest --tb=no -q /app/tests/test_golden_queries.py 2>&1 | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')
if [ "${cold:-0}" -ge 29 ] 2>/dev/null; then
    check "Cold-run golden ≥ 29 (got $cold; Phase 35 baseline was 30)" ok
else
    check "cold regression" fail "got $cold"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
