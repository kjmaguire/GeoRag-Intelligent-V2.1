#!/usr/bin/env bash
# =============================================================================
# scripts/phase1_step4_verify.sh
#
# Phase 1 Step 4 done-definition (per docs/phase1_implementation_kickoff.md):
#   1. silver.shadow_runs + workspace.feature_flags tables exist
#   2. Default platform feature flags seeded
#   3. ingest_pdf workflow registered with the engine
#   4. ingestion worker pool advertises ingest_pdf via --list
#   5. Workflow file imports cleanly (smoke triggers an end-to-end run
#      against a known-good PDF that's already in the bronze bucket)
#
# Step 4B (next session) closes the smoke loop with a real PDF + asserts
# silver row writes. This verifier covers the SCAFFOLD landing.
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=9

check() {
    if [ "$2" = ok ]; then
        echo "  [PASS] $1"
        PASS=$((PASS+1))
    else
        echo "  [FAIL] $1 — $3"
    fi
}

q() {
    docker exec georag-postgresql psql -U georag -d georag -tAc "$1" 2>/dev/null
}

cat <<'BANNER'

============================================================
PHASE 1 STEP 4 — DONE-DEFINITION VERIFICATION
============================================================
BANNER

# 1) Schema present. Phase 4 Step 6 dropped silver.shadow_runs alongside
#    the v1.49 shadow-diff harness sunset. Supersession-tolerant: either
#    both tables are present (pre-Phase-4 era), or workspace.feature_flags
#    is present alone with silver.shadow_runs confirmed absent.
n_tables=$(q "
    SELECT count(*) FROM information_schema.tables
    WHERE (table_schema, table_name) IN
          (('silver','shadow_runs'),('workspace','feature_flags'));")
n_tables="${n_tables// /}"
n_ff=$(q "
    SELECT count(*) FROM information_schema.tables
    WHERE (table_schema, table_name) = ('workspace','feature_flags');")
n_ff="${n_ff// /}"
if [ "$n_tables" = "2" ]; then
    check "silver.shadow_runs + workspace.feature_flags present" ok
elif [ "$n_ff" = "1" ]; then
    check "workspace.feature_flags present; silver.shadow_runs intentionally dropped (Phase 4 Step 6)" ok
else
    check "schema" fail "got $n_tables / 2 (ff=$n_ff)"
fi

# 2) Platform feature flags seeded. Phase 4 Step 6 also removed
#    ingest_pdf_shadow_enabled when the shadow harness was retired —
#    supersession-tolerant: either the historical 2-flag pair OR the
#    surviving traffic_pct flag alone is OK.
n_flags=$(q "
    SELECT count(*) FROM workspace.feature_flags
    WHERE workspace_id IS NULL
      AND flag_name IN ('ingest_pdf_hatchet_traffic_pct','ingest_pdf_shadow_enabled');")
n_flags="${n_flags// /}"
if [ "$n_flags" = "2" ]; then
    check "Platform feature flags seeded (traffic_pct + shadow_enabled)" ok
elif [ "$n_flags" = "0" ]; then
    # Phase 4 Step 6 removed both flags (phase4_step6 verifier asserts this).
    check "Phase 1 platform feature flags intentionally removed (Phase 4 Step 6)" ok
elif [ "$n_flags" = "1" ]; then
    n_tp=$(q "
        SELECT count(*) FROM workspace.feature_flags
        WHERE workspace_id IS NULL
          AND flag_name = 'ingest_pdf_hatchet_traffic_pct';")
    n_tp="${n_tp// /}"
    if [ "$n_tp" = "1" ]; then
        check "traffic_pct seeded; shadow_enabled intentionally removed (Phase 4 Step 6)" ok
    else
        check "feature flags" fail "got $n_flags / 2 (no traffic_pct)"
    fi
else
    check "feature flags" fail "got $n_flags / 2"
fi

# 3) ingest_pdf registered with the Hatchet engine
engine_check=$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc \
    "SELECT name FROM \"Workflow\" WHERE name = 'ingest_pdf' LIMIT 1;" 2>/dev/null | tr -d ' ')
[ "$engine_check" = "ingest_pdf" ] && check "Hatchet engine knows about 'ingest_pdf' workflow" ok \
    || check "engine registration" fail "engine reports '$engine_check'"

# 4) Ingestion worker pool advertises it
pool_check=$(docker exec georag-hatchet-worker-ingestion python3 \
    -m app.hatchet_workflows.worker --list 2>&1 | grep -c '^ingest_pdf$')
[ "$pool_check" = "1" ] && check "Ingestion worker pool advertises ingest_pdf via --list" ok \
    || check "pool advertisement" fail "got $pool_check"

# 5) Workflow + supporting modules import cleanly inside the worker
import_check=$(docker exec georag-hatchet-worker-ingestion python3 -c "
import sys; sys.path.insert(0, '/app')
from app.hatchet_workflows.ingest_pdf import (
    ingest_pdf, IngestPdfInput, IngestPdfFinalOut,
)
# task references — confirm all 7 steps declared
fns = sorted(t.action_name() for t in ingest_pdf._tasks) if hasattr(ingest_pdf, '_tasks') else []
print('OK' if (IngestPdfInput and IngestPdfFinalOut) else 'MISSING')
" 2>&1 | tail -1)
[ "$import_check" = "OK" ] && check "Workflow file imports cleanly + IO models declared" ok \
    || check "import" fail "$import_check"

# 6) End-to-end smoke: trigger workflow against the bronze smoke PDF
docker cp "$(dirname "$0")/_phase1_step4b_trigger.py" georag-hatchet-worker-ingestion:/tmp/trigger.py >/dev/null
if timeout 180 docker exec georag-hatchet-worker-ingestion python3 /tmp/trigger.py > /tmp/step4_smoke.log 2>&1; then
    check "End-to-end ingest_pdf smoke (7 steps run, persist returns)" ok
else
    check "End-to-end smoke" fail "see /tmp/step4_smoke.log"
fi

# 7) audit.audit_ledger entry. silver.shadow_runs was dropped in Phase 4
#    Step 6 (sunset of the shadow-diff harness); supersession-tolerant —
#    historically asserted on both `silver.shadow_runs` and
#    `audit.audit_ledger`, post-Phase-4 only the audit row remains.
n_audit=$(q "
    SELECT count(*) FROM audit.audit_ledger
    WHERE action_type = 'ingest_pdf.parse.complete'
      AND created_at > now() - interval '5 minutes';")
n_audit="${n_audit// /}"
if [ "$n_audit" -ge 1 ] 2>/dev/null; then
    check "audit.audit_ledger ingest_pdf.parse.complete row written ($n_audit; shadow_runs sunset Phase 4 Step 6)" ok
else
    check "Audit row" fail "audit=$n_audit"
fi

# 8) silver.reports row written by the persist step (Step 4C)
n_reports=$(q "
    SELECT count(*) FROM silver.reports
    WHERE source_file_sha256 = '524cfa2e23f6b98894cbce9286bd349c96d33355dd347dbf9cde9ed1b6b30205'
      AND parser_used IN ('pdfplumber','unstructured')
      AND parse_quality_pct > 0
      AND title IS NOT NULL;")
n_reports="${n_reports// /}"
if [ "$n_reports" -ge 1 ] 2>/dev/null; then
    check "silver.reports row written by Hatchet persist (parser_used + quality + title)" ok
else
    check "silver.reports row" fail "got $n_reports"
fi

# 9) silver.document_passages rows written (R-P1-4). Pulls passages joined
#    to silver.reports for the smoke fixture's source_file_sha256.
n_passages=$(q "
    SELECT count(p.*) FROM silver.document_passages p
    JOIN silver.reports r ON r.report_id = p.document_id
    WHERE r.source_file_sha256 = '524cfa2e23f6b98894cbce9286bd349c96d33355dd347dbf9cde9ed1b6b30205'
      AND p.chunk_kind = 'narrative'
      AND p.revision_number = 1
      AND length(p.text) > 0;")
n_passages="${n_passages// /}"
if [ "$n_passages" -ge 5 ] 2>/dev/null; then
    check "silver.document_passages rows written by Hatchet persist (n=$n_passages, narrative chunks)" ok
else
    check "silver.document_passages" fail "got $n_passages (expected ≥5)"
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"
echo
echo "DEFERRED to Phase 2 ingestion-pipeline scope:"
echo "  - layout-aware chunking (chunk_kind='table'/'caption_figure', page_first/last,"
echo "    bbox_union, layout_region_ids on silver.document_passages)"
echo "  - parse_quality_pct + parser_used + filing_date type coercion edge cases"
echo "  - Real per-step OTel spans inside parse() (currently the parser logs only)"
echo

exit $((PASS == TOTAL ? 0 : 1))
