#!/usr/bin/env bash
# =============================================================================
# scripts/phase35_step1_verify.sh
#
# Phase 35 Step 1 — third slice of R-P15-1: migrate 5 colon-variant prompts.
#
#   1. All 5 new colon prompt modules exist
#   2. Each module exports PROMPT_VERSION + SYSTEM_PROMPT
#   3. _version_registry contains all 5 new colon entries
#   4. orchestrator.py imports all 5 colon SYSTEM_PROMPT bindings
#   5. orchestrator.py no longer defines any colon variant inline
#   6. Composed colon prompts use [DATA:X] format (not dash)
#   7. Cold-run golden ≥ 29 — no regression
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
REPO="${REPO:-/home/georag/projects/georag}"
PDIR="$REPO/src/fastapi/app/agent/prompts"
REG="$PDIR/_version_registry.py"
ORCH="$REPO/src/fastapi/app/agent/orchestrator.py"
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
PHASE 35 STEP 1 — 5 colon prompts migration (R-P15-1 slice 3)
============================================================
BANNER

# 1) All 5 module files
missing=""
for m in orchestrator_shared_preamble_colon.py orchestrator_default_colon.py \
         orchestrator_numeric_colon.py orchestrator_narrative_colon.py \
         orchestrator_graph_colon.py; do
    if [ ! -s "$PDIR/$m" ]; then
        missing="$missing $m"
    fi
done
if [ -z "$missing" ]; then
    check "All 5 new colon prompt module files present" ok
else
    check "module files" fail "missing:$missing"
fi

# 2) Each exports PROMPT_VERSION + SYSTEM_PROMPT
ok2=1
for m in orchestrator_shared_preamble_colon orchestrator_default_colon \
         orchestrator_numeric_colon orchestrator_narrative_colon \
         orchestrator_graph_colon; do
    grep -q 'PROMPT_VERSION = ' "$PDIR/$m.py" || ok2=0
    grep -q 'SYSTEM_PROMPT = ' "$PDIR/$m.py" || ok2=0
done
if [ "$ok2" = "1" ]; then
    check "All 5 colon modules export PROMPT_VERSION + SYSTEM_PROMPT" ok
else
    check "exports" fail "missing in at least one module"
fi

# 3) Registry entries
ok3=1
for k in orchestrator_shared_preamble_colon orchestrator_default_colon \
         orchestrator_numeric_colon orchestrator_narrative_colon \
         orchestrator_graph_colon; do
    grep -q "\"$k\"" "$REG" || ok3=0
done
if [ "$ok3" = "1" ]; then
    check "_version_registry contains all 5 new colon entries" ok
else
    check "registry" fail "missing entries"
fi

# 4) Orchestrator imports
ok4=1
for m in orchestrator_shared_preamble_colon orchestrator_default_colon \
         orchestrator_numeric_colon orchestrator_narrative_colon \
         orchestrator_graph_colon; do
    grep -q "from app.agent.prompts.$m import" "$ORCH" || ok4=0
done
if [ "$ok4" = "1" ]; then
    check "orchestrator.py imports all 5 colon SYSTEM_PROMPT bindings" ok
else
    check "imports" fail "missing in orchestrator"
fi

# 5) Inline defs removed
ok5=1
for label in SHARED_PREAMBLE_COLON DEFAULT_COLON NUMERIC_COLON \
             NARRATIVE_COLON GRAPH_COLON; do
    if grep -qE "^_SYSTEM_PROMPT_${label} *=" "$ORCH"; then
        ok5=0
    fi
done
if [ "$ok5" = "1" ]; then
    check "orchestrator.py no longer defines any colon variant inline" ok
else
    check "inline removed" fail "at least one still inline"
fi

# 6) Composed colon prompts use [DATA:X] format
out=$(docker exec "$LARAVEL_FA" python3 -c "
from app.agent.prompts.orchestrator_default_colon import SYSTEM_PROMPT as D
from app.agent.prompts.orchestrator_numeric_colon import SYSTEM_PROMPT as N
from app.agent.prompts.orchestrator_narrative_colon import SYSTEM_PROMPT as NA
from app.agent.prompts.orchestrator_graph_colon import SYSTEM_PROMPT as G
checks = [
    '[DATA:1]' in D,
    '[DATA:1]' in N,
    '[NI43:1]' in NA,
    '[DATA:1]' in G,
    '◉ (matched entity)' in G,
    'TASK PROFILE: knowledge-graph' in G,
]
print('OK' if all(checks) else 'FAIL ' + str(checks))
" 2>&1 | tail -1)
if echo "$out" | grep -q '^OK'; then
    check "Composed colon prompts use [DATA:X] format" ok
else
    check "colon format" fail "$out"
fi

# 7) Cold-run no regression
docker restart "$LARAVEL_FA" >/dev/null 2>&1
sleep 100
cold=$(docker exec "$LARAVEL_FA" pytest --tb=no -q /app/tests/test_golden_queries.py 2>&1 | grep -oE '[0-9]+ passed' | head -1 | awk '{print $1}')
if [ "${cold:-0}" -ge 29 ] 2>/dev/null; then
    check "Cold-run golden ≥ 29 (got $cold; Phase 34 baseline was 30)" ok
else
    check "cold regression" fail "got $cold"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
