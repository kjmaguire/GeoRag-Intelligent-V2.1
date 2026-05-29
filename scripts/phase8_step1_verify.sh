#!/usr/bin/env bash
# =============================================================================
# scripts/phase8_step1_verify.sh
#
# Phase 8 Step 1 done-definition — Dagster image rebuild + Tempo e2e
# (R-P7-1, closing Phase 7 Step 1 runtime gap).
#
#   1. georag/dagster:latest image exists locally
#   2. opentelemetry-api installed inside the image
#   3. opentelemetry-sdk installed inside the image
#   4. opentelemetry-exporter-otlp-proto-http installed inside the image
#   5. observability bootstrap importable from the image
#   6. install_tracer_provider() returns True when given an OTLP endpoint
#      (proves the bootstrap path fires with the SDK present)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
IMG="${DAGSTER_IMG:-georag/dagster:latest}"

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

cat <<'BANNER'

============================================================
PHASE 8 STEP 1 — DAGSTER IMAGE OTel REBUILD VERIFICATION
============================================================
BANNER

# 1) Image exists
if docker image inspect "$IMG" >/dev/null 2>&1; then
    check "$IMG image present locally" ok
else
    check "image" fail "$IMG not built — run: docker compose build dagster-daemon"
fi

# 2-4) OTel deps inside the image
for pkg in opentelemetry-api opentelemetry-sdk opentelemetry-exporter-otlp-proto-http; do
    ver=$(docker run --rm "$IMG" pip show "$pkg" 2>/dev/null \
        | awk -F': ' '/^Version:/ {print $2}')
    if [ -n "$ver" ]; then
        check "$pkg installed in image (v$ver)" ok
    else
        check "$pkg" fail "missing"
    fi
done

# 5) Bootstrap import probe — mounts the dagster source tree into the
# image so `from georag_dagster.observability import ...` resolves.
boot_import=$(docker run --rm \
    -v "$REPO/src/dagster:/app" \
    "$IMG" python3 -c "
import sys; sys.path.insert(0, '/app')
from georag_dagster.observability import install_tracer_provider, get_tracer
print('IMPORT_OK')
" 2>&1 | tail -1)
[ "$boot_import" = "IMPORT_OK" ] \
    && check "observability bootstrap importable from the image" ok \
    || check "observability import" fail "$boot_import"

# 6) Bootstrap actually installs with OTLP endpoint set
boot_run=$(docker run --rm \
    -v "$REPO/src/dagster:/app" \
    -e OTEL_EXPORTER_OTLP_ENDPOINT="http://otel-collector:4318" \
    -e OTEL_SERVICE_NAME="phase8-step1-probe" \
    "$IMG" python3 -c "
import sys; sys.path.insert(0, '/app')
from georag_dagster.observability import install_tracer_provider
print('install:', install_tracer_provider('phase8-step1-probe'))
" 2>&1 | tail -1)
[ "$boot_run" = "install: True" ] \
    && check "install_tracer_provider() installs SDK provider with OTel deps present" ok \
    || check "bootstrap install" fail "$boot_run"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
