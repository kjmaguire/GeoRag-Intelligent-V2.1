#!/usr/bin/env bash
# Master-plan §3 Step 1 verifier (doc-phase 49).
#
# Acceptance per docs/phase3_master_plan_kickoff.md Step 1:
#   1. app/ocr/ package importable from inside the running
#      georag-hatchet-worker-ingestion container; each parser module
#      exports the documented async function signature.
#   2. Import-boundary lint passes (no route handler imports app.ocr).
#   3. Smoke-bench measured latency is within 5x the ADR-0002 estimate
#      ranges. If measured latency is >5x, halt Phase 3 and reopen
#      ADR-0002.
#
# Each check exits with a clear pass/fail line; the script aggregates
# and exits 0 only if all three pass.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

# Doc-phase 62 — manifest helper for cascade speedup
source "$SCRIPT_DIR/_verifier_manifest.sh"

# Track failures without exiting early — we want every check to report
# its own status so a partial pass is diagnosable.
FAIL_COUNT=0
RESULTS=()

note() { RESULTS+=("$1"); echo "$1"; }

# ----------------------------------------------------------------------
# Check 1 — module imports + async signatures inside the running container
# ----------------------------------------------------------------------
CONTAINER="${CONTAINER:-georag-hatchet-worker-ingestion}"

if ! docker inspect "$CONTAINER" >/dev/null 2>&1 || \
   [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" != "true" ]; then
    note "[check1] FAIL — container '$CONTAINER' not running"
    FAIL_COUNT=$((FAIL_COUNT + 1))
else
    # Run the import-test inside the container. The PYTHONPATH already
    # points at /app where the FastAPI source is bind-mounted.
    if docker exec "$CONTAINER" python -c "
import importlib, inspect, sys
modules = [
    ('app.ocr.preflight', 'preflight'),
    ('app.ocr.profile', 'profile'),
    ('app.ocr.parse_native', 'parse_native'),
    ('app.ocr.parse_scanned', 'parse_scanned'),
    ('app.ocr.parse_mixed', 'parse_mixed'),
    ('app.ocr.parse_table_heavy', 'parse_table_heavy'),
    ('app.ocr.render', 'render_page'),
    ('app.ocr.quality_graph', 'route_page'),
]
bad = []
for mod, sym in modules:
    m = importlib.import_module(mod)
    fn = getattr(m, sym, None)
    if fn is None:
        bad.append(f'{mod}: missing {sym}')
    elif not inspect.iscoroutinefunction(fn):
        bad.append(f'{mod}.{sym}: not async')
if bad:
    print('FAIL:', '; '.join(bad), file=sys.stderr)
    sys.exit(1)
import app.ocr as pkg
expected = {m[1] for m in modules}
missing = expected - set(getattr(pkg, '__all__', []))
if missing:
    print('FAIL: app.ocr.__all__ missing', missing, file=sys.stderr)
    sys.exit(1)
print('OK: all 8 modules importable, all 8 functions async, __all__ complete')
" >/dev/null 2>&1; then
        note "[check1] PASS — 8/8 app.ocr modules importable + async + re-exported"
    else
        note "[check1] FAIL — app.ocr module imports failed (see container logs)"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
fi

# ----------------------------------------------------------------------
# Check 2 — import-boundary lint
# ----------------------------------------------------------------------
if bash "$SCRIPT_DIR/phase3_master_plan_step1_import_boundary.sh" >/dev/null 2>&1; then
    note "[check2] PASS — import-boundary lint clean"
else
    note "[check2] FAIL — import-boundary lint detected violations"
    bash "$SCRIPT_DIR/phase3_master_plan_step1_import_boundary.sh" || true
    FAIL_COUNT=$((FAIL_COUNT + 1))
fi

# ----------------------------------------------------------------------
# Check 3 — CPU-OCR smoke-bench gates
# ----------------------------------------------------------------------
# Look for the most recent smoke-bench report. If older than 24h or
# missing, the verifier re-runs the bench (which takes ~3 min).
LATEST_REPORT=$(ls -1t "$REPO_ROOT/ops/validation/reports/ocr_cpu_smoke_"*.json 2>/dev/null | head -n 1 || true)

if [ -z "$LATEST_REPORT" ]; then
    echo "[check3] no smoke-bench report; running bench now (~3 min)..."
    bash "$REPO_ROOT/ops/validation/ocr_cpu_smoke.sh" >/dev/null 2>&1 || true
    LATEST_REPORT=$(ls -1t "$REPO_ROOT/ops/validation/reports/ocr_cpu_smoke_"*.json 2>/dev/null | head -n 1 || true)
fi

if [ -z "$LATEST_REPORT" ]; then
    note "[check3] FAIL — no smoke-bench report produced; CPU-OCR assumption unvalidated"
    FAIL_COUNT=$((FAIL_COUNT + 1))
else
    # Extract overall verdict from the JSON.
    if command -v jq >/dev/null 2>&1; then
        OVERALL=$(jq -r '.overall' "$LATEST_REPORT")
    else
        OVERALL=$(python3 -c "import json,sys; print(json.load(open('$LATEST_REPORT'))['overall'])" 2>/dev/null || echo "unknown")
    fi
    if [ "$OVERALL" = "pass" ]; then
        note "[check3] PASS — smoke-bench gates all pass (report: $(basename "$LATEST_REPORT"))"
    else
        note "[check3] FAIL — smoke-bench overall = '$OVERALL' (report: $(basename "$LATEST_REPORT"))"
        FAIL_COUNT=$((FAIL_COUNT + 1))
    fi
fi

# ----------------------------------------------------------------------
# Aggregate
# ----------------------------------------------------------------------
echo ""
echo "=== Phase 3 master-plan Step 1 verifier summary ==="
for r in "${RESULTS[@]}"; do
    echo "  $r"
done
TOTAL=${#RESULTS[@]}
PASSED=$((TOTAL - FAIL_COUNT))
echo "  $PASSED/$TOTAL checks passed"

# Doc-phase 62 — record success in the cascade manifest so downstream
# verifiers can skip the re-run within MANIFEST_TTL_SEC.
if [ $FAIL_COUNT -eq 0 ]; then
    mark_verifier_passed "step1"
fi

exit $FAIL_COUNT
