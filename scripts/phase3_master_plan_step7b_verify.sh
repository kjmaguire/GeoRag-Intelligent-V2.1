#!/usr/bin/env bash
# Master-plan §3 Step 7 part B verifier (doc-phase 56).
#
# Asserts the persistence layer writes orchestrator output to all
# 8 silver tables cleanly:
#   1. _persist module imports + transactional_workspace_session works
#   2. End-to-end integration test passes (orchestrator → persist →
#      verify silver rows)
#   3. Steps 1-7a verifiers still green (no regressions)

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
# Check 1 — _persist module imports
# ----------------------------------------------------------------------
if docker exec "$CONTAINER" python -c "
import inspect
from app.ocr._persist import (
    persist_orchestrator_result,
    transactional_workspace_session,
)
assert inspect.iscoroutinefunction(persist_orchestrator_result)
" >/dev/null 2>&1; then
    note "[check1] PASS — _persist module imports + has async function"
else
    note "[check1] FAIL — _persist import or signature check failed"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 2 — persistence integration tests
# ----------------------------------------------------------------------
if docker exec "$CONTAINER" python -m pytest \
     tests/test_ocr_persist_integration.py \
     --tb=line -q 2>/dev/null \
     | grep -qE "5 passed"; then
    note "[check2] PASS — 5/5 persistence integration tests green"
else
    note "[check2] FAIL — persistence tests failing"
    docker exec "$CONTAINER" python -m pytest \
        tests/test_ocr_persist_integration.py --tb=short -q 2>&1 | tail -20 || true
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 3-9 — Steps 1-7a verifiers still green
# ----------------------------------------------------------------------
for step in 1 2 3 4 5 6 7a; do
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
echo "=== Phase 3 master-plan Step 7b verifier summary ==="
echo "  (all checks must pass)"

# Doc-phase 62 — record success in the cascade manifest.
if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "step7b"
fi

exit $FAIL
