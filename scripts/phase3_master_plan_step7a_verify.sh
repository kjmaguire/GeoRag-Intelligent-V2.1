#!/usr/bin/env bash
# Master-plan §3 Step 7 part A verifier (doc-phase 55).
#
# This verifier covers the orchestrator only. The full Step 7 also
# includes persistence (doc-phase 56) and the Hatchet ingest_pdf
# rewrite (doc-phase 57); those have separate verifiers.
#
# Asserts:
#   1. _orchestrator module imports + orchestrate function is async
#   2. Orchestrator behaviour tests pass on the PLS-2024 native fixture
#   3. Steps 1-6 verifiers still green (no regressions)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

# Doc-phase 62 — manifest helper for cascade speedup
source "$SCRIPT_DIR/_verifier_manifest.sh"

CONTAINER="${CONTAINER:-georag-fastapi}"

FAIL=0
note() { echo "$1"; }

# ----------------------------------------------------------------------
# Check 1 — _orchestrator module imports + orchestrate async
# ----------------------------------------------------------------------
if docker exec "$CONTAINER" python -c "
import inspect
from app.ocr._orchestrator import orchestrate
assert inspect.iscoroutinefunction(orchestrate), 'orchestrate must be async'
" >/dev/null 2>&1; then
    note "[check1] PASS — _orchestrator.orchestrate is importable + async"
else
    note "[check1] FAIL — _orchestrator import or signature check failed"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 2 — orchestrator behaviour tests
# ----------------------------------------------------------------------
if docker exec "$CONTAINER" python -m pytest \
     tests/test_ocr_orchestrator.py \
     --tb=line -q 2>/dev/null \
     | grep -qE "9 passed"; then
    note "[check2] PASS — 9/9 orchestrator behaviour tests green"
else
    note "[check2] FAIL — orchestrator tests failing"
    docker exec "$CONTAINER" python -m pytest \
        tests/test_ocr_orchestrator.py --tb=short -q 2>&1 | tail -20 || true
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 3-8 — Steps 1-6 verifiers still green
# ----------------------------------------------------------------------
for step in 1 2 3 4 5 6; do
    if check_verifier_recent "step${step}"; then
        note "[step${step}] PASS — manifest recent (skip re-run)"
    elif bash "$SCRIPT_DIR/phase3_master_plan_step${step}_verify.sh" >/dev/null 2>&1; then
        note "[step${step}] PASS — verifier re-run green"
    else
        note "[step${step}] FAIL — verifier regressed"
        FAIL=$((FAIL + 1))
    fi
done

# ----------------------------------------------------------------------
# Aggregate
# ----------------------------------------------------------------------
echo ""
echo "=== Phase 3 master-plan Step 7a verifier summary ==="
echo "  $((8 - FAIL))/8 checks passed"

# Doc-phase 62 — record success in the cascade manifest.
if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "step7a"
fi

exit $FAIL
