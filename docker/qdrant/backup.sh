#!/usr/bin/env bash
# =============================================================================
# GeoRAG Qdrant Backup Script
# =============================================================================
#
# Runs inside georag-backup-agent (Alpine sidecar). Calls the Qdrant HTTP
# API over the network using curl and jq — no dependencies on the Qdrant
# container itself (which is distroless and has neither curl nor bash).
#
# For each collection in Qdrant:
#   1. POST /collections/{name}/snapshots  — trigger snapshot creation
#   2. GET  /collections/{name}/snapshots  — find the new snapshot name
#   3. GET  /collections/{name}/snapshots/{snapshot}  — download to staging
#   4. Upload to s3://georag-backups/qdrant/{collection}/{snapshot}
#   5. Retention: delete snapshots older than 7 days from both Qdrant and S3
#
# Environment variables (injected by compose env block):
#   QDRANT_URL             — full base URL (e.g. http://qdrant:6333)
#   QDRANT_API_KEY         — API key (optional; leave empty for no-auth dev)
#   AWS_ACCESS_KEY_ID      — SeaweedFS / S3 access key
#   AWS_SECRET_ACCESS_KEY  — SeaweedFS / S3 secret key
#   S3_ENDPOINT_URL        — SeaweedFS S3 endpoint (e.g. http://minio:8333)
#
# Optional:
#   BACKUP_RETENTION_DAYS  — days to retain (default: 7)
#   DRY_RUN=1              — log without executing
#
# Usage (from Ofelia job-exec or manual drill):
#   docker exec georag-backup-agent /backup-scripts/qdrant/backup.sh
#   docker exec georag-backup-agent bash -c 'DRY_RUN=1 /backup-scripts/qdrant/backup.sh'
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
QDRANT_URL="${QDRANT_URL:-http://qdrant:6333}"
QDRANT_API_KEY="${QDRANT_API_KEY:-}"
AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-}"
AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-}"
S3_ENDPOINT_URL="${S3_ENDPOINT_URL:-}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
DRY_RUN="${DRY_RUN:-0}"

STAGING_BASE="/backup/staging/qdrant"
S3_BUCKET="georag-backups"
S3_PREFIX="qdrant"

# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------
log() {
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*" >&2
}

log "=== Qdrant backup starting ==="
log "  Qdrant:    ${QDRANT_URL}"
log "  Staging:   ${STAGING_BASE}"
log "  S3 prefix: s3://${S3_BUCKET}/${S3_PREFIX}/"
log "  Retention: ${RETENTION_DAYS} days"
log "  Dry-run:   ${DRY_RUN}"

# ---------------------------------------------------------------------------
# Validate required env
# ---------------------------------------------------------------------------
if [[ -z "${AWS_ACCESS_KEY_ID}" ]]; then
    log "ERROR: AWS_ACCESS_KEY_ID is not set" ; exit 1
fi
if [[ -z "${AWS_SECRET_ACCESS_KEY}" ]]; then
    log "ERROR: AWS_SECRET_ACCESS_KEY is not set" ; exit 1
fi
if [[ -z "${S3_ENDPOINT_URL}" ]]; then
    log "ERROR: S3_ENDPOINT_URL is not set" ; exit 1
fi

export AWS_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY

# ---------------------------------------------------------------------------
# curl helper that injects API key header if set
# ---------------------------------------------------------------------------
qdrant_curl() {
    local args=("$@")
    if [[ -n "${QDRANT_API_KEY}" ]]; then
        curl -s -H "api-key: ${QDRANT_API_KEY}" "${args[@]}"
    else
        curl -s "${args[@]}"
    fi
}

# ---------------------------------------------------------------------------
# Dry-run path — must come before any curl calls
# (The sidecar image ships curl, but DRY_RUN should not reach the network.)
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" == "1" ]]; then
    log "DRY_RUN=1 — skipping actual backup execution"
    log "Would run: GET ${QDRANT_URL}/collections  (list all collections)"
    log "For each collection:"
    log "  POST ${QDRANT_URL}/collections/<name>/snapshots"
    log "  GET  ${QDRANT_URL}/collections/<name>/snapshots/<snapshot_name>  (download)"
    log "  aws s3 cp <staging>/<name>/<snapshot> s3://${S3_BUCKET}/${S3_PREFIX}/<name>/<snapshot>"
    log "  Retention sweep: delete objects older than ${RETENTION_DAYS}d from Qdrant + S3"
    log "DRY_RUN complete — no files written, no S3 calls made"
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 0 — List collections
# ---------------------------------------------------------------------------
log "Step 0: Listing Qdrant collections..."

COLLECTIONS_RESP=$(qdrant_curl -f "${QDRANT_URL}/collections")
COLLECTIONS=$(echo "${COLLECTIONS_RESP}" | jq -r '.result.collections[].name' 2>/dev/null || true)

if [[ -z "${COLLECTIONS}" ]]; then
    log "No collections found — nothing to back up"
    exit 0
fi

log "Collections: $(echo "${COLLECTIONS}" | tr '\n' ' ')"

# ---------------------------------------------------------------------------
# Process each collection
# ---------------------------------------------------------------------------
OVERALL_FAIL=0

while IFS= read -r COLLECTION; do
    log "--- Processing collection: ${COLLECTION} ---"

    # Step 1 — Create snapshot
    log "  Triggering snapshot for ${COLLECTION}..."
    SNAP_RESP=$(qdrant_curl -f -X POST \
        "${QDRANT_URL}/collections/${COLLECTION}/snapshots")

    SNAP_NAME=$(echo "${SNAP_RESP}" | jq -r '.result.name' 2>/dev/null || true)
    if [[ -z "${SNAP_NAME}" || "${SNAP_NAME}" == "null" ]]; then
        log "  ERROR: Failed to create snapshot for ${COLLECTION}"
        OVERALL_FAIL=1
        continue
    fi
    log "  Snapshot created: ${SNAP_NAME}"

    # Step 2 — Ensure staging directory for this collection
    COL_STAGING="${STAGING_BASE}/${COLLECTION}"
    mkdir -p "${COL_STAGING}"

    # Step 3 — Download snapshot
    SNAP_FILE="${COL_STAGING}/${SNAP_NAME}"
    log "  Downloading snapshot to ${SNAP_FILE}..."
    qdrant_curl -f -o "${SNAP_FILE}" \
        "${QDRANT_URL}/collections/${COLLECTION}/snapshots/${SNAP_NAME}"

    SNAP_SIZE=$(stat -c%s "${SNAP_FILE}" 2>/dev/null || echo "0")
    log "  Downloaded: ${SNAP_SIZE} bytes"

    # Step 4 — Upload to S3
    S3_DEST="s3://${S3_BUCKET}/${S3_PREFIX}/${COLLECTION}/${SNAP_NAME}"
    log "  Uploading to ${S3_DEST}..."
    aws s3 cp \
        "${SNAP_FILE}" \
        "${S3_DEST}" \
        --endpoint-url "${S3_ENDPOINT_URL}" \
        --no-progress
    log "  Upload complete"

    # Remove local staging copy
    rm -f "${SNAP_FILE}"

    # Step 5a — Retention: S3 side
    log "  Applying ${RETENTION_DAYS}-day retention on S3 for ${COLLECTION}..."
    # Compute cutoff via shell arithmetic — avoids BSD `date -v` and GNU
    # `date -d "N days ago"` which busybox (Alpine ash) does not support.
    # The sidecar runs bash so date -u -d is available via GNU coreutils.
    CUTOFF_EPOCH=$(( $(date -u +%s) - RETENTION_DAYS * 86400 ))

    aws s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}/${COLLECTION}/" \
        --endpoint-url "${S3_ENDPOINT_URL}" \
        | while read -r _date _time _size obj_name; do
            obj_date_str="${_date} ${_time}"
            obj_epoch=$(date -u -d "${obj_date_str}" +%s 2>/dev/null || echo "0")
            if [[ "${obj_epoch}" -gt 0 && "${obj_epoch}" -lt "${CUTOFF_EPOCH}" ]]; then
                log "  Deleting expired S3 object: ${obj_name} (${_date})"
                aws s3 rm "s3://${S3_BUCKET}/${S3_PREFIX}/${COLLECTION}/${obj_name}" \
                    --endpoint-url "${S3_ENDPOINT_URL}"
            fi
        done

    # Step 5b — Retention: Qdrant snapshot list side
    log "  Applying ${RETENTION_DAYS}-day retention on Qdrant snapshots for ${COLLECTION}..."
    SNAPS_RESP=$(qdrant_curl -f "${QDRANT_URL}/collections/${COLLECTION}/snapshots")

    echo "${SNAPS_RESP}" | jq -r '.result[] | [.name, .creation_time] | @tsv' 2>/dev/null \
        | while IFS=$'\t' read -r snap_name snap_time; do
            if [[ -z "${snap_name}" || "${snap_name}" == "null" ]]; then continue; fi
            snap_epoch=$(date -u -d "${snap_time}" +%s 2>/dev/null || echo "0")
            if [[ "${snap_epoch}" -gt 0 && "${snap_epoch}" -lt "${CUTOFF_EPOCH}" ]]; then
                log "  Deleting expired Qdrant snapshot: ${snap_name}"
                qdrant_curl -f -X DELETE \
                    "${QDRANT_URL}/collections/${COLLECTION}/snapshots/${snap_name}" \
                    > /dev/null || log "  WARNING: Failed to delete Qdrant snapshot ${snap_name}"
            fi
        done

    log "--- Collection ${COLLECTION} done ---"
done <<< "${COLLECTIONS}"

if [[ "${OVERALL_FAIL}" -ne 0 ]]; then
    log "ERROR: One or more collections failed to back up"
    exit 1
fi

log "=== Qdrant backup finished for all collections ==="
