#!/usr/bin/env bash
# Master-plan §3 Step 3 verifier (doc-phase 51).
#
# Asserts the native parser path implementations land cleanly:
#   1. preflight / profile / parse_native modules are no longer skeletons
#   2. Behaviour tests pass against the committed PLS-2024 fixture
#   3. Step 1 + Step 2 verifiers still green (no regressions)
#
# This is a thin orchestrator over pytest — the actual assertions live
# in tests/test_ocr_native_path.py + the prior step verifiers.

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
# Check 1 — native path behaviour tests
# ----------------------------------------------------------------------
if ! docker inspect "$CONTAINER" >/dev/null 2>&1 \
   || [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" != "true" ]; then
    note "[check1] FAIL — container '$CONTAINER' not running"
    FAIL=$((FAIL + 1))
else
    if docker exec "$CONTAINER" python -m pytest \
         tests/test_ocr_native_path.py \
         --tb=line -q 2>/dev/null \
         | grep -qE "8 passed"; then
        note "[check1] PASS — 8/8 native path behaviour tests green"
    else
        note "[check1] FAIL — native path behaviour tests failing"
        docker exec "$CONTAINER" python -m pytest \
            tests/test_ocr_native_path.py --tb=short -q 2>&1 | tail -20 || true
        FAIL=$((FAIL + 1))
    fi
fi

# ----------------------------------------------------------------------
# Check 2 — graduated modules removed from SKELETON_MODULES
# ----------------------------------------------------------------------
GRADUATED_EXPECTED="app.ocr.preflight app.ocr.profile app.ocr.parse_native"
SKELETON_FILE="$REPO_ROOT/src/fastapi/tests/test_ocr_module_imports.py"

bad=""
for mod in $GRADUATED_EXPECTED; do
    # SKELETON_MODULES set is a Python literal; grep for the module name
    # appearing as a quoted string inside the set definition.
    if awk '/^SKELETON_MODULES = \{/,/^\}/' "$SKELETON_FILE" \
         | grep -q "\"$mod\""; then
        bad="$bad $mod"
    fi
done
if [ -z "$bad" ]; then
    note "[check2] PASS — preflight/profile/parse_native removed from SKELETON_MODULES"
else
    note "[check2] FAIL — still listed as skeleton:$bad"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 3-4 — Steps 1-2 verifiers still green (manifest-cached cascade)
# ----------------------------------------------------------------------
for step in 1 2; do
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
echo "=== Phase 3 master-plan Step 3 verifier summary ==="
echo "  $((4 - FAIL))/4 checks passed"

# Doc-phase 62 — record success in the cascade manifest.
if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "step3"
fi

exit $FAIL
