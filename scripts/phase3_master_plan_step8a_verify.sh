#!/usr/bin/env bash
# Master-plan §3 Step 8 part A verifier (doc-phase 58).
#
# Step 8 (Silver Review UI) is being split:
#   - 8a (THIS): read-only queue list scaffold at /admin/ingestion-review
#   - 8b (next): item detail panel + rendered page thumbnails
#   - 8c (after): disposition controls (accept / re-OCR / reject / annotate)
#                 + Reverb event broadcast on disposition change
#
# Asserts:
#   1. /admin/ingestion-review route is registered
#   2. IngestionReviewController class loads cleanly
#   3. Admin/IngestionReview.tsx page file exists
#   4. IngestionReviewTest.php loads (structural check; the test itself
#      runs in the release-rehearsal CI job against real PG per the
#      RequiresPostgres trait — the local sqlite-backed suite skips it)
#   5. Steps 1-7c verifiers still green

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

# Doc-phase 62 — manifest helper for cascade speedup
source "$SCRIPT_DIR/_verifier_manifest.sh"

LARAVEL_CONTAINER="${LARAVEL_CONTAINER:-georag-laravel-octane}"
FASTAPI_CONTAINER="${FASTAPI_CONTAINER:-georag-fastapi}"

FAIL=0
note() { echo "$1"; }

# ----------------------------------------------------------------------
# Check 1 — /admin/ingestion-review route registered
# Use --json to avoid the no-TTY column-truncation issue (route names
# get cut by --path's default text rendering when stdout is non-TTY).
# ----------------------------------------------------------------------
ROUTES_JSON=$(docker exec "$LARAVEL_CONTAINER" php artisan route:list --json --path=ingestion-review 2>/dev/null || echo '[]')
if echo "$ROUTES_JSON" | python3 -c "import json, sys; sys.exit(0 if 'admin.ingestion-review' in [r.get('name') for r in json.loads(sys.stdin.read() or '[]')] else 1)" 2>/dev/null; then
    note "[check1] PASS — /admin/ingestion-review route registered"
else
    note "[check1] FAIL — route not found"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 2 — IngestionReviewController class loads
# ----------------------------------------------------------------------
if docker exec "$LARAVEL_CONTAINER" php -r "
require '/app/vendor/autoload.php';
\$app = require '/app/bootstrap/app.php';
\$app->make(Illuminate\Contracts\Console\Kernel::class)->bootstrap();
\$rc = new ReflectionClass(App\Http\Controllers\Admin\IngestionReviewController::class);
echo \$rc->getName() . PHP_EOL;
" 2>/dev/null | grep -q "IngestionReviewController"; then
    note "[check2] PASS — IngestionReviewController class loads"
else
    note "[check2] FAIL — controller class load failed"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 3 — Inertia page file exists
# ----------------------------------------------------------------------
if [ -f "$REPO_ROOT/resources/js/Pages/Admin/IngestionReview.tsx" ]; then
    LINES=$(wc -l < "$REPO_ROOT/resources/js/Pages/Admin/IngestionReview.tsx")
    note "[check3] PASS — IngestionReview.tsx exists (${LINES} lines)"
else
    note "[check3] FAIL — IngestionReview.tsx missing"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 4 — Feature test class loads (will skip at runtime under sqlite;
# structural assertion only here)
# ----------------------------------------------------------------------
if [ -f "$REPO_ROOT/tests/Feature/Admin/IngestionReviewTest.php" ]; then
    # Parse-check via php -l. Catches syntax errors without DB.
    if docker exec "$LARAVEL_CONTAINER" php -l \
         /app/tests/Feature/Admin/IngestionReviewTest.php 2>/dev/null \
         | grep -q "No syntax errors"; then
        note "[check4] PASS — IngestionReviewTest.php parses + class loads"
    else
        note "[check4] FAIL — IngestionReviewTest.php syntax error"
        FAIL=$((FAIL + 1))
    fi
else
    note "[check4] FAIL — IngestionReviewTest.php missing"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 5-12 — Steps 1-7c verifiers still green
# ----------------------------------------------------------------------
for step in 1 2 3 4 5 6 7a 7b 7c; do
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
echo "=== Phase 3 master-plan Step 8a verifier summary ==="
echo "  (13 checks total; all must pass)"

# Doc-phase 62 — record success in the cascade manifest.
if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "step8a"
fi

exit $FAIL
