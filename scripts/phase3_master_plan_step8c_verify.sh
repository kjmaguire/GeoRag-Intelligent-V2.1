#!/usr/bin/env bash
# Master-plan §3 Step 8 part C verifier (doc-phase 60).
#
# Step 8 split:
#   - 8a (doc-phase 58): queue list scaffold ✓
#   - 8b (doc-phase 59): FastAPI render endpoint + bronze tracking ✓
#   - 8c (THIS): React detail panel + Laravel JSON endpoint +
#                page-render reverse-proxy
#   - 8d (doc-phase 61): disposition controls + Reverb broadcast
#
# Asserts:
#   1. /admin/ingestion-review/{id}.json route registered (show)
#   2. /admin/ingestion-review/{id}/page/{n}.png route registered (pageRender)
#   3. IngestionReviewController has show() + pageRender() methods
#   4. IngestionReview.tsx contains the DetailPanel component
#   5. Updated IngestionReviewTest.php parses cleanly
#   6. Steps 1-8b verifiers still green

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

# Doc-phase 62 — manifest helper for cascade speedup
source "$SCRIPT_DIR/_verifier_manifest.sh"

LARAVEL_CONTAINER="${LARAVEL_CONTAINER:-georag-laravel-octane}"

FAIL=0
note() { echo "$1"; }

# Capture route:list output once via --json (avoids no-TTY truncation
# of route names; doc-phase 62 fix).
ROUTES_JSON=$(docker exec "$LARAVEL_CONTAINER" php artisan route:list --json --path=ingestion-review 2>/dev/null || echo '[]')

_route_name_exists() {
    echo "$ROUTES_JSON" | python3 -c "import json, sys; sys.exit(0 if '$1' in [r.get('name') for r in json.loads(sys.stdin.read() or '[]')] else 1)" 2>/dev/null
}

# ----------------------------------------------------------------------
# Check 1 — show route registered
# ----------------------------------------------------------------------
if _route_name_exists "admin.ingestion-review.show"; then
    note "[check1] PASS — admin.ingestion-review.show route registered"
else
    note "[check1] FAIL — show route missing"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 2 — page-render route registered
# ----------------------------------------------------------------------
if _route_name_exists "admin.ingestion-review.page-render"; then
    note "[check2] PASS — admin.ingestion-review.page-render route registered"
else
    note "[check2] FAIL — page-render route missing"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 3 — Controller has show() + pageRender() methods
# ----------------------------------------------------------------------
if docker exec "$LARAVEL_CONTAINER" php -r "
require '/app/vendor/autoload.php';
\$app = require '/app/bootstrap/app.php';
\$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
\$rc = new ReflectionClass(App\Http\Controllers\Admin\IngestionReviewController::class);
if (!\$rc->hasMethod('show')) { echo 'no show'; exit(1); }
if (!\$rc->hasMethod('pageRender')) { echo 'no pageRender'; exit(1); }
echo 'ok';
" 2>/dev/null | grep -q "ok"; then
    note "[check3] PASS — controller has show() + pageRender()"
else
    note "[check3] FAIL — controller missing show or pageRender"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 4 — IngestionReview.tsx has DetailPanel
# ----------------------------------------------------------------------
if grep -q "function DetailPanel" \
     "$REPO_ROOT/resources/js/Pages/Admin/IngestionReview.tsx" \
   && grep -q "page_render_url" \
     "$REPO_ROOT/resources/js/Pages/Admin/IngestionReview.tsx"; then
    note "[check4] PASS — IngestionReview.tsx has DetailPanel + page_render_url usage"
else
    note "[check4] FAIL — DetailPanel or page_render_url missing in TSX"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 5 — IngestionReviewTest.php parses cleanly
# ----------------------------------------------------------------------
if docker exec "$LARAVEL_CONTAINER" php -l \
     /app/tests/Feature/Admin/IngestionReviewTest.php 2>/dev/null \
     | grep -q "No syntax errors"; then
    note "[check5] PASS — IngestionReviewTest.php parses cleanly"
else
    note "[check5] FAIL — IngestionReviewTest.php syntax error"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 6+ — Steps 1-8b verifiers still green
# ----------------------------------------------------------------------
for step in 1 2 3 4 5 6 7a 7b 7c 8a 8b; do
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
echo "=== Phase 3 master-plan Step 8c verifier summary ==="
echo "  (16 checks total; all must pass)"

# Doc-phase 62 — record success in the cascade manifest.
if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "step8c"
fi

exit $FAIL
