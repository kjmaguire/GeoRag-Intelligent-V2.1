#!/usr/bin/env bash
# Master-plan §3 Step 8 part E verifier (doc-phase 63).
#
# Step 8e wires the re-OCR auto-trigger: Laravel disposition update
# → POST /internal/v1/re_ocr_page/trigger → Hatchet re_ocr_page
# workflow → escalated parse_scanned + persist new silver rows.
#
# Asserts:
#   1. re_ocr_page Hatchet workflow imports + ReOcrPageInput is a
#      Pydantic model
#   2. re_ocr_page is registered in the worker.py POOLS dict (ingestion)
#   3. /internal/v1/re_ocr_page/trigger route is registered in FastAPI
#   4. IngestionReviewController has dispatchReOcr() method
#   5. Import-boundary lint clean (hatchet_workflows/ broadly allowed)
#   6. Steps 1-8d cascade still green (manifest fast)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

# Doc-phase 62 — manifest helper for cascade speedup
source "$SCRIPT_DIR/_verifier_manifest.sh"

FASTAPI_CONTAINER="${FASTAPI_CONTAINER:-georag-fastapi}"
LARAVEL_CONTAINER="${LARAVEL_CONTAINER:-georag-laravel-octane}"

FAIL=0
note() { echo "$1"; }

# ----------------------------------------------------------------------
# Check 1 — re_ocr_page workflow + Pydantic models load
# ----------------------------------------------------------------------
if docker exec "$FASTAPI_CONTAINER" python -c "
from app.hatchet_workflows.re_ocr_page import (
    re_ocr_page, ReOcrPageInput, ReOcrPageOutput,
)
assert hasattr(re_ocr_page, 'aio_run_no_wait'), 'workflow missing aio_run_no_wait'
assert ReOcrPageInput.__name__ == 'ReOcrPageInput'
" >/dev/null 2>&1; then
    note "[check1] PASS — re_ocr_page workflow + input/output models load"
else
    note "[check1] FAIL — re_ocr_page module import failed"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 2 — re_ocr_page registered in worker POOLS
# ----------------------------------------------------------------------
if grep -q "re_ocr_page" "$REPO_ROOT/src/fastapi/app/hatchet_workflows/worker.py"; then
    note "[check2] PASS — re_ocr_page imported in worker.py POOLS"
else
    note "[check2] FAIL — worker.py does not reference re_ocr_page"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 3 — /internal/v1/re_ocr_page/trigger route registered
# ----------------------------------------------------------------------
if docker exec "$FASTAPI_CONTAINER" python -c "
from app.main import app
routes = {r.path for r in app.routes if hasattr(r, 'path')}
assert '/internal/v1/re_ocr_page/trigger' in routes
" >/dev/null 2>&1; then
    note "[check3] PASS — /internal/v1/re_ocr_page/trigger route registered"
else
    note "[check3] FAIL — trigger route not registered"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 4 — IngestionReviewController has dispatchReOcr() method
# ----------------------------------------------------------------------
if docker exec "$LARAVEL_CONTAINER" php -r "
require '/app/vendor/autoload.php';
\$app = require '/app/bootstrap/app.php';
\$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
\$rc = new ReflectionClass(App\Http\Controllers\Admin\IngestionReviewController::class);
if (!\$rc->hasMethod('dispatchReOcr')) { echo 'no'; exit(1); }
echo 'ok';
" 2>/dev/null | grep -q "ok"; then
    note "[check4] PASS — IngestionReviewController has dispatchReOcr()"
else
    note "[check4] FAIL — dispatchReOcr() method missing"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 5 — Import-boundary lint clean
# ----------------------------------------------------------------------
if bash "$SCRIPT_DIR/phase3_master_plan_step1_import_boundary.sh" >/dev/null 2>&1; then
    note "[check5] PASS — import-boundary lint clean"
else
    note "[check5] FAIL — import-boundary lint flagged a violation"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 6+ — Steps 1-8d still green (manifest-cached cascade)
# ----------------------------------------------------------------------
for step in 1 2 3 4 5 6 7a 7b 7c 8a 8b 8c 8d; do
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
echo "=== Phase 3 master-plan Step 8e verifier summary ==="
echo "  (18 checks total; all must pass)"

# Doc-phase 62 — record success in the cascade manifest.
if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "step8e"
fi

exit $FAIL
