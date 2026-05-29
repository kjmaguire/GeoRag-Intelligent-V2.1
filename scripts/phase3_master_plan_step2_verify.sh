#!/usr/bin/env bash
# Master-plan §3 Step 2 verifier (doc-phase 50).
#
# Confirms the 8 §9.3 + §9.6 silver tables exist with the correct
# structural facts: schema, indexes, RLS, FK targets. Does NOT assert
# anything about data — those checks land in Step 3+ when parsers
# actually populate the tables.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Doc-phase 62 — manifest helper for cascade speedup
source "$SCRIPT_DIR/_verifier_manifest.sh"

CONTAINER="${CONTAINER:-georag-postgresql}"

FAIL=0
note() { echo "$1"; }

PSQL="docker exec $CONTAINER psql -U georag -d georag -tAX"

# 1. All 8 tables present
EXPECTED_TABLES="ocr_page_quality document_ingestion_quality table_extraction_quality parser_run_artifacts low_confidence_page_reviews ingest_extractions ingest_layouts ingest_ocr_results"
MISSING=""
for t in $EXPECTED_TABLES; do
    EXISTS=$($PSQL -c "SELECT 1 FROM pg_tables WHERE schemaname='silver' AND tablename='$t';")
    if [ "$EXISTS" != "1" ]; then
        MISSING="$MISSING $t"
    fi
done
if [ -z "$MISSING" ]; then
    note "[check1] PASS — all 8 silver tables exist"
else
    note "[check1] FAIL — missing tables:$MISSING"
    FAIL=$((FAIL + 1))
fi

# 2. RLS enabled on all 8 tables
RLS_OFF=""
for t in $EXPECTED_TABLES; do
    ENABLED=$($PSQL -c "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname='$t' AND relnamespace = 'silver'::regnamespace;")
    if [ "$ENABLED" != "t" ]; then
        RLS_OFF="$RLS_OFF $t"
    fi
done
if [ -z "$RLS_OFF" ]; then
    note "[check2] PASS — RLS enabled + forced on all 8 tables"
else
    note "[check2] FAIL — RLS missing on:$RLS_OFF"
    FAIL=$((FAIL + 1))
fi

# 3. tenant_isolation policy on all 8 tables
POLICY_MISSING=""
for t in $EXPECTED_TABLES; do
    EXISTS=$($PSQL -c "SELECT 1 FROM pg_policies WHERE schemaname='silver' AND tablename='$t' AND policyname='tenant_isolation';")
    if [ "$EXISTS" != "1" ]; then
        POLICY_MISSING="$POLICY_MISSING $t"
    fi
done
if [ -z "$POLICY_MISSING" ]; then
    note "[check3] PASS — tenant_isolation policy on all 8 tables"
else
    note "[check3] FAIL — policy missing on:$POLICY_MISSING"
    FAIL=$((FAIL + 1))
fi

# 4. (pdf_id, page) or (pdf_id, page, region) primary keys / indexes
# Tables that should be keyed by (report_id, page):
PAGE_KEYED="ocr_page_quality"
# Tables that should be keyed by (report_id, page, region):
REGION_KEYED="ingest_extractions ingest_layouts ingest_ocr_results"

for t in $PAGE_KEYED; do
    HAS_KEY=$($PSQL -c "SELECT 1 FROM pg_constraint WHERE conname = '${t}_pkey' AND pg_get_constraintdef(oid) LIKE '%report_id%page%';")
    if [ "$HAS_KEY" != "1" ]; then
        note "[check4] FAIL — silver.$t missing (report_id, page) PK"
        FAIL=$((FAIL + 1))
    fi
done
for t in $REGION_KEYED; do
    HAS_KEY=$($PSQL -c "SELECT 1 FROM pg_constraint WHERE conname = '${t}_pkey' AND pg_get_constraintdef(oid) LIKE '%report_id%page%region%';")
    if [ "$HAS_KEY" != "1" ]; then
        note "[check4] FAIL — silver.$t missing (report_id, page, region) PK"
        FAIL=$((FAIL + 1))
    fi
done
if [ $FAIL -eq 0 ]; then
    note "[check4] PASS — page + region composite keys present"
fi

# 5. workspace_id FK to silver.workspaces on all 8 tables
# Check via pg_class join — search_path resolution means the constraint
# definition may not include the schema qualifier.
FK_MISSING=""
for t in $EXPECTED_TABLES; do
    HAS_FK=$($PSQL -c "SELECT 1 FROM pg_constraint c JOIN pg_class tgt ON tgt.oid = c.confrelid JOIN pg_namespace tgt_ns ON tgt_ns.oid = tgt.relnamespace WHERE c.conrelid = ('silver.'||quote_ident('$t'))::regclass AND c.contype='f' AND tgt_ns.nspname='silver' AND tgt.relname='workspaces';")
    if [ "$HAS_FK" != "1" ]; then
        FK_MISSING="$FK_MISSING $t"
    fi
done
if [ -z "$FK_MISSING" ]; then
    note "[check5] PASS — workspace_id FK to silver.workspaces on all 8 tables"
else
    note "[check5] FAIL — workspace_id FK missing on:$FK_MISSING"
    FAIL=$((FAIL + 1))
fi

# 6. Laravel migrations table records all 9 entries
RECORDED=$($PSQL -c "SELECT COUNT(*) FROM migrations WHERE migration LIKE '2026_05_12_18000%';")
if [ "$RECORDED" = "9" ]; then
    note "[check6] PASS — Laravel migrations table records all 9 entries (8 tables + RLS)"
else
    note "[check6] FAIL — expected 9 migration entries; found $RECORDED"
    FAIL=$((FAIL + 1))
fi

echo ""
echo "=== Phase 3 master-plan Step 2 verifier summary ==="
echo "  $((6 - FAIL))/6 checks passed"

# Doc-phase 62 — record success in the cascade manifest.
if [ $FAIL -eq 0 ]; then
    mark_verifier_passed "step2"
fi

exit $FAIL
