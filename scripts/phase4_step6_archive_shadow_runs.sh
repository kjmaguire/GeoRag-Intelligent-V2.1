#!/usr/bin/env bash
# =============================================================================
# scripts/phase4_step6_archive_shadow_runs.sh
#
# Phase 4 Step 6 — archive silver.shadow_runs to S3 before dropping the
# table (R-P1-10 cleanup). Standard pg_dump → SeaweedFS one-shot, with
# 90-day archive retention. Idempotent: re-running uploads a fresh
# snapshot under a timestamped key.
# =============================================================================

set -euo pipefail

ARCHIVE_PREFIX="archive/phase1/shadow_runs"
ARCHIVE_BUCKET="${ARCHIVE_BUCKET:-bronze}"
TS=$(date -u +%Y%m%dT%H%M%SZ)
DUMP_NAME="silver.shadow_runs.${TS}.dump"

cat <<BANNER
============================================================
PHASE 4 STEP 6 — silver.shadow_runs ARCHIVE TO S3
============================================================
target : s3://${ARCHIVE_BUCKET}/${ARCHIVE_PREFIX}/${DUMP_NAME}
============================================================
BANNER

# 1. pg_dump the table inside Postgres → host scratch.
docker exec georag-postgresql pg_dump \
    -U georag -d georag \
    -t silver.shadow_runs \
    --format=custom --no-owner --no-acl \
    -f "/tmp/${DUMP_NAME}"
SIZE=$(docker exec georag-postgresql stat -c %s "/tmp/${DUMP_NAME}")
echo "  pg_dump complete (${SIZE} bytes)"

# 2. Copy host-side then upload via AI worker (has aioboto3 + S3 creds).
docker cp "georag-postgresql:/tmp/${DUMP_NAME}" "/tmp/${DUMP_NAME}"
docker cp "/tmp/${DUMP_NAME}" "georag-hatchet-worker-ai:/tmp/${DUMP_NAME}"

docker exec \
    -e ARCHIVE_BUCKET="$ARCHIVE_BUCKET" \
    -e ARCHIVE_KEY="${ARCHIVE_PREFIX}/${DUMP_NAME}" \
    -e DUMP_PATH="/tmp/${DUMP_NAME}" \
    georag-hatchet-worker-ai python3 -c "
import asyncio, os, aioboto3
async def main():
    with open(os.environ['DUMP_PATH'], 'rb') as f:
        body = f.read()
    sess = aioboto3.Session(
        aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
        region_name='us-east-1',
    )
    async with sess.client('s3', endpoint_url=os.environ.get('S3_ENDPOINT_URL', 'http://minio:8333')) as s3:
        await s3.put_object(
            Bucket=os.environ['ARCHIVE_BUCKET'],
            Key=os.environ['ARCHIVE_KEY'],
            Body=body,
            ContentType='application/x-postgres-dump',
            Metadata={'phase': 'phase4-step6', 'retention-days': '90'},
        )
        print(f'uploaded {len(body)} bytes')
asyncio.run(main())
"

docker exec georag-postgresql rm -f "/tmp/${DUMP_NAME}"
docker exec georag-hatchet-worker-ai rm -f "/tmp/${DUMP_NAME}"
rm -f "/tmp/${DUMP_NAME}"

echo
echo "  ARCHIVE OK — s3://${ARCHIVE_BUCKET}/${ARCHIVE_PREFIX}/${DUMP_NAME}"
echo "  Retention 90 days (operator-managed; no auto-delete)"
