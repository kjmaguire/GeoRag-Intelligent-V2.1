#!/usr/bin/env bash
# Master-plan §3 Step 5 verifier (doc-phase 53).
#
# Asserts the Docling-backed mixed + table-heavy parser paths land
# cleanly:
#   1. parse_mixed + parse_table_heavy graduated from SKELETON_MODULES
#   2. Behaviour tests pass on the PLS-2024 native fixture
#   3. Step 1-4 verifiers still green (no regressions)
#
# Docling cold-load + per-page parse means this verifier takes
# ~30-60 sec wall on the 7-page PLS-2024 fixture.

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
# Check 1 — mixed + table-heavy path behaviour tests
# ----------------------------------------------------------------------
if ! docker inspect "$CONTAINER" >/dev/null 2>&1 \
   || [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" != "true" ]; then
    note "[check1] FAIL — container '$CONTAINER' not running"
    FAIL=$((FAIL + 1))
else
    if docker exec "$CONTAINER" python -m pytest \
         tests/test_ocr_mixed_path.py \
         --tb=line -q 2>/dev/null \
         | grep -qE "9 passed"; then
        note "[check1] PASS — 9/9 mixed + table-heavy path tests green"
    else
        note "[check1] FAIL — mixed/table-heavy path tests failing"
        docker exec "$CONTAINER" python -m pytest \
            tests/test_ocr_mixed_path.py --tb=short -q 2>&1 | tail -20 || true
        FAIL=$((FAIL + 1))
    fi
fi

# ----------------------------------------------------------------------
# Check 2 — graduated modules removed from SKELETON_MODULES
# ----------------------------------------------------------------------
GRADUATED_EXPECTED="app.ocr.parse_mixed app.ocr.parse_table_heavy"
SKELETON_FILE="$REPO_ROOT/src/fastapi/tests/test_ocr_module_imports.py"

bad=""
for mod in $GRADUATED_EXPECTED; do
    if awk '/^SKELETON_MODULES = \{/,/^\}/' "$SKELETON_FILE" \
         | grep -q "\"$mod\""; then
        bad="$bad $mod"
    fi
done
if [ -z "$bad" ]; then
    note "[check2] PASS — parse_mixed + parse_table_heavy removed from SKELETON_MODULES"
else
    note "[check2] FAIL — still listed as skeleton:$bad"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 3-6 — Step 1-4 verifiers still green (no regression)
# ----------------------------------------------------------------------
for step in 1 2 3 4; do
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
echo "=== Phase 3 master-plan Step 5 verifier summary ==="
echo "  $((6 - FAIL))/6 checks passed"

# Doc-phase 62 — record success in the cascade manifest.
if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "step5"
fi

exit $FAIL
