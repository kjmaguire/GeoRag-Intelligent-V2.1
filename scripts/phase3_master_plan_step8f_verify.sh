#!/usr/bin/env bash
# Master-plan §3 Step 8 part F verifier (doc-phase 64).
#
# Step 8f closes out Step 8 with the two remaining doc-phase 61 §7
# deferrals:
#   - audit.audit_ledger emission per disposition (silver.low_confidence_page_reviews.disposition)
#   - Reverb broadcast on disposition change (private-admin.ingestion-review)
#
# Asserts:
#   1. IngestionReviewDispositionChanged event class exists + parses
#   2. routes/channels.php has admin.ingestion-review broadcast channel
#   3. IngestionReviewController imports AuditEmitter + Event facade
#   4. IngestionReviewController.update() emits audit + dispatches event
#   5. Step 8e verifier (and via cascade, everything prior) still green

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

# Doc-phase 62 — manifest helper for cascade speedup
source "$SCRIPT_DIR/_verifier_manifest.sh"

LARAVEL_CONTAINER="${LARAVEL_CONTAINER:-georag-laravel-octane}"

FAIL=0
note() { echo "$1"; }

# ----------------------------------------------------------------------
# Check 1 — IngestionReviewDispositionChanged event class
# ----------------------------------------------------------------------
EVENT_FILE="$REPO_ROOT/app/Events/Admin/IngestionReviewDispositionChanged.php"
if [ -f "$EVENT_FILE" ] && docker exec "$LARAVEL_CONTAINER" php -l "/app/app/Events/Admin/IngestionReviewDispositionChanged.php" 2>/dev/null | grep -q "No syntax errors"; then
    note "[check1] PASS — IngestionReviewDispositionChanged.php exists + parses"
else
    note "[check1] FAIL — event class missing or syntax error"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 2 — admin.ingestion-review broadcast channel registered
# ----------------------------------------------------------------------
if grep -q "admin.ingestion-review" "$REPO_ROOT/routes/channels.php"; then
    note "[check2] PASS — admin.ingestion-review channel registered in channels.php"
else
    note "[check2] FAIL — broadcast channel auth not registered"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 3 — controller imports + emits both
# ----------------------------------------------------------------------
CONTROLLER_FILE="$REPO_ROOT/app/Http/Controllers/Admin/IngestionReviewController.php"
if grep -q "use App\\\\Services\\\\Audit\\\\AuditEmitter;" "$CONTROLLER_FILE" \
   && grep -q "use App\\\\Events\\\\Admin\\\\IngestionReviewDispositionChanged;" "$CONTROLLER_FILE"; then
    note "[check3] PASS — controller imports AuditEmitter + DispositionChanged event"
else
    note "[check3] FAIL — controller missing AuditEmitter or event import"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 4 — controller calls AuditEmitter->emit + Event::dispatch
# ----------------------------------------------------------------------
if grep -q "AuditEmitter::class" "$CONTROLLER_FILE" \
   && grep -q "silver.low_confidence_page_reviews.disposition" "$CONTROLLER_FILE" \
   && grep -q "Event::dispatch(new IngestionReviewDispositionChanged" "$CONTROLLER_FILE"; then
    note "[check4] PASS — controller emits audit + broadcasts event"
else
    note "[check4] FAIL — controller missing audit/broadcast wiring"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 5+ — Steps 1-8e still green (manifest-cached cascade)
# ----------------------------------------------------------------------
for step in 1 2 3 4 5 6 7a 7b 7c 8a 8b 8c 8d 8e; do
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
echo "=== Phase 3 master-plan Step 8f verifier summary ==="
echo "  (18 checks total; all must pass)"

# Doc-phase 62 — record success.
if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "step8f"
fi

exit $FAIL
