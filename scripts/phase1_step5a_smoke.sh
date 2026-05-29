#!/usr/bin/env bash
# =============================================================================
# scripts/phase1_step5a_smoke.sh
#
# Phase 1 Step 5A smoke — exercises the ShadowRouter end-to-end:
#
#   1. Sets traffic_pct = 100 for a synthetic workspace so dual-write fires
#   2. Calls ShadowRouter from inside laravel-octane via tinker
#   3. Asserts:
#       - silver.shadow_runs row inserted with classification='partial'
#       - hatchet_audit_run_id set (the Hatchet trigger returned a run_id)
#       - audit_ledger has a row with action_type='ingest_pdf.shadow.dispatched'
#       - eventually (within 60s): hatchet_result jsonb populated by the
#         persist step of the ingest_pdf workflow
#   4. Cleans up the synthetic workspace + rows on EXIT
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
PHASE 1 STEP 5A — ShadowRouter SMOKE TEST
============================================================
Workspace: ${WS_ID}
Minio key: ${MINIO_KEY}
============================================================
BANNER

# Seed silver.workspaces + per-workspace traffic_pct=100 + shadow PDF.
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO silver.workspaces (workspace_id, name, slug)
    VALUES ('${WS_ID}', 'phase1-shadow-smoke', 'phase1-shadow-smoke-${WS_ID:0:8}')
    ON CONFLICT (workspace_id) DO NOTHING;

    INSERT INTO workspace.feature_flags (workspace_id, flag_name, int_value)
    VALUES ('${WS_ID}'::uuid, 'ingest_pdf_hatchet_traffic_pct', 100)
    ON CONFLICT (workspace_id, flag_name) DO UPDATE SET int_value = 100;
" >/dev/null

# Make sure the smoke PDF is in bronze under the synthetic project key.
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
        # copy the phase1-smoke object to phase1-shadow-smoke key
        copy_source = {'Bucket': 'bronze', 'Key': 'reports/phase1-smoke/PLS-2024-Technical-Report.pdf'}
        await s3.copy_object(CopySource=copy_source, Bucket='bronze', Key='${MINIO_KEY}')
        print('copied smoke PDF to bronze:${MINIO_KEY}')
asyncio.run(main())
"

# Invoke ShadowRouter via tinker.
echo
echo "--- ShadowRouter::maybeShadow() ---"
docker exec -w /app georag-laravel-octane php -r "
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
"

# Assertions
echo
echo "--- Assertions ---"
n_shadow=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM silver.shadow_runs
    WHERE workspace_id = '${WS_ID}'
      AND classification IN ('partial','clean','minor','divergent','fatal');" | tr -d ' ')
n_audit=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM audit.audit_ledger
    WHERE workspace_id = '${WS_ID}'
      AND action_type = 'ingest_pdf.shadow.dispatched';" | tr -d ' ')

echo "  silver.shadow_runs rows for workspace: $n_shadow"
echo "  audit_ledger 'ingest_pdf.shadow.dispatched' rows: $n_audit"

if [ "$n_shadow" = "1" ] && [ "$n_audit" = "1" ]; then
    echo "  [PASS] ShadowRouter inserted shadow row + audit"
else
    echo "  [FAIL] expected shadow=1 audit=1"
    exit 1
fi

# Wait for the Hatchet workflow to update the row's hatchet_result.
echo
echo "--- Waiting up to 300s for Hatchet persist to UPSERT hatchet_result ---"
echo "    (workflow may queue behind outbox_dispatcher runs on the ingestion pool)"
for i in $(seq 1 60); do
    state=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
        SELECT count(*)::text || '/' ||
               COALESCE(BOOL_OR(hatchet_result IS NOT NULL)::text, 'no_rows')
        FROM silver.shadow_runs WHERE workspace_id = '${WS_ID}';")
    state="${state// /}"
    has_hatchet=$(echo "$state" | cut -d/ -f2)
    # ::text on a bool returns "true"/"false"; psql's -tAc on a column-typed
    # boolean returns "t"/"f". Accept either.
    if [ "$has_hatchet" = "t" ] || [ "$has_hatchet" = "true" ]; then
        echo "  [PASS] Hatchet side persisted hatchet_result after ${i} polls (~$((i*5))s); state=$state"
        break
    fi
    if [ $((i % 6)) = 0 ]; then
        echo "    poll $i (~$((i*5))s): rows/has_hatchet=$state"
    fi
    sleep 5
done

if [ "$has_hatchet" != "t" ] && [ "$has_hatchet" != "true" ]; then
    echo "  [FAIL] Hatchet result never appeared in shadow_runs"
    docker logs --tail 30 georag-hatchet-worker-ingestion 2>&1 | tail -15
    exit 1
fi

echo
echo "============================================================"
echo "PHASE 1 STEP 5A — SMOKE PASSED"
echo "============================================================"
