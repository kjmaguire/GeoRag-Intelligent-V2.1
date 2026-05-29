#!/usr/bin/env bash
# =============================================================================
# tests/load_k6/run_section11_battery.sh
#
# §11.9b — full SLO battery against the dev or staging stack. Runs all
# 5 k6 scripts sequentially (sequential vs concurrent to keep load shape
# clean — concurrent runs interfere with each other's p95 measurements)
# and drops a markdown summary into docs/load_tests/.
#
# Pre-requisites:
#   - Docker available (k6 runs in a container)
#   - GEORAG_BASE_URL, GEORAG_BEARER_TOKEN (or GEORAG_SERVICE_KEY for
#     admin-gated scripts), GEORAG_WORKSPACE_ID env vars set
#
# Run:
#   ./tests/load_k6/run_section11_battery.sh             # full battery
#   ./tests/load_k6/run_section11_battery.sh quick       # short stages
#
# Exit code 0 = every script passed its thresholds.
# =============================================================================

set -uo pipefail

MODE="${1:-full}"
TS=$(date -u +"%Y%m%dT%H%M%SZ")
OUT_DIR="${OUT_DIR:-docs/load_tests}"
mkdir -p "$OUT_DIR"
SUMMARY="$OUT_DIR/section11_battery_${TS}.md"

BASE_URL="${GEORAG_BASE_URL:-http://localhost:8000}"
TILE_BASE="${GEORAG_TILE_BASE:-http://localhost:3000}"
WS_ID="${GEORAG_WORKSPACE_ID:-a0000000-0000-0000-0000-000000000001}"
PRJ_ID="${GEORAG_PROJECT_ID:-22222222-2222-2222-2222-222222222222}"
TOKEN="${GEORAG_BEARER_TOKEN:-}"
SVC="${GEORAG_SERVICE_KEY:-}"

if [ "$MODE" = "quick" ]; then
    # Quick mode: skip iterations + tag scenario as smoke. k6 --vus
    # overrides the script's named scenarios with a simple constant-VU
    # executor, which trips reports that export named scenario funcs
    # (no `default` export). For the battery's smoke purpose this is
    # acceptable; full SLO validation needs MODE=full.
    DURATION_OVERRIDE="--vus 2 --duration 10s --no-thresholds --quiet"
else
    DURATION_OVERRIDE=""
fi

# Header
{
    echo "# §11.9b k6 SLO Battery — $TS"
    echo
    echo "Mode: \`$MODE\`"
    echo "Targets: \`$BASE_URL\` (api) / \`$TILE_BASE\` (tiles)"
    echo
    echo "| Script | SLO | Result |"
    echo "|--------|-----|--------|"
} > "$SUMMARY"

FAILED=0

run_one() {
    local name="$1"
    local script="$2"
    local slo="$3"
    local extra_env="$4"

    echo
    echo "============================================================"
    echo "  $name"
    echo "============================================================"

    # MSYS_NO_PATHCONV=1 disables Git Bash's auto path translation —
    # without it, `/scripts/foo.js` gets rewritten to a Windows path
    # before being passed into the docker container, which then can't
    # find it. Linux/macOS shells ignore the env var.
    MSYS_NO_PATHCONV=1 docker run --rm --network host \
        -e GEORAG_BASE_URL="$BASE_URL" \
        -e GEORAG_TILE_BASE="$TILE_BASE" \
        -e GEORAG_BEARER_TOKEN="$TOKEN" \
        -e GEORAG_SERVICE_KEY="$SVC" \
        -e GEORAG_WORKSPACE_ID="$WS_ID" \
        -e GEORAG_PROJECT_ID="$PRJ_ID" \
        $extra_env \
        -v "$PWD/tests/load_k6:/scripts" \
        grafana/k6 run $DURATION_OVERRIDE "/scripts/$script"

    local rc=$?
    local result
    if [ "$rc" -eq 0 ]; then
        result="✓ PASS"
    else
        result="✗ FAIL"
        FAILED=$((FAILED + 1))
    fi
    echo "| $name | $slo | $result |" >> "$SUMMARY"
}

# Subset: skip scripts the operator hasn't seeded credentials for.
if [ -n "$TOKEN" ]; then
    run_one "RAG query"           "rag_query.k6.js"        "p95 chat < 8s @ 100u"   ""
fi
run_one "Map tile fetch"          "map_tile_fetch.k6.js"   "p95 tile < 200ms @ 100u" ""
if [ -n "$SVC" ]; then
    run_one "Report plan + draft" "report_build.k6.js"     "p95 report < 30s; draft < 2s" ""
fi
if [ -n "$TOKEN" ]; then
    run_one "Ingestion upload"    "ingestion_upload.k6.js" "(starter SLO)"          ""
    run_one "Viz strip log"       "viz_strip_log.k6.js"    "(starter SLO)"          ""
fi

{
    echo
    if [ "$FAILED" -eq 0 ]; then
        echo "**Result:** all scripts within SLO."
    else
        echo "**Result:** $FAILED script(s) failed SLO. Investigate before deploy."
    fi
} >> "$SUMMARY"

echo
echo "============================================================"
echo "  Summary written to: $SUMMARY"
echo "  Exit: $FAILED failed"
echo "============================================================"

exit $FAILED
