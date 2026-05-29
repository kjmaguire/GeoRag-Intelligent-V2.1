#!/usr/bin/env bash
# Master-plan §3 Step 7 part C verifier (doc-phase 57).
#
# Asserts the Hatchet ingest_pdf dual-write wiring:
#   1. _ingest_helper module imports + run_p04p_for_ingest is async
#   2. ingest_pdf module still imports cleanly (the parse + persist
#      step contracts intact)
#   3. _ingest_helper behaviour tests pass (happy path + 2 failure
#      modes)
#   4. Adjacent existing tests still pass (no regression in the
#      broader FastAPI test suite touched by the import graph)
#   5. Steps 1-7b verifiers still green

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
# Check 1 — _ingest_helper module imports + signature
# ----------------------------------------------------------------------
if docker exec "$CONTAINER" python -c "
import inspect
from app.ocr._ingest_helper import run_p04p_for_ingest
assert inspect.iscoroutinefunction(run_p04p_for_ingest)
" >/dev/null 2>&1; then
    note "[check1] PASS — _ingest_helper imports + run_p04p_for_ingest is async"
else
    note "[check1] FAIL — _ingest_helper import or signature check failed"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 2 — ingest_pdf module imports + IngestPdfFinalOut has p04p_telemetry
# ----------------------------------------------------------------------
if docker exec "$CONTAINER" python -c "
from app.hatchet_workflows.ingest_pdf import (
    ingest_pdf, IngestPdfInput, IngestPdfFinalOut, ParseOut
)
assert 'p04p_telemetry' in IngestPdfFinalOut.model_fields, \
    'IngestPdfFinalOut missing p04p_telemetry field'
" >/dev/null 2>&1; then
    note "[check2] PASS — ingest_pdf imports + IngestPdfFinalOut has p04p_telemetry"
else
    note "[check2] FAIL — ingest_pdf module import or contract check failed"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 3 — ingest helper behaviour tests
# ----------------------------------------------------------------------
if docker exec "$CONTAINER" python -m pytest \
     tests/test_ocr_ingest_helper.py \
     --tb=line -q 2>/dev/null \
     | grep -qE "3 passed"; then
    note "[check3] PASS — 3/3 ingest helper tests green"
else
    note "[check3] FAIL — ingest helper tests failing"
    docker exec "$CONTAINER" python -m pytest \
        tests/test_ocr_ingest_helper.py --tb=short -q 2>&1 | tail -20 || true
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 4 — adjacent existing tests still pass (sanity regression check)
# ----------------------------------------------------------------------
if docker exec "$CONTAINER" python -m pytest \
     tests/test_acquire_scoped.py tests/test_agent_tools.py \
     --tb=line -q 2>/dev/null \
     | grep -qE "25 passed"; then
    note "[check4] PASS — 25/25 adjacent existing tests still green"
else
    note "[check4] FAIL — adjacent tests regressed"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 5-11 — Steps 1-7b verifiers still green
# ----------------------------------------------------------------------
for step in 1 2 3 4 5 6 7a 7b; do
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
echo "=== Phase 3 master-plan Step 7c verifier summary ==="
echo "  (12 checks total; all must pass)"

# Doc-phase 62 — record success in the cascade manifest.
if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "step7c"
fi

exit $FAIL
