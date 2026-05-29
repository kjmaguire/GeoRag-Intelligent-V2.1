#!/usr/bin/env bash
# =============================================================================
# scripts/phase2_step4_verify.sh
#
# Phase 2 Step 4 done-definition — first scheduled-import flow.
#
#   1. public_geoscience_pull workflow file imports cleanly + IO models
#   2. Hatchet engine knows about it (registered)
#   3. AI worker pool advertises it via --list
#   4. integrations_trigger registry contains public_geoscience_pull
#   5. Feature flag activepieces.public_geoscience_pull.enabled exists
#   6. Flag-disabled path returns skipped=true (workflow gate works)
#   7. End-to-end smoke (delegates to phase2_step4_smoke.sh)
# =============================================================================

set -uo pipefail

PASS=0
TOTAL=7
ENVFILE="${ENV_FILE:-/home/georag/projects/georag/.env}"
KEY=$(awk -F= '/^FASTAPI_SERVICE_KEY=/ { print $2 }' "$ENVFILE" 2>/dev/null | head -1)
BASE="${FASTAPI_INTERNAL_URL:-http://localhost:8000}"

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
PHASE 2 STEP 4 — public_geoscience_pull VERIFICATION
============================================================
BANNER

# 1) Workflow imports cleanly inside the AI worker
import_check=$(docker exec georag-hatchet-worker-ai python3 -c "
import sys; sys.path.insert(0, '/app')
from app.hatchet_workflows.public_geoscience_pull import (
    public_geoscience_pull, PublicGeoSciencePullInput, PublicGeoSciencePullOut,
)
print('OK' if public_geoscience_pull and PublicGeoSciencePullInput and PublicGeoSciencePullOut else 'MISSING')
" 2>&1 | tail -1)
[ "$import_check" = "OK" ] && check "Workflow + IO models import cleanly" ok \
    || check "import" fail "$import_check"

# 2) Hatchet engine knows about it
engine_check=$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc \
    "SELECT name FROM \"Workflow\" WHERE name='public_geoscience_pull' AND \"deletedAt\" IS NULL LIMIT 1;" \
    2>/dev/null | tr -d ' ')
[ "$engine_check" = "public_geoscience_pull" ] \
    && check "Hatchet engine knows public_geoscience_pull" ok \
    || check "engine registration" fail "got '$engine_check'"

# 3) AI pool advertises it
pool_check=$(docker exec georag-hatchet-worker-ai python3 -m app.hatchet_workflows.worker --list 2>&1 \
    | grep -c '^public_geoscience_pull$')
[ "$pool_check" = "1" ] \
    && check "AI worker pool advertises public_geoscience_pull" ok \
    || check "pool advertisement" fail "got $pool_check"

# 4) integrations_trigger registry contains public_geoscience_pull
flows=$(curl -fsS "$BASE/internal/v1/integrations/flows" -H "X-Service-Key: $KEY" 2>/dev/null)
case "$flows" in
    *public_geoscience_pull*) check "integrations registry has public_geoscience_pull" ok ;;
    *)                        check "registry entry" fail "got: $flows" ;;
esac

# 5) Feature flag seeded
flag_count=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM workspace.feature_flags
     WHERE workspace_id IS NULL
       AND flag_name = 'activepieces.public_geoscience_pull.enabled';" \
    2>/dev/null | tr -d ' ')
[ "$flag_count" = "1" ] \
    && check "feature flag activepieces.public_geoscience_pull.enabled seeded" ok \
    || check "feature flag" fail "got count=$flag_count"

# 6) Disabled-flag path returns skipped=true. Force flag false, drop a
#    fixture, trigger, expect SUCCEEDED (workflow short-circuits) but no
#    provenance row.
TEMP_KEY="public_geoscience/phase2-verify-skip/$(date -u +%s).geojson"
docker exec georag-postgresql psql -U georag -d georag -q -c "
    INSERT INTO workspace.feature_flags
        (workspace_id, flag_name, bool_value, updated_at)
    VALUES (NULL, 'activepieces.public_geoscience_pull.enabled', false, now())
    ON CONFLICT (workspace_id, flag_name) DO UPDATE SET bool_value=false, updated_at=now();" >/dev/null
docker exec georag-hatchet-worker-ai python3 -c "
import asyncio, os, json, aioboto3
async def main():
    s = aioboto3.Session(aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'], region_name='us-east-1')
    body = json.dumps({'type':'FeatureCollection','features':[]}).encode()
    async with s.client('s3', endpoint_url=os.environ.get('S3_ENDPOINT_URL','http://minio:8333')) as c:
        await c.put_object(Bucket='bronze', Key='${TEMP_KEY}', Body=body)
asyncio.run(main())" >/dev/null 2>&1
SKIP_RESP=$(curl -fsS -X POST "$BASE/internal/v1/integrations/public_geoscience_pull/trigger" \
    -H 'Content-Type: application/json' -H "X-Service-Key: $KEY" \
    -d "{\"minio_key\":\"${TEMP_KEY}\",\"source_id\":\"verify-skip\"}")
SKIP_RUN_ID=$(echo "$SKIP_RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("workflow_run_id",""))')
sleep 8
prov_skip=$(docker exec georag-postgresql psql -U georag -d georag -tAc "
    SELECT count(*) FROM bronze.provenance WHERE source_file = 's3://bronze/${TEMP_KEY}';" \
    2>/dev/null | tr -d ' ')
status_skip=$(docker exec georag-postgresql psql -U hatchet -d hatchet -tAc \
    "SELECT readable_status::text FROM v1_runs_olap WHERE external_id='${SKIP_RUN_ID}'::uuid;" \
    2>/dev/null | tr -d ' ')
if [ "$status_skip" = "COMPLETED" ] && [ "$prov_skip" = "0" ]; then
    check "Flag-disabled path: workflow COMPLETED with no provenance row written" ok
else
    check "flag gate" fail "status=$status_skip, provenance=$prov_skip"
fi
# cleanup verify-skip artefacts
docker exec georag-hatchet-worker-ai python3 -c "
import asyncio, os, aioboto3
async def main():
    s = aioboto3.Session(aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'], region_name='us-east-1')
    async with s.client('s3', endpoint_url=os.environ.get('S3_ENDPOINT_URL','http://minio:8333')) as c:
        try: await c.delete_object(Bucket='bronze', Key='${TEMP_KEY}')
        except Exception: pass
asyncio.run(main())" >/dev/null 2>&1 || true

# 7) End-to-end smoke (delegated)
echo
echo "  ── Running phase2_step4_smoke.sh ──"
if timeout 240 bash "$(dirname "$0")/phase2_step4_smoke.sh" > /tmp/step4_smoke.log 2>&1; then
    check "End-to-end public_geoscience_pull smoke" ok
else
    check "End-to-end smoke" fail "see /tmp/step4_smoke.log"
    tail -15 /tmp/step4_smoke.log
fi

echo
echo "============================================================"
echo "Result: $PASS / $TOTAL checks passed"
echo "============================================================"

exit $((PASS == TOTAL ? 0 : 1))
