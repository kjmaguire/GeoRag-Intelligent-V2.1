#!/usr/bin/env bash
# =============================================================================
# scripts/phase1_step5b_verify.sh
#
# Phase 1 Step 5B done-definition verifier:
#
#   1. shadow_diff + shadow_diff_scan workflows registered with the engine.
#   2. AI worker pool advertises both via --list.
#   3. shadow_diff workflow file imports cleanly inside the worker.
#   4. Classifier handles all 4 classifications + the side-error fatal short.
#   5. Dagster v1.49 hook module imports cleanly + record_v149_for_shadow
#      is callable.
#   6. UploadController calls ShadowRouter for category='reports'.
#   7. End-to-end smoke (delegates to phase1_step5b_smoke.sh).
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7

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
PHASE 1 STEP 5B — DONE-DEFINITION VERIFICATION
============================================================
BANNER

# 1) Engine registration
engine_check=$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc \
    "SELECT count(*) FROM \"Workflow\" WHERE name IN ('shadow_diff','shadow_diff_scan');" \
    2>/dev/null | tr -d ' ')
[ "$engine_check" = "2" ] && check "Hatchet engine knows shadow_diff + shadow_diff_scan" ok \
    || check "engine registration" fail "got $engine_check / 2"

# 2) AI pool advertises both
pool_count=$(docker exec georag-hatchet-worker-ai python3 -m app.hatchet_workflows.worker --list 2>&1 \
    | grep -cE '^shadow_diff(_scan)?$')
[ "$pool_count" = "2" ] && check "AI worker pool advertises shadow_diff + shadow_diff_scan" ok \
    || check "AI pool advertisement" fail "got $pool_count / 2"

# 3) Workflow imports cleanly
import_check=$(docker exec georag-hatchet-worker-ai python3 -c "
import sys; sys.path.insert(0, '/app')
from app.hatchet_workflows.shadow_diff import shadow_diff, shadow_diff_scan
from app.services.shadow_diff import classify_shadow_run
print('OK')
" 2>&1 | tail -1)
[ "$import_check" = "OK" ] && check "shadow_diff workflow + classifier import cleanly" ok \
    || check "import" fail "$import_check"

# 4) Classifier covers each classification path
classifier_check=$(docker exec georag-hatchet-worker-ai python3 -c "
import sys; sys.path.insert(0, '/app')
from app.services.shadow_diff import classify_shadow_run
crit = {'ingest_pdf.parse.complete', 'silver.reports.write'}
base = {
    'sha256':'a','minio_key':'k','page_count':1,'page_languages':['en'],
    'parse_quality_pct':0.5,'parser_used':'u','title':'t','company':'c',
    'project_name':'p','filing_date':'2024-01-01','commodity':'g','region':'r',
    'authors':[],'sections':[],'sections_count':0,'resource_tables':[],'resource_tables_count':0,
}
expectations = [
    ('clean',     classify_shadow_run(v149=base, hatchet=base,
                                       v149_audit_action_types=crit, hatchet_audit_action_types=crit).classification),
    ('minor',     classify_shadow_run(v149=base, hatchet=base,
                                       v149_audit_action_types=crit | {'ingest_pdf.parse.ocr_applied'},
                                       hatchet_audit_action_types=crit).classification),
    ('divergent', classify_shadow_run(v149=base, hatchet=dict(base, title='x'),
                                       v149_audit_action_types=crit, hatchet_audit_action_types=crit).classification),
    ('fatal',     classify_shadow_run(v149=base, hatchet=None, hatchet_error='boom').classification),
]
print(';'.join(f'{e}={a}' for e, a in expectations))
" 2>&1 | tail -1)
expected="clean=clean;minor=minor;divergent=divergent;fatal=fatal"
[ "$classifier_check" = "$expected" ] && check "Classifier covers all 4 classifications" ok \
    || check "classifier paths" fail "got '$classifier_check'"

# 5) Dagster v1.49 hook importable. Tested in the ingestion worker — that's
#    where the georag_dagster volume is mounted and where the asset-equivalent
#    parse path runs.
hook_check=$(docker exec georag-hatchet-worker-ingestion python3 -c "
from georag_dagster.hooks.shadow_v149 import record_v149_for_shadow
print('OK' if callable(record_v149_for_shadow) else 'NOT_CALLABLE')
" 2>&1 | tail -1)
[ "$hook_check" = "OK" ] && check "Dagster v1.49 shadow hook import + callable" ok \
    || check "hook import" fail "$hook_check"

# 6) UploadController dispatches to ShadowRouter for category='reports'
upload_check=$(docker exec georag-laravel-octane grep -c 'shadowRouter->maybeShadow' \
    /app/app/Http/Controllers/Api/V1/UploadController.php 2>/dev/null)
[ "$upload_check" -ge 1 ] 2>/dev/null \
    && check "UploadController calls ShadowRouter for PDF reports" ok \
    || check "UploadController integration" fail "no maybeShadow() call"

# 7) Smoke
echo
echo "  ── Running phase1_step5b_smoke.sh ──"
if timeout 480 bash "$(dirname "$0")/phase1_step5b_smoke.sh" > /tmp/step5b_smoke.log 2>&1; then
    smoke_class=$(grep 'SMOKE PASSED' /tmp/step5b_smoke.log | sed 's/.*classification=//' | tr -d ')')
    check "End-to-end shadow_diff smoke (classification=${smoke_class})" ok
else
    check "End-to-end smoke" fail "see /tmp/step5b_smoke.log"
    tail -20 /tmp/step5b_smoke.log
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
