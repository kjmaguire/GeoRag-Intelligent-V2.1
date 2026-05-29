#!/usr/bin/env bash
# =============================================================================
# scripts/phase5_step4_verify.sh
#
# Phase 5 Step 4 done-definition — per-step OTel spans in parse_pdf_report
# (R-P3-7).
#
#   1. observability/ package exists in the dagster module
#   2. install_tracer_provider + get_tracer importable
#   3. install_tracer_provider returns False with no OTLP endpoint (no-op)
#   4. install_tracer_provider returns True when endpoint is configured
#   5. pdf_report module exposes a _tracer attr at load
#   6. pdf_report.py source has all 7 stage spans (preflight, unstructured,
#      pdfplumber, ocr, metadata, sections, resource_tables)
#   7. pdf_report still importable end-to-end inside the ingestion worker
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
REPO="${REPO:-/home/georag/projects/georag}"
WORKER="${WORKER_CONTAINER:-georag-hatchet-worker-ingestion}"
PARSER_PY="$REPO/src/dagster/georag_dagster/parsers/pdf_report.py"
OTEL_PY="$REPO/src/dagster/georag_dagster/observability/otel.py"

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
PHASE 5 STEP 4 — PARSE_PDF_REPORT OTEL SPAN VERIFICATION
============================================================
BANNER

# 1) Package files present
if [ -f "$OTEL_PY" ] && [ -f "$REPO/src/dagster/georag_dagster/observability/__init__.py" ]; then
    check "observability package present" ok
else
    check "package present" fail "missing $OTEL_PY or __init__.py"
fi

# 2-4) install_tracer_provider behavior in the worker container
probe=$(docker exec "$WORKER" python3 -c "
import os, sys
sys.path.insert(0, '/app')
os.environ.pop('OTEL_EXPORTER_OTLP_ENDPOINT', None)
from georag_dagster.observability import install_tracer_provider, get_tracer
print('no_endpoint', install_tracer_provider('p5s4-probe'))
t = get_tracer('p5s4-probe')
print('tracer_callable', hasattr(t, 'start_as_current_span'))
os.environ['OTEL_EXPORTER_OTLP_ENDPOINT'] = 'http://otel-collector:4318'
os.environ['OTEL_SERVICE_NAME'] = 'p5s4-probe'
print('with_endpoint', install_tracer_provider('p5s4-probe'))
" 2>&1)
case "$probe" in
    *no_endpoint\ False*)
        check "install_tracer_provider() no-op when OTLP unset" ok ;;
    *) check "no-endpoint path" fail "$probe" ;;
esac
case "$probe" in
    *tracer_callable\ True*)
        check "get_tracer() returns a usable tracer" ok ;;
    *) check "tracer callable" fail "$probe" ;;
esac
case "$probe" in
    *with_endpoint\ True*)
        check "install_tracer_provider() installs SDK provider with OTLP env" ok ;;
    *) check "with-endpoint path" fail "$probe" ;;
esac

# 5) pdf_report module exposes _tracer
attr=$(docker exec "$WORKER" python3 -c "
import sys, importlib.util
sys.path.insert(0, '/app')
spec = importlib.util.spec_from_file_location(
    'georag_dagster.parsers.pdf_report',
    '/app/georag_dagster/parsers/pdf_report.py',
)
mod = importlib.util.module_from_spec(spec)
sys.modules['georag_dagster.parsers.pdf_report'] = mod
spec.loader.exec_module(mod)
print('has_tracer', hasattr(mod, '_tracer'))
" 2>&1 | tail -1)
[ "$attr" = "has_tracer True" ] \
    && check "pdf_report exposes _tracer module attr at load" ok \
    || check "module attr" fail "$attr"

# 6) Source spans — exactly the 7 expected stage names
mapfile -t found < <(grep -oE "start_as_current_span\([\"']pdf_report\.[a-z_]+" "$PARSER_PY" \
    | sed -E "s/.*pdf_report\.//; s/[\"'].*//" | sort -u)
expected=(metadata ocr pdfplumber preflight resource_tables sections unstructured)
if [ "${found[*]}" = "${expected[*]}" ]; then
    check "All 7 stage spans present (preflight, unstructured, pdfplumber, ocr, metadata, sections, resource_tables)" ok
else
    check "stage spans" fail "got ${found[*]}; expected ${expected[*]}"
fi

# 7) End-to-end importability inside the worker (via the canonical package
#    path so importing other parsers doesn't break — pdf_report.py is the
#    target, not the parsers/__init__.py umbrella).
import_ok=$(docker exec "$WORKER" python3 -c "
import sys, importlib.util
sys.path.insert(0, '/app')
spec = importlib.util.spec_from_file_location(
    'georag_dagster.parsers.pdf_report',
    '/app/georag_dagster/parsers/pdf_report.py',
)
mod = importlib.util.module_from_spec(spec)
sys.modules['georag_dagster.parsers.pdf_report'] = mod
try:
    spec.loader.exec_module(mod)
    print('import_ok', callable(getattr(mod, 'parse_pdf_report', None)))
except Exception as e:
    print('import_fail', repr(e))
" 2>&1 | tail -1)
[ "$import_ok" = "import_ok True" ] \
    && check "parse_pdf_report importable + callable in worker" ok \
    || check "import" fail "$import_ok"

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
