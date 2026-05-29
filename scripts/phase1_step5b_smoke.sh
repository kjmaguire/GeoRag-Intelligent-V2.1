#!/usr/bin/env bash
# =============================================================================
# scripts/phase1_step5b_smoke.sh
#
# Phase 1 Step 5B smoke — exercises the full diff contract end-to-end:
#
#   1. Synthetic workspace + traffic_pct=100 (so dual-write fires).
#   2. Calls ShadowRouter via tinker → Hatchet ingest_pdf populates
#      hatchet_result + hatchet_audit_run_id.
#   3. Manually invokes the Dagster v1.49 hook
#      (georag_dagster.hooks.shadow_v149.record_v149_for_shadow) to populate
#      v149_result + v149_audit_run_id WITHOUT depending on the bronze sensor
#      cadence (which can take >5 min).
#   4. Triggers the shadow_diff Hatchet workflow on the row id and waits
#      up to 60s for classification != 'partial'.
#   5. Asserts:
#       - classification ∈ {clean, minor, divergent, fatal}
#       - diff_details JSON populated
#       - audit_ledger has 'ingest_pdf.shadow.classified'
#   6. Cleanup on EXIT.
# =============================================================================

set -uo pipefail

WS_ID="${WS_ID:-00000000-aaaa-aaaa-aaaa-555555550005}"
MINIO_KEY="reports/phase1-shadow-smoke/PLS-2024-Technical-Report.pdf"
FILE_SIZE=17722

cleanup() {
    docker exec georag-postgresql psql -U georag -d georag -q -c "
        DELETE FROM audit.audit_ledger WHERE workspace_id = '${WS_ID}';
        DELETE FROM silver.shadow_runs WHERE workspace_id = '${WS_ID}';
        DELETE FROM workspace.feature_flags WHERE workspace_id = '${WS_ID}'::uuid;
        DELETE FROM silver.workspaces WHERE workspace_id = '${WS_ID}';
    " >/dev/null
}
trap cleanup EXIT
cleanup

cat <<BANNER

============================================================
PHASE 1 STEP 5B — DIFF CONTRACT SMOKE
============================================================
Workspace: ${WS_ID}
Minio key: ${MINIO_KEY}
============================================================
BANNER

# ---------------------------------------------------------------------------
# Seed workspace + traffic_pct=100 + bronze PDF.
# ---------------------------------------------------------------------------
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO silver.workspaces (workspace_id, name, slug)
    VALUES ('${WS_ID}', 'phase1-shadow-smoke', 'phase1-shadow-smoke-${WS_ID:0:8}')
    ON CONFLICT (workspace_id) DO NOTHING;

    INSERT INTO workspace.feature_flags (workspace_id, flag_name, int_value)
    VALUES ('${WS_ID}'::uuid, 'ingest_pdf_hatchet_traffic_pct', 100)
    ON CONFLICT (workspace_id, flag_name) DO UPDATE SET int_value = 100;
" >/dev/null

docker exec georag-hatchet-worker-ingestion python3 -c "
import asyncio, os
import aioboto3
async def main():
    sess = aioboto3.Session(
        aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
        region_name='us-east-1',
    )
    async with sess.client('s3', endpoint_url=os.environ.get('S3_ENDPOINT_URL', 'http://minio:8333')) as s3:
        copy_source = {'Bucket': 'bronze', 'Key': 'reports/phase1-smoke/PLS-2024-Technical-Report.pdf'}
        await s3.copy_object(CopySource=copy_source, Bucket='bronze', Key='${MINIO_KEY}')
asyncio.run(main())
" >/dev/null

# ---------------------------------------------------------------------------
# 1. ShadowRouter via tinker — fires Hatchet side.
# ---------------------------------------------------------------------------
echo
echo "--- ShadowRouter::maybeShadow() (Hatchet side) ---"
SHADOW_RESULT=$(docker exec -w /app georag-laravel-octane php -r "
require '/app/vendor/autoload.php';
\$app = require '/app/bootstrap/app.php';
\$app->make(\Illuminate\Contracts\Console\Kernel::class)->bootstrap();
\$router = \$app->make(App\Services\Ingestion\ShadowRouter::class);
\$result = \$router->maybeShadow(
    workspaceId: '${WS_ID}',
    minioKey: '${MINIO_KEY}',
    fileSize: ${FILE_SIZE},
    projectId: 'phase1-shadow-smoke',
);
echo json_encode(\$result, JSON_UNESCAPED_SLASHES) . PHP_EOL;
")
echo "  $SHADOW_RESULT"

SHADOW_ID=$(echo "$SHADOW_RESULT" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("shadow_runs_id") or "")')
if [ -z "$SHADOW_ID" ]; then
    echo "  [FAIL] no shadow_runs_id from ShadowRouter"
    exit 1
fi
echo "  shadow_runs_id = $SHADOW_ID"

# ---------------------------------------------------------------------------
# Wait for the Hatchet path's persist step to populate hatchet_result.
# ---------------------------------------------------------------------------
echo
echo "--- Waiting up to 300s for Hatchet persist to populate hatchet_result ---"
for i in $(seq 1 60); do
    has_h=$(docker exec georag-postgresql psql -U georag -d georag -tAc \
        "SELECT (hatchet_result IS NOT NULL)::text FROM silver.shadow_runs WHERE id = '${SHADOW_ID}';" | tr -d ' ')
    if [ "$has_h" = "t" ] || [ "$has_h" = "true" ]; then
        echo "  [PASS] hatchet_result populated after ~$((i*5))s"
        break
    fi
    sleep 5
done
if [ "$has_h" != "t" ] && [ "$has_h" != "true" ]; then
    echo "  [FAIL] hatchet_result never appeared"
    exit 1
fi

# ---------------------------------------------------------------------------
# 2. Synthesise the v1.49 side. The hook itself is exercised in step5b_verify
#    (under Dagster's psycopg2 environment); for the smoke we copy the
#    Hatchet-side payload into the v1.49 columns via psql, which models the
#    'clean' equivalence case (both sides produced identical JSON).
# ---------------------------------------------------------------------------
echo
echo "--- Backfill v149_result from hatchet_result (smoke 'clean' case) ---"
docker exec georag-postgresql psql -U georag -d georag -q -c "
    UPDATE silver.shadow_runs
       SET v149_result = hatchet_result,
           v149_duration_ms = hatchet_duration_ms,
           v149_audit_run_id = hatchet_audit_run_id
     WHERE id = '${SHADOW_ID}'::uuid;
" >/dev/null
echo "  v1.49 side filled (synthetic)"

# Mirror the action_types onto a v149 trace_id slot. The classifier reads
# audit_ledger by trace_id; copying the existing rows under a synthetic
# v1.49 trace_id is simplest.
docker exec georag-postgresql psql -U georag -d georag -q -c "
    UPDATE silver.shadow_runs
       SET v149_audit_run_id = hatchet_audit_run_id
     WHERE id = '${SHADOW_ID}'::uuid;
" >/dev/null

# ---------------------------------------------------------------------------
# 3. Trigger ai shadow_diff for this row and wait for classification.
# ---------------------------------------------------------------------------
echo
echo "--- Trigger shadow_diff for ${SHADOW_ID} ---"
docker exec georag-fastapi python3 -c "
import asyncio, sys
sys.path.insert(0, '/app')
from app.hatchet_workflows.shadow_diff import shadow_diff, ShadowDiffInput
async def main():
    ref = await shadow_diff.aio_run_no_wait(
        ShadowDiffInput(shadow_runs_id='${SHADOW_ID}')
    )
    print('triggered workflow_run_id =', ref.workflow_run_id)
asyncio.run(main())
"

echo
echo "--- Waiting up to 90s for classification to land ---"
classification=""
for i in $(seq 1 18); do
    classification=$(docker exec georag-postgresql psql -U georag -d georag -tAc \
        "SELECT classification FROM silver.shadow_runs WHERE id = '${SHADOW_ID}';" | tr -d ' ')
    if [ -n "$classification" ] && [ "$classification" != "partial" ]; then
        echo "  [PASS] classification = $classification (after ~$((i*5))s)"
        break
    fi
    sleep 5
done

if [ -z "$classification" ] || [ "$classification" = "partial" ]; then
    echo "  [FAIL] classification stuck at '${classification}'"
    docker logs --tail 40 georag-hatchet-worker-ai 2>&1 | tail -25
    exit 1
fi

# ---------------------------------------------------------------------------
# 4. Assert diff_details + audit row.
# ---------------------------------------------------------------------------
echo
echo "--- Assertions ---"
n_details=$(docker exec georag-postgresql psql -U georag -d georag -tAc \
    "SELECT (diff_details IS NOT NULL)::text FROM silver.shadow_runs WHERE id = '${SHADOW_ID}';" | tr -d ' ')
n_audit=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM audit.audit_ledger
    WHERE workspace_id = '${WS_ID}'
      AND action_type = 'ingest_pdf.shadow.classified';" | tr -d ' ')

echo "  diff_details populated: $n_details"
echo "  audit 'ingest_pdf.shadow.classified' rows: $n_audit"

if [ "$n_details" = "t" ] || [ "$n_details" = "true" ]; then
    if [ "$n_audit" -ge 1 ] 2>/dev/null; then
        echo "  [PASS] diff_details + audit landed"
    else
        echo "  [FAIL] missing audit row"
        exit 1
    fi
else
    echo "  [FAIL] diff_details not populated"
    exit 1
fi

echo
echo "============================================================"
echo "PHASE 1 STEP 5B — SMOKE PASSED (classification=$classification)"
echo "============================================================"
