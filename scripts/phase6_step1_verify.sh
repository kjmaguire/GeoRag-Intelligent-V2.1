#!/usr/bin/env bash
# =============================================================================
# scripts/phase6_step1_verify.sh
#
# Phase 6 Step 1 done-definition — tracer bootstrap wired at worker
# startup + parse spans actually export to Tempo (R-P5-1, R-P5-3,
# R-P5-4 fold-in).
#
#   1. worker.py main() calls install_tracer_provider()
#   2. pdf_report.py no longer self-bootstraps at module-load
#   3. Both hatchet workers booted with OTel env + logged install
#   4. ingestion worker has the dagster bind mount (parser visible)
#   5. AI worker has the dagster bind mount + R-P5-4 env (kestra JWT)
#   6. Tempo is reachable
#   7. Running parse_pdf_report against a fixture emits ≥6 stage spans
#      that land in Tempo within 60s (queried via TraceQL by
#      resource.service.name).
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
REPO="${REPO:-/home/georag/projects/georag}"
INGEST="${INGEST_CONTAINER:-georag-hatchet-worker-ingestion}"
AI="${AI_CONTAINER:-georag-hatchet-worker-ai}"
TEMPO="${TEMPO_URL:-http://localhost:3200}"
PROBE_SVC="p6s1-probe-$(date +%s)"
FIXTURE_HOST="$REPO/src/dagster/tests/fixtures/reports/PLS-2024-Technical-Report.pdf"

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
PHASE 6 STEP 1 — WORKER OTEL BOOTSTRAP + TEMPO E2E
============================================================
BANNER

# 1) Bootstrap call in worker.py main()
if grep -q 'install_tracer_provider(default_service_name=worker_name)' \
        "$REPO/src/fastapi/app/hatchet_workflows/worker.py"; then
    check "worker.py main() installs the TracerProvider" ok
else
    check "worker bootstrap" fail "install_tracer_provider not wired into main()"
fi

# 2) pdf_report.py no longer self-bootstraps
if grep -q 'install_tracer_provider' \
        "$REPO/src/dagster/georag_dagster/parsers/pdf_report.py"; then
    check "pdf_report stops self-bootstrapping" fail \
        "install_tracer_provider still imported by parser"
else
    check "pdf_report stops self-bootstrapping at module-load" ok
fi

# 3) Both workers booted with otel install logged
ingest_ok=$(docker logs "$INGEST" 2>&1 | grep -c 'otel: tracer install -> True')
ai_ok=$(docker logs "$AI" 2>&1 | grep -c 'otel: tracer install -> True')
if [ "$ingest_ok" -ge 1 ] && [ "$ai_ok" -ge 1 ]; then
    check "Both worker pools logged successful otel install" ok
else
    check "worker logs" fail "ingestion=$ingest_ok ai=$ai_ok"
fi

# 4) ingestion bind mount
if docker exec "$INGEST" test -d /app/georag_dagster/parsers; then
    check "ingestion worker sees georag_dagster mount" ok
else
    check "ingestion mount" fail "/app/georag_dagster missing on ingestion"
fi

# 5) AI worker mount + R-P5-4 env
ai_mount=$(docker exec "$AI" test -d /app/georag_dagster/parsers && echo y || echo n)
ai_jwt=$(docker exec "$AI" printenv KESTRA_FLOW_JWT_SECRET 2>/dev/null | head -c 8)
if [ "$ai_mount" = "y" ] && [ -n "$ai_jwt" ]; then
    check "AI worker has dagster mount + KESTRA_FLOW_JWT_SECRET (R-P5-4)" ok
else
    check "AI worker env" fail "mount=$ai_mount jwt=${ai_jwt:0:4}"
fi

# 6) Tempo reachable
tempo_ready=$(curl -s -o /dev/null -w '%{http_code}' "$TEMPO/ready" 2>/dev/null || echo 000)
[ "$tempo_ready" = "200" ] \
    && check "Tempo /ready returns 200" ok \
    || check "tempo ready" fail "got $tempo_ready"

# 7) End-to-end: parse the fixture under a probe service name + flush + query
if [ ! -f "$FIXTURE_HOST" ]; then
    check "fixture present + e2e probe" fail "missing $FIXTURE_HOST"
else
    # Copy the fixture into the worker (the bind mount excludes tests/).
    docker exec "$INGEST" mkdir -p /tmp/p6s1 >/dev/null 2>&1
    docker cp "$FIXTURE_HOST" "$INGEST:/tmp/p6s1/fixture.pdf" >/dev/null

    # Run a fresh-process probe inside the worker. It installs its own
    # TracerProvider (service.name = $PROBE_SVC) and forces a flush
    # after parse, so spans hit Tempo before the docker exec returns.
    probe_out=$(docker exec \
        -e OTEL_EXPORTER_OTLP_ENDPOINT="${OTEL_EXPORTER_OTLP_ENDPOINT:-http://otel-collector:4318}" \
        -e OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf" \
        -e OTEL_SERVICE_NAME="$PROBE_SVC" \
        "$INGEST" python3 -c "
import sys, os
sys.path.insert(0, '/app')
from georag_dagster.observability import install_tracer_provider
ok = install_tracer_provider(default_service_name='$PROBE_SVC')
print('install:', ok)

import importlib.util
spec = importlib.util.spec_from_file_location(
    'georag_dagster.parsers.pdf_report',
    '/app/georag_dagster/parsers/pdf_report.py',
)
mod = importlib.util.module_from_spec(spec)
sys.modules['georag_dagster.parsers.pdf_report'] = mod
spec.loader.exec_module(mod)

try:
    result = mod.parse_pdf_report('/tmp/p6s1/fixture.pdf')
    print('parsed:', len(result.sections), 'sections')
except Exception as e:
    print('parse_error:', repr(e))

# Force-flush the BatchSpanProcessor so spans hit the collector.
from opentelemetry import trace
provider = trace.get_tracer_provider()
flush_ok = False
if hasattr(provider, 'force_flush'):
    flush_ok = provider.force_flush(timeout_millis=10000)
print('flush:', flush_ok)
" 2>&1)
    echo "    probe_out: $(echo "$probe_out" | tr '\n' '|')"

    # Poll Tempo for matching spans. Tempo's TraceQL needs the service
    # name escaped. We expect ≥6 spans (one per parse stage; OCR is
    # conditional on low-text PDFs and may be skipped on this fixture).
    found=0
    for _ in $(seq 1 12); do
        # /api/search?tags is the v1 endpoint that accepts resource.service.name
        resp=$(curl -s --get "$TEMPO/api/search" \
            --data-urlencode "tags=service.name=$PROBE_SVC" \
            --data-urlencode "limit=50" 2>/dev/null)
        traces=$(echo "$resp" | python3 -c "
import sys, json
try:
    d = json.loads(sys.stdin.read())
    print(len(d.get('traces', [])))
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
        check "parse_pdf_report emitted ≥6 spans visible in Tempo (got $found)" ok
    else
        check "tempo e2e" fail "only $found traces found for service.name=$PROBE_SVC after 60s"
    fi
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
