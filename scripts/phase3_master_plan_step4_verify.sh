#!/usr/bin/env bash
# Master-plan §3 Step 4 verifier (doc-phase 52).
#
# Asserts the scanned parser path (PaddleOCR PP-OCRv5 CPU image-input)
# and the render_page module land cleanly:
#   1. render + parse_scanned modules graduated from SKELETON_MODULES
#   2. Behaviour tests pass against the synthetic scanned fixture
#   3. Step 1-3 verifiers still green (no regressions)
#
# Note: this verifier takes ~100 sec on first run because PaddleOCR
# downloads ~50 MB of model weights to /tmp/.paddleocr/ on cold start.
# Subsequent runs are fast (models cached).

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
# Check 1 — scanned path behaviour tests
# ----------------------------------------------------------------------
if ! docker inspect "$CONTAINER" >/dev/null 2>&1 \
   || [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" != "true" ]; then
    note "[check1] FAIL — container '$CONTAINER' not running"
    FAIL=$((FAIL + 1))
else
    if docker exec "$CONTAINER" python -m pytest \
         tests/test_ocr_scanned_path.py \
         --tb=line -q 2>/dev/null \
         | grep -qE "7 passed"; then
        note "[check1] PASS — 7/7 scanned path behaviour tests green"
    else
        note "[check1] FAIL — scanned path behaviour tests failing"
        docker exec "$CONTAINER" python -m pytest \
            tests/test_ocr_scanned_path.py --tb=short -q 2>&1 | tail -20 || true
        FAIL=$((FAIL + 1))
    fi
fi

# ----------------------------------------------------------------------
# Check 2 — graduated modules removed from SKELETON_MODULES
# ----------------------------------------------------------------------
GRADUATED_EXPECTED="app.ocr.render app.ocr.parse_scanned"
SKELETON_FILE="$REPO_ROOT/src/fastapi/tests/test_ocr_module_imports.py"

bad=""
for mod in $GRADUATED_EXPECTED; do
    if awk '/^SKELETON_MODULES = \{/,/^\}/' "$SKELETON_FILE" \
         | grep -q "\"$mod\""; then
        bad="$bad $mod"
    fi
done
if [ -z "$bad" ]; then
    note "[check2] PASS — render + parse_scanned removed from SKELETON_MODULES"
else
    note "[check2] FAIL — still listed as skeleton:$bad"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 3-5 — Steps 1-3 verifiers still green (manifest-cached cascade)
# ----------------------------------------------------------------------
for step in 1 2 3; do
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
echo "=== Phase 3 master-plan Step 4 verifier summary ==="
echo "  $((5 - FAIL))/5 checks passed"

# Doc-phase 62 — record success in the cascade manifest.
if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "step4"
fi

exit $FAIL
