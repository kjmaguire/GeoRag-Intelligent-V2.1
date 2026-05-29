#!/usr/bin/env bash
# =============================================================================
# scripts/phase3_step4_smoke.sh
#
# Phase 3 Step 4 smoke — exercises the public_geoscience_pull bridge
# end-to-end using a per-flow JWT (Phase 3 auth). Bypasses the Kestra
# UI entirely; we mint a JWT, drop a synthetic GeoJSON in bronze, and
# POST directly to FastAPI's trigger endpoint.
#
# Identical shape to the Phase 2 smoke but auth is `Authorization:
# Bearer <jwt>` instead of `X-Service-Key: ...`.
# =============================================================================

set -uo pipefail

ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
BASE="${FASTAPI_INTERNAL_URL:-http://localhost:8000}"

SOURCE_ID="phase3-step4-smoke"
MINIO_KEY="public_geoscience/${SOURCE_ID}/$(date -u +%Y%m%dT%H%M%S)Z.geojson"

cleanup() {
    docker exec georag-postgresql psql -U georag -d georag -q -c "
        UPDATE workspace.feature_flags
           SET bool_value = false, updated_at = now()
         WHERE workspace_id IS NULL
           AND flag_name = 'flows.public_geoscience_pull.enabled';
        DELETE FROM bronze.provenance
         WHERE source_file = 's3://bronze/${MINIO_KEY}';
        DELETE FROM audit.audit_ledger
         WHERE action_type = 'public_geoscience.pull.complete'
           AND payload->>'minio_key' = '${MINIO_KEY}';
    " >/dev/null
    docker exec georag-hatchet-worker-ai python3 -c "
import asyncio, os, aioboto3
async def main():
    s = aioboto3.Session(aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'], region_name='us-east-1')
    async with s.client('s3', endpoint_url=os.environ.get('S3_ENDPOINT_URL','http://minio:8333')) as c:
        try: await c.delete_object(Bucket='bronze', Key='${MINIO_KEY}')
        except Exception: pass
asyncio.run(main())
" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cat <<BANNER

============================================================
PHASE 3 STEP 4 — public_geoscience_pull (Kestra) SMOKE
============================================================
Source ID : ${SOURCE_ID}
Minio key : ${MINIO_KEY}
Auth      : per-flow JWT (Phase 3)
============================================================
BANNER

# Mint a per-flow JWT.
JWT=$(docker exec georag-fastapi python3 -c "
import sys; sys.path.insert(0,'/app')
from app.services.flow_jwt import mint_flow_jwt
print(mint_flow_jwt('public_geoscience_pull', ttl_seconds=300), end='')
")
[ -z "$JWT" ] && { echo "  [FAIL] could not mint JWT"; exit 1; }
echo "  JWT minted (${#JWT} chars)"

# Enable flag.
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO workspace.feature_flags
        (workspace_id, flag_name, bool_value, updated_at)
    VALUES (NULL, 'flows.public_geoscience_pull.enabled', true, now())
    ON CONFLICT (workspace_id, flag_name) DO UPDATE
        SET bool_value = EXCLUDED.bool_value, updated_at = now();
" >/dev/null
echo "  flag enabled"

# Drop synthetic GeoJSON.
docker exec georag-hatchet-worker-ai python3 -c "
import asyncio, os, json, aioboto3
async def main():
    s = aioboto3.Session(aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'], region_name='us-east-1')
    body = json.dumps({'type':'FeatureCollection','features':[{'type':'Feature','geometry':{'type':'Point','coordinates':[-106.0,52.0]},'properties':{'source_id':'${SOURCE_ID}'}}]}).encode()
    async with s.client('s3', endpoint_url=os.environ.get('S3_ENDPOINT_URL','http://minio:8333')) as c:
        await c.put_object(Bucket='bronze', Key='${MINIO_KEY}', Body=body, ContentType='application/geo+json')
asyncio.run(main())
" >/dev/null
echo "  GeoJSON uploaded"

# POST trigger with JWT.
echo
echo "--- POST integrations/public_geoscience_pull/trigger (JWT) ---"
RESP=$(curl -fsS -X POST "$BASE/internal/v1/integrations/public_geoscience_pull/trigger" \
    -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $JWT" \
    -d "{
        \"minio_key\": \"${MINIO_KEY}\",
        \"source_id\": \"${SOURCE_ID}\",
        \"source_url\": \"https://example.test/phase3-fixture\",
        \"fetched_at\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\"
    }")
echo "  $RESP"
RUN_ID=$(echo "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("workflow_run_id",""))')
[ -z "$RUN_ID" ] && { echo "  [FAIL] no workflow_run_id"; exit 1; }

# Wait for COMPLETED.
echo
echo "--- Waiting up to 60s for v1_runs_olap COMPLETED ---"
status=""
for i in $(seq 1 12); do
    status=$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc \
        "SELECT readable_status::text FROM v1_runs_olap WHERE external_id='${RUN_ID}'::uuid LIMIT 1;" \
        2>/dev/null | tr -d ' ')
    case "$status" in
        COMPLETED) echo "  [PASS] COMPLETED after ~$((i*5))s"; break ;;
        FAILED|CANCELLED|EVICTED) echo "  [FAIL] status=$status"; exit 1 ;;
    esac
    sleep 5
done
[ "$status" = "COMPLETED" ] || { echo "  [FAIL] last=$status"; exit 1; }

# Assert provenance + audit.
echo
n_prov=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM bronze.provenance
     WHERE source_file = 's3://bronze/${MINIO_KEY}'
       AND parser_name = 'activepieces_public_geoscience_pull';" | tr -d ' ')
n_audit=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM audit.audit_ledger
     WHERE action_type = 'public_geoscience.pull.complete'
       AND payload->>'minio_key' = '${MINIO_KEY}';" | tr -d ' ')
echo "  bronze.provenance rows : $n_prov"
echo "  audit_ledger rows      : $n_audit"
if [ "$n_prov" = "1" ] && [ "$n_audit" = "1" ]; then
    echo "  [PASS] bronze.provenance + audit landed via JWT auth"
else
    echo "  [FAIL] provenance=$n_prov audit=$n_audit"
    exit 1
fi

echo
echo "============================================================"
echo "PHASE 3 STEP 4 — SMOKE PASSED"
echo "============================================================"
