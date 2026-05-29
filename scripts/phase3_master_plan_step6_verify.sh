#!/usr/bin/env bash
# Master-plan §3 Step 6 verifier (doc-phase 54).
#
# Asserts the LangGraph OCR Quality Graph (implemented as pure
# decision-tree function — see quality_graph.py docstring for the
# rationale) lands cleanly:
#   1. quality_graph module graduated (SKELETON_MODULES is empty)
#   2. Behaviour tests pass (route_page + summarize_document)
#   3. All prior step verifiers still green
#
# Pure-function tests have no I/O or model dependencies; sub-second runtime.

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
# Check 1 — quality_graph behaviour tests
# ----------------------------------------------------------------------
if ! docker inspect "$CONTAINER" >/dev/null 2>&1 \
   || [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" != "true" ]; then
    note "[check1] FAIL — container '$CONTAINER' not running"
    FAIL=$((FAIL + 1))
else
    if docker exec "$CONTAINER" python -m pytest \
         tests/test_ocr_quality_graph.py \
         --tb=line -q 2>/dev/null \
         | grep -qE "19 passed"; then
        note "[check1] PASS — 19/19 quality_graph tests green"
    else
        note "[check1] FAIL — quality_graph tests failing"
        docker exec "$CONTAINER" python -m pytest \
            tests/test_ocr_quality_graph.py --tb=short -q 2>&1 | tail -20 || true
        FAIL=$((FAIL + 1))
    fi
fi

# ----------------------------------------------------------------------
# Check 2 — SKELETON_MODULES is empty (all 8 modules graduated)
# ----------------------------------------------------------------------
SKELETON_FILE="$REPO_ROOT/src/fastapi/tests/test_ocr_module_imports.py"
SKELETON_CONTENT=$(awk '/^SKELETON_MODULES/{print}' "$SKELETON_FILE")
if echo "$SKELETON_CONTENT" | grep -qE 'SKELETON_MODULES.*=.*set\(\)'; then
    note "[check2] PASS — SKELETON_MODULES is empty (all 8 modules graduated)"
else
    note "[check2] FAIL — SKELETON_MODULES is not empty"
    echo "$SKELETON_CONTENT" >&2
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 3 — summarize_document exported from app.ocr top-level
# ----------------------------------------------------------------------
if docker exec "$CONTAINER" python -c "
from app.ocr import summarize_document, route_page
assert callable(summarize_document)
assert callable(route_page)
" >/dev/null 2>&1; then
    note "[check3] PASS — route_page + summarize_document exported from app.ocr"
else
    note "[check3] FAIL — top-level exports missing"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 4-8 — Step 1-5 verifiers still green
# ----------------------------------------------------------------------
for step in 1 2 3 4 5; do
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
echo "=== Phase 3 master-plan Step 6 verifier summary ==="
echo "  $((8 - FAIL))/8 checks passed"

# Doc-phase 62 — record success in the cascade manifest.
if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "step6"
fi

exit $FAIL
