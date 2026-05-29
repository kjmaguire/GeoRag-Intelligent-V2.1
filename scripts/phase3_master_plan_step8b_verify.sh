#!/usr/bin/env bash
# Master-plan §3 Step 8 part B verifier (doc-phase 59).
#
# Step 8 is split:
#   - 8a: queue list scaffold (doc-phase 58) ✓
#   - 8b (THIS): FastAPI render endpoint + bronze-key tracking
#   - 8c: React detail panel + disposition controls (doc-phase 60)
#
# Asserts:
#   1. /internal/v1/ocr/render route is registered in FastAPI
#   2. ocr_render router imports cleanly + has the X-Service-Key gate
#   3. _persist.persist_orchestrator_result accepts bronze_s3_key kwarg
#   4. _ingest_helper.run_p04p_for_ingest accepts bronze_s3_key kwarg
#   5. ingest_pdf.persist threads input.minio_key into the helper call
#   6. Behaviour tests pass (auth + happy path + 404 paths)
#   7. Steps 1-8a verifiers still green

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
# Check 1 — render route registered in FastAPI's app
# ----------------------------------------------------------------------
if docker exec "$CONTAINER" python -c "
from app.main import app
routes = {r.path: r.methods for r in app.routes if hasattr(r, 'path')}
assert '/internal/v1/ocr/render' in routes, 'render route missing'
assert 'GET' in routes['/internal/v1/ocr/render'], 'render route not GET'
" >/dev/null 2>&1; then
    note "[check1] PASS — /internal/v1/ocr/render route registered (GET)"
else
    note "[check1] FAIL — render route not registered"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 2 — ocr_render module imports + service-key gate present
# ----------------------------------------------------------------------
if docker exec "$CONTAINER" python -c "
from app.routers.ocr_render import router, _check_service_key, _lookup_bronze_key
import inspect
assert inspect.iscoroutinefunction(_lookup_bronze_key)
" >/dev/null 2>&1; then
    note "[check2] PASS — ocr_render imports + has service-key gate + lookup helper"
else
    note "[check2] FAIL — ocr_render module import failed"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 3 — persist_orchestrator_result accepts bronze_s3_key kwarg
# ----------------------------------------------------------------------
if docker exec "$CONTAINER" python -c "
import inspect
from app.ocr._persist import persist_orchestrator_result
sig = inspect.signature(persist_orchestrator_result)
assert 'bronze_s3_key' in sig.parameters, 'bronze_s3_key kwarg missing'
" >/dev/null 2>&1; then
    note "[check3] PASS — persist_orchestrator_result has bronze_s3_key kwarg"
else
    note "[check3] FAIL — bronze_s3_key kwarg missing from persist"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 4 — run_p04p_for_ingest accepts bronze_s3_key kwarg
# ----------------------------------------------------------------------
if docker exec "$CONTAINER" python -c "
import inspect
from app.ocr._ingest_helper import run_p04p_for_ingest
sig = inspect.signature(run_p04p_for_ingest)
assert 'bronze_s3_key' in sig.parameters, 'bronze_s3_key kwarg missing'
" >/dev/null 2>&1; then
    note "[check4] PASS — run_p04p_for_ingest has bronze_s3_key kwarg"
else
    note "[check4] FAIL — bronze_s3_key kwarg missing from helper"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 5 — ingest_pdf.persist passes minio_key to helper
# ----------------------------------------------------------------------
if grep -q "bronze_s3_key=input.minio_key" \
     "$REPO_ROOT/src/fastapi/app/hatchet_workflows/ingest_pdf.py"; then
    note "[check5] PASS — ingest_pdf.persist passes input.minio_key as bronze_s3_key"
else
    note "[check5] FAIL — bronze_s3_key arg not wired in ingest_pdf"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 6 — render endpoint behaviour tests
# ----------------------------------------------------------------------
if docker exec "$CONTAINER" python -m pytest \
     tests/test_ocr_render_endpoint.py \
     --tb=line -q 2>/dev/null \
     | grep -qE "5 passed"; then
    note "[check6] PASS — 5/5 render endpoint behaviour tests green"
else
    note "[check6] FAIL — render endpoint tests failing"
    docker exec "$CONTAINER" python -m pytest \
        tests/test_ocr_render_endpoint.py --tb=short -q 2>&1 | tail -15 || true
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 7-15 — Steps 1-8a verifiers still green
# ----------------------------------------------------------------------
for step in 1 2 3 4 5 6 7a 7b 7c 8a; do
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
echo "=== Phase 3 master-plan Step 8b verifier summary ==="
echo "  (16 checks total; all must pass)"

# Doc-phase 62 — record success in the cascade manifest.
if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "step8b"
fi

exit $FAIL
