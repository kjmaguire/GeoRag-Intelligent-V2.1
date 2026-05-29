#!/usr/bin/env bash
# Master-plan §3 Step 8 part D verifier (doc-phase 61).
#
# Step 8d ships the disposition controls: PATCH endpoint for accept/
# re-OCR/reject + React panel buttons. Re-OCR Hatchet workflow trigger
# and Reverb broadcast are explicitly split to doc-phase 62 to keep
# this tick bounded (see doc-phase 61 handoff § 4).
#
# Asserts:
#   1. PATCH /admin/ingestion-review/{id} route registered
#   2. IngestionReviewController has update() method
#   3. IngestionReview.tsx has DispositionControls component
#   4. Updated IngestionReviewTest.php parses cleanly
#   5. Steps 1-8c verifiers still green

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

# Doc-phase 62 — manifest helper for cascade speedup
source "$SCRIPT_DIR/_verifier_manifest.sh"

LARAVEL_CONTAINER="${LARAVEL_CONTAINER:-georag-laravel-octane}"

FAIL=0
note() { echo "$1"; }

# Capture route:list output via --json (no-TTY truncation safe;
# doc-phase 62 fix).
ROUTES_JSON=$(docker exec "$LARAVEL_CONTAINER" php artisan route:list --json --path=ingestion-review 2>/dev/null || echo '[]')

# ----------------------------------------------------------------------
# Check 1 — PATCH route registered
# ----------------------------------------------------------------------
if echo "$ROUTES_JSON" | python3 -c "
import json, sys
routes = json.loads(sys.stdin.read() or '[]')
for r in routes:
    if r.get('name') == 'admin.ingestion-review.update' and 'PATCH' in (r.get('method') or ''):
        sys.exit(0)
sys.exit(1)
" 2>/dev/null; then
    note "[check1] PASS — PATCH /admin/ingestion-review/{id} registered"
else
    note "[check1] FAIL — PATCH route missing"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 2 — update() method present
# ----------------------------------------------------------------------
if docker exec "$LARAVEL_CONTAINER" php -r "
require '/app/vendor/autoload.php';
\$app = require '/app/bootstrap/app.php';
\$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
\$rc = new ReflectionClass(App\Http\Controllers\Admin\IngestionReviewController::class);
if (!\$rc->hasMethod('update')) { echo 'no'; exit(1); }
echo 'ok';
" 2>/dev/null | grep -q "ok"; then
    note "[check2] PASS — controller has update() method"
else
    note "[check2] FAIL — controller missing update()"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 3 — DispositionControls component in TSX
# ----------------------------------------------------------------------
if grep -q "function DispositionControls" \
     "$REPO_ROOT/resources/js/Pages/Admin/IngestionReview.tsx" \
   && grep -q "resolved_accept" \
     "$REPO_ROOT/resources/js/Pages/Admin/IngestionReview.tsx"; then
    note "[check3] PASS — DispositionControls component + resolved_accept action present"
else
    note "[check3] FAIL — DispositionControls component missing"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 4 — IngestionReviewTest.php parses cleanly
# ----------------------------------------------------------------------
if docker exec "$LARAVEL_CONTAINER" php -l \
     /app/tests/Feature/Admin/IngestionReviewTest.php 2>/dev/null \
     | grep -q "No syntax errors"; then
    note "[check4] PASS — IngestionReviewTest.php parses cleanly"
else
    note "[check4] FAIL — IngestionReviewTest.php syntax error"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 5+ — Steps 1-8c verifiers still green
# ----------------------------------------------------------------------
for step in 1 2 3 4 5 6 7a 7b 7c 8a 8b 8c; do
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
echo "=== Phase 3 master-plan Step 8d verifier summary ==="
echo "  (16 checks total; all must pass)"

# Doc-phase 62 — record success in the cascade manifest.
if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "step8d"
fi

exit $FAIL
