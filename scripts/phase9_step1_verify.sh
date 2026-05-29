#!/usr/bin/env bash
# =============================================================================
# scripts/phase9_step1_verify.sh
#
# Phase 9 Step 1 done-definition — Dagster Tempo e2e probe (R-P5-3
# Dagster variant). Phase 8 Step 1 confirmed the OTel SDK is in the
# georag/dagster:latest image; this step proves spans actually flow
# from the image to Tempo end-to-end.
#
#   1. georag/dagster:latest image still has the OTel SDK
#   2. Dagster source mount (georag_dagster + tests fixtures) is
#      addressable from a one-shot container
#   3. parse_pdf_report called from inside the image installs the
#      tracer + emits stage spans
#   4. ≥6 spans land in Tempo under service.name=$PROBE
#   5. Tempo's tag value endpoint returns the probe service name
#      (proves the resource attribute propagated end-to-end)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=5
REPO="${REPO:-/home/georag/projects/georag}"
IMG="${DAGSTER_IMG:-georag/dagster:latest}"
TEMPO="${TEMPO_URL:-http://localhost:3200}"
NETWORK="${COMPOSE_NETWORK:-georag}"
PROBE="phase9-dagster-probe-$(date +%s)"
FIXTURE="$REPO/src/dagster/tests/fixtures/reports/PLS-2024-Technical-Report.pdf"

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
PHASE 9 STEP 1 — DAGSTER TEMPO e2e VERIFICATION
============================================================
BANNER

# 1) SDK still present in the image
sdk_ver=$(docker run --rm "$IMG" pip show opentelemetry-sdk 2>/dev/null \
    | awk -F': ' '/^Version:/ {print $2}')
[ -n "$sdk_ver" ] \
    && check "georag/dagster:latest carries opentelemetry-sdk (v$sdk_ver)" ok \
    || check "sdk present" fail "missing"

# 2) Source + fixture readable
if [ -f "$FIXTURE" ] \
    && [ -f "$REPO/src/dagster/georag_dagster/observability/otel.py" ]; then
    check "Dagster source + fixture PDF reachable from host" ok
else
    check "source mount sources" fail "fixture or otel.py missing"
fi

# 3-5) End-to-end probe
# Drop a probe script next to the source so it's bind-mounted in.
PROBE_PY=$(mktemp --suffix=.py)
cat > "$PROBE_PY" <<'PY'
import importlib.util
import os
import sys
import time

sys.path.insert(0, '/app/georag_dagster_src')

from georag_dagster.observability import install_tracer_provider
installed = install_tracer_provider(default_service_name=os.environ['OTEL_SERVICE_NAME'])
print('install:', installed, flush=True)

# Load parse_pdf_report directly under its canonical name so the
# dataclass decorator can locate the module in sys.modules.
spec = importlib.util.spec_from_file_location(
    'georag_dagster.parsers.pdf_report',
    '/app/georag_dagster_src/georag_dagster/parsers/pdf_report.py',
)
mod = importlib.util.module_from_spec(spec)
sys.modules['georag_dagster.parsers.pdf_report'] = mod
spec.loader.exec_module(mod)

try:
    result = mod.parse_pdf_report('/fixture.pdf')
    print('parsed:', len(result.sections), 'sections', flush=True)
except Exception as e:
    print('parse_error:', repr(e), flush=True)

# Force a flush so spans hit the collector before exit.
from opentelemetry import trace
provider = trace.get_tracer_provider()
flushed = getattr(provider, 'force_flush', lambda **_: False)(timeout_millis=10000)
print('flush:', flushed, flush=True)
PY
# The dagster image runs as `nobody` (uid 65534). A mktemp under
# /tmp inherits the host shell's mode (often 0600); mark it
# world-readable so the in-container user can read it.
chmod 0644 "$PROBE_PY"

probe_out=$(docker run --rm \
    --network "$NETWORK" \
    -v "$REPO/src/dagster:/app/georag_dagster_src:ro" \
    -v "$FIXTURE:/fixture.pdf:ro" \
    -v "$PROBE_PY:/probe.py:ro" \
    -e OTEL_EXPORTER_OTLP_ENDPOINT="http://otel-collector:4318" \
    -e OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf" \
    -e OTEL_SERVICE_NAME="$PROBE" \
    "$IMG" python3 /probe.py 2>&1)
rm -f "$PROBE_PY"

# Extract the key markers from the probe output
echo "    probe_out: $(echo "$probe_out" | tr '\n' '|' | head -c 240)"
if echo "$probe_out" | grep -qE 'install: True'; then
    inst_ok="y"
else
    inst_ok="n"
fi
parsed_count=$(echo "$probe_out" | grep -oE 'parsed: [0-9]+ sections' | head -1)
flush_ok=$(echo "$probe_out" | grep -E '^flush: ' | tail -1)

if [ "$inst_ok" = "y" ] && [ -n "$parsed_count" ]; then
    check "parse_pdf_report ran inside the dagster image ($parsed_count)" ok
else
    check "in-image parse" fail "install=$inst_ok parsed=$parsed_count flush=$flush_ok"
fi

# 4) Tempo span count for the probe service. Polled — BatchSpanProcessor
# flush has already fired, but Tempo's ingestor may take a few seconds
# to index.
found=0
for _ in $(seq 1 12); do
    resp=$(curl -s --get "$TEMPO/api/search" \
        --data-urlencode "tags=service.name=$PROBE" \
        --data-urlencode "limit=50" 2>/dev/null)
    traces=$(echo "$resp" | python3 -c "
import sys, json
try:
    print(len(json.load(sys.stdin).get('traces', [])))
except Exception:
    print(0)
" 2>/dev/null || echo 0)
    if [ "${traces:-0}" -ge 6 ] 2>/dev/null; then
        found=$traces
        break
    fi
    sleep 5
done
if [ "${found:-0}" -ge 6 ] 2>/dev/null; then
    check "≥6 spans visible in Tempo under service.name=$PROBE (got $found)" ok
else
    check "tempo span count" fail "only $found traces found after 60s"
fi

# 5) Tempo's tag-values endpoint also surfaces the probe service name
tag_values=$(curl -s "$TEMPO/api/search/tag/service.name/values" 2>/dev/null \
    | python3 -c "
import sys, json
try:
    vals = json.load(sys.stdin).get('tagValues', []) or []
    print('\n'.join(vals))
except Exception:
    pass
")
if echo "$tag_values" | grep -qx "$PROBE"; then
    check "Tempo /tag/service.name/values lists the probe service" ok
else
    check "tempo tag values" fail "probe service not in tag value list"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
