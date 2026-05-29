#!/usr/bin/env bash
# Doc-phase 65 verifier — §04p dual-write Prometheus alerting.
#
# Closes the doc-phase 59 §5.3 carry-over ("No alerting on §04p
# dual-write failures"). Asserts:
#   1. Counters defined in app.metrics
#   2. Counters incremented from ingest_pdf.persist on success/failure
#   3. Prometheus rule file exists + promtool-valid
#   4. Steps 1-8f cascade green (manifest-cached)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$REPO_ROOT"

source "$SCRIPT_DIR/_verifier_manifest.sh"

FASTAPI_CONTAINER="${FASTAPI_CONTAINER:-georag-fastapi}"
PROM_CONTAINER="${PROM_CONTAINER:-georag-prometheus}"

FAIL=0
note() { echo "$1"; }

# ----------------------------------------------------------------------
# Check 1 — counters defined
# ----------------------------------------------------------------------
if docker exec "$FASTAPI_CONTAINER" python -c "
from app.metrics import P04P_DUAL_WRITE_SUCCESS, P04P_DUAL_WRITE_FAILURES
assert P04P_DUAL_WRITE_SUCCESS._name == 'georag_p04p_dual_write_success'
assert P04P_DUAL_WRITE_FAILURES._name == 'georag_p04p_dual_write_failures'
# Verify the labelnames for failures counter
assert 'error_kind' in P04P_DUAL_WRITE_FAILURES._labelnames
" >/dev/null 2>&1; then
    note "[check1] PASS — P04P dual-write counters defined with correct labels"
else
    note "[check1] FAIL — counters missing or wrong shape"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 2 — ingest_pdf.persist references the counters
# ----------------------------------------------------------------------
if grep -q "P04P_DUAL_WRITE_SUCCESS" "$REPO_ROOT/src/fastapi/app/hatchet_workflows/ingest_pdf.py" \
   && grep -q "P04P_DUAL_WRITE_FAILURES" "$REPO_ROOT/src/fastapi/app/hatchet_workflows/ingest_pdf.py"; then
    note "[check2] PASS — ingest_pdf.persist references both counters"
else
    note "[check2] FAIL — counters not wired in ingest_pdf"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 3 — Prometheus rule file + promtool valid
# ----------------------------------------------------------------------
RULE_FILE="$REPO_ROOT/docker/prometheus/rules/p04p-dual-write-alerts.yml"
if [ -f "$RULE_FILE" ]; then
    if docker exec "$PROM_CONTAINER" promtool check rules /etc/prometheus/rules/p04p-dual-write-alerts.yml 2>/dev/null | grep -q "SUCCESS"; then
        note "[check3] PASS — alert rules file exists + promtool validates"
    else
        note "[check3] FAIL — promtool rejected the rule file"
        FAIL=$((FAIL + 1))
    fi
else
    note "[check3] FAIL — alert rules file missing"
    FAIL=$((FAIL + 1))
fi

# ----------------------------------------------------------------------
# Check 4+ — Steps 1-8f cascade green
# ----------------------------------------------------------------------
for step in 1 2 3 4 5 6 7a 7b 7c 8a 8b 8c 8d 8e 8f; do
    if check_verifier_recent "step${step}"; then
        note "[step${step}] PASS — manifest recent (skip re-run)"
    elif bash "$SCRIPT_DIR/phase3_master_plan_step${step}_verify.sh" >/dev/null 2>&1; then
        note "[step${step}] PASS — verifier re-run green"
    else
        note "[step${step}] FAIL — verifier regressed"
        FAIL=$((FAIL + 1))
    fi
done

echo ""
echo "=== Phase 3 master-plan Step 8g verifier summary ==="
echo "  (18 checks total; all must pass)"

if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "step8g"
fi

exit $FAIL
