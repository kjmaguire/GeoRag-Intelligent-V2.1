#!/usr/bin/env bash
# =============================================================================
# scripts/phase2_step4_smoke.sh
#
# Phase 2 Step 4 smoke — exercises the public_geoscience_pull bridge
# end-to-end WITHOUT depending on the Activepieces UI:
#
#   1. Sets activepieces.public_geoscience_pull.enabled = true
#   2. Drops a tiny synthetic GeoJSON in bronze under a known key
#   3. POSTs /internal/v1/integrations/public_geoscience_pull/trigger
#   4. Waits for the Hatchet workflow to complete (v1_runs_olap status)
#   5. Asserts bronze.provenance has a row pointing at the S3 key
#   6. Asserts audit.audit_ledger has 'public_geoscience.pull.complete'
#   7. Cleanup on EXIT — flag back to false, provenance row deleted,
#      S3 object removed
# =============================================================================

set -uo pipefail

ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
KEY=$(awk -F= '/^FASTAPI_SERVICE_KEY=/ { print $2 }' "$ENVFILE" 2>/dev/null | head -1)
BASE="${FASTAPI_INTERNAL_URL:-http://localhost:8000}"

SOURCE_ID="phase2-step4-smoke"
MINIO_KEY="public_geoscience/${SOURCE_ID}/$(date -u +%Y%m%dT%H%M%S)Z.geojson"

cleanup() {
    docker exec georag-postgresql psql -U georag -d georag -q -c "
        UPDATE workspace.feature_flags
           SET bool_value = false, updated_at = now()
         WHERE workspace_id IS NULL
           AND flag_name = 'activepieces.public_geoscience_pull.enabled';
        DELETE FROM bronze.provenance
         WHERE source_file = 's3://bronze/${MINIO_KEY}';
        DELETE FROM audit.audit_ledger
         WHERE action_type = 'public_geoscience.pull.complete'
           AND payload->>'minio_key' = '${MINIO_KEY}';
    " >/dev/null
    docker exec georag-hatchet-worker-ai python3 -c "
import asyncio, os
import aioboto3
async def main():
    sess = aioboto3.Session(
        aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
        region_name='us-east-1',
    )
    async with sess.client('s3', endpoint_url=os.environ.get('S3_ENDPOINT_URL', 'http://minio:8333')) as s3:
        try:
            await s3.delete_object(Bucket='bronze', Key='${MINIO_KEY}')
        except Exception:
            pass
asyncio.run(main())
" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cat <<BANNER

============================================================
PHASE 2 STEP 4 — public_geoscience_pull SMOKE
============================================================
Source ID : ${SOURCE_ID}
Minio key : ${MINIO_KEY}
============================================================
BANNER

# 1. Enable the flag.
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO workspace.feature_flags
        (workspace_id, flag_name, bool_value, updated_at)
    VALUES (NULL, 'activepieces.public_geoscience_pull.enabled', true, now())
    ON CONFLICT (workspace_id, flag_name) DO UPDATE
        SET bool_value = EXCLUDED.bool_value, updated_at = now();
" >/dev/null
echo "  flag enabled"

# 2. Drop a synthetic GeoJSON. 2-feature FeatureCollection — small + fast.
docker exec georag-hatchet-worker-ai python3 -c "
import asyncio, os, json
import aioboto3
GEOJSON = json.dumps({
    'type': 'FeatureCollection',
    'name': 'phase2-step4-smoke',
    'features': [
        {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [-106.45, 52.13]}, 'properties': {'source_id': '${SOURCE_ID}', 'name': 'fixture-A'}},
        {'type': 'Feature', 'geometry': {'type': 'Point', 'coordinates': [-105.66, 51.92]}, 'properties': {'source_id': '${SOURCE_ID}', 'name': 'fixture-B'}},
    ],
}).encode('utf-8')
async def main():
    sess = aioboto3.Session(
        aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
        region_name='us-east-1',
    )
    async with sess.client('s3', endpoint_url=os.environ.get('S3_ENDPOINT_URL', 'http://minio:8333')) as s3:
        await s3.put_object(Bucket='bronze', Key='${MINIO_KEY}', Body=GEOJSON, ContentType='application/geo+json')
asyncio.run(main())
" >/dev/null
echo "  synthetic GeoJSON uploaded to bronze:${MINIO_KEY}"

# 3. POST trigger.
echo
echo "--- POST integrations/public_geoscience_pull/trigger ---"
RESP=$(curl -fsS -X POST "$BASE/internal/v1/integrations/public_geoscience_pull/trigger" \
    -H 'Content-Type: application/json' \
    -H "X-Service-Key: $KEY" \
    -d "{
        \"minio_key\": \"${MINIO_KEY}\",
        \"source_id\": \"${SOURCE_ID}\",
        \"source_url\": \"https://example.test/phase2-fixture\",
        \"fetched_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"
    }")
echo "  $RESP"
RUN_ID=$(echo "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("workflow_run_id",""))')
if [ -z "$RUN_ID" ]; then
    echo "  [FAIL] no workflow_run_id"
    exit 1
fi

# 4. Wait for completion. Hatchet V1 uses the `v1_readable_status_olap`
#    enum with COMPLETED as the success terminal (not SUCCEEDED).
echo
echo "--- Waiting up to 60s for v1_runs_olap status COMPLETED ---"
status=""
for i in $(seq 1 12); do
    status=$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc \
        "SELECT readable_status::text FROM v1_runs_olap WHERE external_id='${RUN_ID}'::uuid LIMIT 1;" \
        2>/dev/null | tr -d ' ')
    case "$status" in
        COMPLETED)                  echo "  [PASS] workflow status=COMPLETED after ~$((i*5))s"; break ;;
        FAILED|CANCELLED|EVICTED)   echo "  [FAIL] workflow status=$status"; exit 1 ;;
    esac
    sleep 5
done
[ "$status" = "COMPLETED" ] || { echo "  [FAIL] never reached COMPLETED (last=$status)"; exit 1; }

# 5. bronze.provenance row.
echo
echo "--- Assertions ---"
n_prov=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM bronze.provenance
     WHERE source_file = 's3://bronze/${MINIO_KEY}'
       AND parser_name = 'activepieces_public_geoscience_pull';" | tr -d ' ')
echo "  bronze.provenance rows: ${n_prov}"

n_audit=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM audit.audit_ledger
     WHERE action_type = 'public_geoscience.pull.complete'
       AND payload->>'minio_key' = '${MINIO_KEY}';" | tr -d ' ')
echo "  audit 'public_geoscience.pull.complete' rows: ${n_audit}"

if [ "$n_prov" = "1" ] && [ "$n_audit" = "1" ]; then
    echo "  [PASS] bronze.provenance + audit landed"
else
    echo "  [FAIL] provenance=${n_prov} audit=${n_audit}"
    exit 1
fi

echo
echo "============================================================"
echo "PHASE 2 STEP 4 — SMOKE PASSED"
echo "============================================================"
