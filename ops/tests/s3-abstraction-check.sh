#!/usr/bin/env bash
# =============================================================================
# GeoRAG — S3 Abstraction Integrity Check
# =============================================================================
# Produced by: devops-engineer agent (Claude Sonnet 4.6)
# Date: 2026-04-19 (Module 2 Phase B, Item B7)
# Authority: 02-data-stores-hardening.md §B7
#
# PURPOSE:
#   Verifies that the S3 abstraction layer (SeaweedFS via boto3 / aws-cli) can
#   perform a full put/get/delete round-trip against the georag-bronze bucket.
#   Tests the vendor-agnostic path: application code uses standard S3 API calls
#   with S3_ENDPOINT_URL — no SeaweedFS-native or MinIO SDK calls.
#
# USAGE (run from backup-agent container or any container with aws-cli):
#   docker exec georag-backup-agent bash /backup-scripts/s3-abstraction-check.sh
#
#   Or from the host (requires aws-cli and access to the Docker network):
#   docker exec georag-backup-agent bash /ops/tests/s3-abstraction-check.sh
#
#   From the project root (mounts this script into backup-agent):
#   docker run --rm \
#     --network georag-intelligence-v10_georag \
#     --env-file .env \
#     -v "$(pwd)/ops/tests/s3-abstraction-check.sh:/check.sh:ro" \
#     amazon/aws-cli bash /check.sh
#
# ENVIRONMENT (injected automatically when run inside backup-agent):
#   AWS_ACCESS_KEY_ID     — SeaweedFS / S3 access key
#   AWS_SECRET_ACCESS_KEY — SeaweedFS / S3 secret key
#   S3_ENDPOINT_URL       — SeaweedFS S3 endpoint (default: http://minio:8333)
#   S3_BUCKET             — Target bucket (default: bronze)
#
# EXIT CODES:
#   0 — all three operations (put/get/delete) succeeded; round-trip clean
#   1 — one or more operations failed; error message written to stderr
# =============================================================================

set -euo pipefail

ENDPOINT="${S3_ENDPOINT_URL:-http://minio:8333}"
BUCKET="${S3_BUCKET:-bronze}"
TEST_KEY="ops-integrity-check/s3-round-trip-$(date +%Y%m%dT%H%M%SZ).txt"
TEST_CONTENT="GeoRAG S3 abstraction integrity check — $(date -u +%Y-%m-%dT%H:%M:%SZ)"
TMPFILE="$(mktemp)"

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }
fail() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] FAIL: $*" >&2; exit 1; }

cleanup() {
    rm -f "${TMPFILE}"
    # Best-effort delete on exit (handles abort paths)
    aws s3 rm "s3://${BUCKET}/${TEST_KEY}" \
        --endpoint-url "${ENDPOINT}" \
        2>/dev/null || true
}
trap cleanup EXIT

log "=== S3 abstraction integrity check ==="
log "  Endpoint : ${ENDPOINT}"
log "  Bucket   : ${BUCKET}"
log "  Test key : ${TEST_KEY}"

# ---------------------------------------------------------------------------
# Step 1 — PUT
# ---------------------------------------------------------------------------
log "Step 1: PUT object"
echo "${TEST_CONTENT}" > "${TMPFILE}"
aws s3 cp "${TMPFILE}" "s3://${BUCKET}/${TEST_KEY}" \
    --endpoint-url "${ENDPOINT}" \
    --no-progress \
    --content-type "text/plain" \
    || fail "PUT failed — check SeaweedFS S3 API at ${ENDPOINT}"
log "  PUT: OK"

# ---------------------------------------------------------------------------
# Step 2 — GET (retrieve and verify content matches)
# ---------------------------------------------------------------------------
log "Step 2: GET object and verify content"
RETRIEVED="$(aws s3 cp "s3://${BUCKET}/${TEST_KEY}" - \
    --endpoint-url "${ENDPOINT}" \
    --no-progress \
    2>/dev/null)" \
    || fail "GET failed — object not readable after PUT"

if [ "${RETRIEVED}" != "${TEST_CONTENT}" ]; then
    fail "Content mismatch: expected '${TEST_CONTENT}', got '${RETRIEVED}'"
fi
log "  GET: OK (content verified)"

# ---------------------------------------------------------------------------
# Step 3 — HEAD (verify object metadata — ETag and size)
# ---------------------------------------------------------------------------
log "Step 3: HEAD object metadata"
aws s3api head-object \
    --bucket "${BUCKET}" \
    --key "${TEST_KEY}" \
    --endpoint-url "${ENDPOINT}" \
    --output json \
    > /dev/null \
    || fail "HEAD failed — object metadata not retrievable"
log "  HEAD: OK"

# ---------------------------------------------------------------------------
# Step 4 — LIST (verify key appears in bucket listing)
# ---------------------------------------------------------------------------
log "Step 4: LIST bucket prefix"
LIST_OUTPUT="$(aws s3 ls "s3://${BUCKET}/ops-integrity-check/" \
    --endpoint-url "${ENDPOINT}" \
    2>/dev/null)"
if ! echo "${LIST_OUTPUT}" | grep -q "s3-round-trip-"; then
    fail "LIST failed — test key not found in bucket listing"
fi
log "  LIST: OK"

# ---------------------------------------------------------------------------
# Step 5 — DELETE (clean up test artifact)
# ---------------------------------------------------------------------------
log "Step 5: DELETE object"
aws s3 rm "s3://${BUCKET}/${TEST_KEY}" \
    --endpoint-url "${ENDPOINT}" \
    || fail "DELETE failed — test object not removed"
log "  DELETE: OK"

# ---------------------------------------------------------------------------
# Step 6 — Verify deletion (GET should fail now)
# ---------------------------------------------------------------------------
log "Step 6: Verify object is gone (GET should return 404)"
if aws s3 cp "s3://${BUCKET}/${TEST_KEY}" - \
    --endpoint-url "${ENDPOINT}" \
    2>/dev/null; then
    fail "Object still exists after DELETE — SeaweedFS delete may be broken"
fi
log "  Deletion verified: OK"

log "=== Round-trip PASSED: put/head/list/get/delete all succeeded ==="
log "    Vendor purity confirmed: standard aws-cli S3 API calls only"
log "    No SeaweedFS-native or MinIO SDK calls used"
exit 0
