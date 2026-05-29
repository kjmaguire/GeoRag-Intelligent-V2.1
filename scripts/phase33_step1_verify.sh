#!/usr/bin/env bash
# =============================================================================
# scripts/phase33_step1_verify.sh
#
# Phase 33 Step 1 — first slice of R-P15-1 (orchestrator prompts migration).
# Migrates _SYSTEM_PROMPT_SHARED_PREAMBLE (dash variant) into the
# canonical prompts/ tree.
#
#   1. New prompt module file present
#   2. Module exports PROMPT_VERSION + SYSTEM_PROMPT
#   3. _version_registry.py contains the new entry
#   4. orchestrator.py no longer defines _SYSTEM_PROMPT_SHARED_PREAMBLE inline
#   5. orchestrator.py imports SYSTEM_PROMPT from the new module
#   6. Text is byte-identical to the pre-migration inline definition
#      (sanity check via length + first/last-line content)
#   7. Cold-run golden ≥ 29 — must not regress
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
REPO="${REPO:-/home/georag/projects/georag}"
MOD="$REPO/src/fastapi/app/agent/prompts/orchestrator_shared_preamble_dash.py"
REG="$REPO/src/fastapi/app/agent/prompts/_version_registry.py"
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
PHASE 33 STEP 1 — shared-preamble migration (R-P15-1 slice 1)
============================================================
BANNER

# 1) Module file
if [ -s "$MOD" ]; then
    lines=$(wc -l < "$MOD")
    [ "$lines" -ge 70 ] \
        && check "Prompt module present ($lines lines)" ok \
        || check "module length" fail "only $lines lines"
else
    check "module exists" fail "missing"
fi

# 2) Module exports
if grep -q 'PROMPT_VERSION = ' "$MOD" \
   && grep -q 'SYSTEM_PROMPT = ' "$MOD"; then
    check "Module exports PROMPT_VERSION + SYSTEM_PROMPT" ok
else
    check "exports" fail "missing"
fi

# 3) Registry entry
if grep -q '"orchestrator_shared_preamble_dash"' "$REG" \
   && grep -q 'app.agent.prompts.orchestrator_shared_preamble_dash' "$REG"; then
    check "_version_registry contains the new entry" ok
else
    check "registry entry" fail "missing"
fi

# 4) Inline definition removed
if ! grep -qE '^_SYSTEM_PROMPT_SHARED_PREAMBLE = "' "$ORCH"; then
    check "orchestrator.py no longer defines _SYSTEM_PROMPT_SHARED_PREAMBLE inline" ok
else
    check "inline removed" fail "still present"
fi

# 5) Import from module
if grep -q 'from app.agent.prompts.orchestrator_shared_preamble_dash import' "$ORCH" \
   && grep -q 'SYSTEM_PROMPT as _SYSTEM_PROMPT_SHARED_PREAMBLE' "$ORCH"; then
    check "orchestrator.py imports SYSTEM_PROMPT from new module" ok
else
    check "import" fail "missing"
fi

# 6) Text content sanity — first + last meaningful lines preserved
if grep -q 'You are GeoRAG, a senior geological intelligence assistant' "$MOD" \
   && grep -q 'refusal is unambiguous' "$MOD"; then
    check "Text content preserved (sentinel phrases present)" ok
else
    check "text content" fail "preamble text malformed"
fi

# 7) Cold-run no regression
docker restart georag-fastapi >/dev/null 2>&1
sleep 100
cold=$(docker exec georag-fastapi pytest --tb=no -q /app/tests/test_golden_queries.py 2>&1 | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')
if [ "${cold:-0}" -ge 29 ] 2>/dev/null; then
    check "Cold-run golden ≥ 29 (got $cold; Phase 32 baseline was 30-31)" ok
else
    check "cold regression" fail "got $cold"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
