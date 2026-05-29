#!/usr/bin/env bash
# CPU-OCR smoke-bench orchestrator for master-plan §3 Step 1.
#
# Runs `ocr_cpu_smoke.py` inside `georag-hatchet-worker-ingestion`
# (which has the §04p libs per ADR-0002 amendment 2026-05-12), then
# copies the JSON report back to the host at
# ops/validation/reports/ocr_cpu_smoke_<timestamp>.json.
#
# Acceptance per kickoff Step 1:
#   - native latency within 1-25 sec/page (1 sec/page × 5x tolerance)
#   - scanned warm latency within 5-150 sec/page (5 sec/page × 5x tolerance)
#   - exits 0 on pass, 1 on investigate, 2+ on infrastructure failure
#
# Usage:
#   bash ops/validation/ocr_cpu_smoke.sh                # default fixtures
#   OCR_SMOKE_NATIVE_PDF=/path/to/pdf bash ocr_cpu_smoke.sh  # custom PDF
set -euo pipefail

CONTAINER="${CONTAINER:-georag-hatchet-worker-ingestion}"
HOST_REPORT_DIR="${HOST_REPORT_DIR:-ops/validation/reports}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$REPO_ROOT"
mkdir -p "$HOST_REPORT_DIR"

# Pre-flight: confirm the container is up + has the libs we expect.
if ! docker inspect "$CONTAINER" >/dev/null 2>&1; then
    echo "FATAL: container '$CONTAINER' not found. Is the stack up?" >&2
    exit 2
fi

if [ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER")" != "true" ]; then
    echo "FATAL: container '$CONTAINER' exists but is not running." >&2
    exit 2
fi

echo "[smoke] container OK: $CONTAINER"

# Copy the Python script into the container at a known path.
# /tmp inside the container is writable for any process.
docker cp "$SCRIPT_DIR/ocr_cpu_smoke.py" "$CONTAINER:/tmp/ocr_cpu_smoke.py"
echo "[smoke] script copied to $CONTAINER:/tmp/ocr_cpu_smoke.py"

# Copy the native input PDF into the container. The hatchet-worker-ingestion
# container only mounts the FastAPI source tree; the dagster fixtures live
# under a different mount that the worker doesn't see. So we ship the PDF
# in via docker cp to a known /tmp path.
HOST_NATIVE_PDF="${OCR_SMOKE_NATIVE_PDF_HOST:-src/dagster/tests/fixtures/reports/PLS-2024-Technical-Report.pdf}"
if [ ! -f "$HOST_NATIVE_PDF" ]; then
    echo "FATAL: native input PDF not found on host at: $HOST_NATIVE_PDF" >&2
    exit 2
fi
CONTAINER_NATIVE_PDF="/tmp/ocr_smoke_native.pdf"
docker cp "$HOST_NATIVE_PDF" "$CONTAINER:$CONTAINER_NATIVE_PDF"
echo "[smoke] native PDF copied to $CONTAINER:$CONTAINER_NATIVE_PDF (host: $HOST_NATIVE_PDF)"

# The Python script reads OCR_SMOKE_NATIVE_PDF for the in-container path.
ENV_ARG="-e OCR_SMOKE_NATIVE_PDF=${OCR_SMOKE_NATIVE_PDF:-$CONTAINER_NATIVE_PDF}"

# Run the bench. Use --user 0 to write to /tmp without permission noise;
# this is a measurement script, not a production code path.
set +e
docker exec $ENV_ARG --user 0 "$CONTAINER" python /tmp/ocr_cpu_smoke.py
EXIT_CODE=$?
set -e

echo "[smoke] python script exited with code: $EXIT_CODE"

# Copy the JSON report out of the container.
LATEST_IN_CONTAINER=$(docker exec "$CONTAINER" bash -c \
    "ls -1t /tmp/ocr_cpu_smoke_*.json 2>/dev/null | head -n 1" || true)

if [ -z "$LATEST_IN_CONTAINER" ]; then
    echo "WARN: no ocr_cpu_smoke_*.json report found in container /tmp/" >&2
    exit $EXIT_CODE
fi

BASENAME=$(basename "$LATEST_IN_CONTAINER")
DEST="$HOST_REPORT_DIR/$BASENAME"
docker cp "$CONTAINER:$LATEST_IN_CONTAINER" "$DEST"
echo "[smoke] report saved: $DEST"

# Print a quick human summary.
if command -v jq >/dev/null 2>&1; then
    echo ""
    echo "=== summary ==="
    jq -r '
        "overall: \(.overall)",
        "cpu_count: \(.cpu_count)",
        "parse_native wall_ms: \(.parse_native.wall_ms)",
        "parse_scanned per_page_warm_ms: \(.parse_scanned.per_page_warm_ms)",
        "parse_mixed wall_ms: \(.parse_mixed.wall_ms)",
        (.gates[] | "gate[\(.label)]: \(.verdict)")
    ' "$DEST"
else
    echo "(install jq for a formatted summary; raw JSON is in $DEST)"
fi

exit $EXIT_CODE
