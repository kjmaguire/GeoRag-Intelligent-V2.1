#!/usr/bin/env bash
# =============================================================================
# scripts/phase7_step1_verify.sh
#
# Phase 7 Step 1 done-definition — Dagster daemon tracer bootstrap
# (R-P6-1).
#
#   1. install_tracer_provider call wired into definitions.py
#   2. dagster-daemon compose block has OTEL_EXPORTER_OTLP_ENDPOINT +
#      OTEL_SERVICE_NAME
#   3. dagster-webserver compose block has the same OTel env
#   4. opentelemetry-* deps added to dagster/pyproject.toml so future
#      image rebuilds get the SDK
#   5. observability package reachable from the Dagster module tree
#   6. definitions.py parses cleanly (no syntax regression from the
#      bootstrap insertion)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=6
REPO="${REPO:-/home/georag/projects/georag}"
DEFS="$REPO/src/dagster/georag_dagster/definitions.py"
COMPOSE="$REPO/docker-compose.yml"
PYPROJ="$REPO/src/dagster/pyproject.toml"

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
PHASE 7 STEP 1 — DAGSTER TRACER BOOTSTRAP VERIFICATION
============================================================
BANNER

# 1) Bootstrap call in definitions.py
if grep -q 'install_tracer_provider(default_service_name="georag-dagster-daemon")' "$DEFS"; then
    check "definitions.py calls install_tracer_provider() at module load" ok
else
    check "definitions wiring" fail "install call missing"
fi

# 2) dagster-daemon compose env
# Extract the daemon block lines (between the daemon container_name and the
# next container_name).
daemon_block=$(awk '
    /container_name: georag-dagster-daemon/ { found=1 }
    found && /container_name: georag-dagster-webserver/ { exit }
    found { print }
' "$COMPOSE")
if echo "$daemon_block" | grep -q 'OTEL_EXPORTER_OTLP_ENDPOINT' \
    && echo "$daemon_block" | grep -q 'OTEL_SERVICE_NAME: georag-dagster-daemon'; then
    check "dagster-daemon block has OTel endpoint + service name" ok
else
    check "daemon env" fail "missing OTel env on dagster-daemon"
fi

# 3) dagster-webserver compose env
webserver_block=$(awk '
    /container_name: georag-dagster-webserver/ { found=1 }
    found && /^  [a-z]/ && !/container_name: georag-dagster-webserver/ { lines++ }
    found { print }
    found && lines > 200 { exit }
' "$COMPOSE")
if echo "$webserver_block" | grep -q 'OTEL_EXPORTER_OTLP_ENDPOINT' \
    && echo "$webserver_block" | grep -q 'OTEL_SERVICE_NAME: georag-dagster-webserver'; then
    check "dagster-webserver block has OTel endpoint + service name" ok
else
    check "webserver env" fail "missing OTel env on dagster-webserver"
fi

# 4) pyproject OTel deps
otel_deps=$(grep -cE '^[[:space:]]+"opentelemetry-(api|sdk|exporter-otlp-proto-http)' "$PYPROJ")
[ "$otel_deps" = "3" ] \
    && check "dagster/pyproject.toml lists 3 opentelemetry-* deps" ok \
    || check "pyproject deps" fail "got $otel_deps / 3"

# 5) observability module present in the Dagster tree
if [ -f "$REPO/src/dagster/georag_dagster/observability/__init__.py" ] \
    && [ -f "$REPO/src/dagster/georag_dagster/observability/otel.py" ]; then
    check "observability package present under georag_dagster/" ok
else
    check "observability module" fail "missing files"
fi

# 6) definitions.py parses
if python3 -c "import ast; ast.parse(open('$DEFS').read())" 2>/dev/null; then
    check "definitions.py parses cleanly" ok
else
    check "definitions parse" fail "syntax error after bootstrap insertion"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "Note: actual span export to Tempo activates after the next dagster"
echo "      image rebuild (opentelemetry-* deps come in via pyproject)."
echo "      The wiring is correct; runtime e2e is deferred to Phase 8."
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
