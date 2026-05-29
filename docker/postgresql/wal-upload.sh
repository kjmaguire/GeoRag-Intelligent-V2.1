#!/usr/bin/env bash
# =============================================================================
# GeoRAG PostgreSQL WAL Segment Upload Script
# =============================================================================
#
# Runs inside georag-backup-agent (Alpine sidecar). Invoked every 5 minutes
# by Ofelia (job-exec on georag-backup-agent) to upload WAL segments from the
# shared pg_wal_archive volume to SeaweedFS S3.
#
# Design:
#   1. aws s3 sync /pg_wal_archive/ → s3://georag-backups/pg-wal/ (new/changed only)
#   2. After successful sync: delete local WAL files already confirmed in S3
#      (prevents the shared volume from growing unbounded; PG continuously
#      writes new segments at ~16 MB each)
#   3. Retention: delete S3 objects older than WAL_RETENTION_DAYS (default: 8)
#      — one day past the 7-day basebackup retention, so every basebackup
#      has WAL coverage for its full recovery window.
#
# Environment variables:
#   AWS_ACCESS_KEY_ID       — SeaweedFS S3 access key (required)
#   AWS_SECRET_ACCESS_KEY   — SeaweedFS S3 secret key (required)
#   S3_ENDPOINT_URL         — SeaweedFS S3 endpoint (required)
#   WAL_RETENTION_DAYS      — S3 WAL retention in days (default: 8)
#   WAL_ARCHIVE_DIR         — local WAL archive mount (default: /pg_wal_archive)
#   DRY_RUN                 — set to 1 to log intent without executing
#
# Usage:
#   docker exec georag-backup-agent /backup-scripts/postgresql/wal-upload.sh
#   docker exec georag-backup-agent bash -c 'DRY_RUN=1 /backup-scripts/postgresql/wal-upload.sh'
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-}"
AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-}"
S3_ENDPOINT_URL="${S3_ENDPOINT_URL:-}"
WAL_RETENTION_DAYS="${WAL_RETENTION_DAYS:-8}"
WAL_ARCHIVE_DIR="${WAL_ARCHIVE_DIR:-/pg_wal_archive}"
DRY_RUN="${DRY_RUN:-0}"

S3_BUCKET="georag-backups"
S3_PREFIX="pg-wal"
S3_DEST="s3://${S3_BUCKET}/${S3_PREFIX}/"

# ---------------------------------------------------------------------------
# Logging helper — ISO 8601 timestamps to stderr
# ---------------------------------------------------------------------------
log() {
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*" >&2
}

log "=== WAL upload starting ==="
log "  Local dir:  ${WAL_ARCHIVE_DIR}"
log "  S3 dest:    ${S3_DEST}"
log "  Retention:  ${WAL_RETENTION_DAYS} days"
log "  Dry-run:    ${DRY_RUN}"

# ---------------------------------------------------------------------------
# Validate required env
# ---------------------------------------------------------------------------
if [[ -z "${AWS_ACCESS_KEY_ID}" ]]; then
    log "ERROR: AWS_ACCESS_KEY_ID is not set"; exit 1
fi
if [[ -z "${AWS_SECRET_ACCESS_KEY}" ]]; then
    log "ERROR: AWS_SECRET_ACCESS_KEY is not set"; exit 1
fi
if [[ -z "${S3_ENDPOINT_URL}" ]]; then
    log "ERROR: S3_ENDPOINT_URL is not set"; exit 1
fi

if [[ ! -d "${WAL_ARCHIVE_DIR}" ]]; then
    log "ERROR: WAL archive directory ${WAL_ARCHIVE_DIR} does not exist"; exit 1
fi

# ---------------------------------------------------------------------------
# Dry-run path — log intent without executing
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" == "1" ]]; then
    log "DRY_RUN=1 — skipping actual execution"
    log "Would run: aws s3 sync ${WAL_ARCHIVE_DIR}/ ${S3_DEST} --endpoint-url ${S3_ENDPOINT_URL} --size-only"
    log "Would run: for each file in ${WAL_ARCHIVE_DIR}/: check if present in S3 with matching size, delete local copy"
    log "Would run: delete S3 objects in ${S3_DEST} older than ${WAL_RETENTION_DAYS} days"

    # List local files so the operator can see what would be uploaded
    LOCAL_COUNT=$(find "${WAL_ARCHIVE_DIR}" -maxdepth 1 -type f | wc -l)
    log "Local WAL files in ${WAL_ARCHIVE_DIR}: ${LOCAL_COUNT}"
    if [[ "${LOCAL_COUNT}" -gt 0 ]]; then
        # Use ls -lh — BusyBox find does not support -printf
        ls -lh "${WAL_ARCHIVE_DIR}" >&2 2>/dev/null || true
    fi
    log "DRY_RUN complete — no files written, no S3 calls made"
    exit 0
fi

# ---------------------------------------------------------------------------
# Export credentials for AWS CLI
# ---------------------------------------------------------------------------
export AWS_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY

# ---------------------------------------------------------------------------
# Step 1 — Sync local WAL archive → S3 (upload new/changed only)
# --size-only: skip files where local and S3 sizes match (WAL segments are
# immutable once written; size equality implies content equality).
# ---------------------------------------------------------------------------
log "Step 1: Syncing ${WAL_ARCHIVE_DIR}/ → ${S3_DEST}"
aws s3 sync \
    "${WAL_ARCHIVE_DIR}/" \
    "${S3_DEST}" \
    --endpoint-url "${S3_ENDPOINT_URL}" \
    --size-only \
    --no-progress

log "Step 1 complete: sync finished"

# ---------------------------------------------------------------------------
# Step 2 — Clean up local WAL files that are confirmed in S3
# List S3 objects, compare against local files by name and size.
# Delete local files where S3 has a matching entry (same name, same size).
# This prevents the pg_wal_archive volume from growing unbounded.
#
# Safety: only delete files whose name+size match an S3 object — never
# delete files that may be in-progress or larger than the S3 copy.
# ---------------------------------------------------------------------------
log "Step 2: Removing local WAL files already confirmed in S3..."

DELETED_COUNT=0

# Build a temporary lookup of S3 objects: "filename size" per line
S3_LISTING=$(aws s3 ls "${S3_DEST}" \
    --endpoint-url "${S3_ENDPOINT_URL}" \
    2>/dev/null || true)

if [[ -z "${S3_LISTING}" ]]; then
    log "Step 2: No S3 objects found — skipping local cleanup"
else
    while IFS= read -r local_file; do
        filename=$(basename "${local_file}")
        local_size=$(stat -c%s "${local_file}" 2>/dev/null || echo "0")

        # Check if S3 listing has this filename with matching size
        # aws s3 ls format: "2026-04-19 12:34:56   16777216 000000010000000100000001"
        if echo "${S3_LISTING}" | awk '{print $3, $4}' | grep -q "^${local_size} ${filename}$"; then
            log "Deleting local WAL (confirmed in S3): ${filename} (${local_size} bytes)"
            rm -f "${local_file}"
            DELETED_COUNT=$(( DELETED_COUNT + 1 ))
        fi
    done < <(find "${WAL_ARCHIVE_DIR}" -maxdepth 1 -type f)
fi

log "Step 2 complete: ${DELETED_COUNT} local WAL file(s) removed"

# ---------------------------------------------------------------------------
# Step 3 — S3 retention: delete WAL objects older than WAL_RETENTION_DAYS
# Uses pure shell epoch arithmetic — compatible with Alpine/BusyBox bash.
# Cutoff is WAL_RETENTION_DAYS ago; WAL outside the retention window is
# no longer needed for PITR (the oldest basebackup is 7 days old; WAL
# coverage of 8 days guarantees every basebackup has its WAL).
# ---------------------------------------------------------------------------
log "Step 3: Applying ${WAL_RETENTION_DAYS}-day retention on ${S3_DEST}"

CUTOFF_EPOCH=$(( $(date -u +%s) - WAL_RETENTION_DAYS * 86400 ))

EXPIRED_COUNT=0
aws s3 ls "${S3_DEST}" \
    --endpoint-url "${S3_ENDPOINT_URL}" \
    2>/dev/null \
    | while read -r _date _time _size obj_name; do
        obj_date_str="${_date} ${_time}"
        # On Alpine bash (GNU bash), 'date -u -d' parses "YYYY-MM-DD HH:MM:SS" format.
        obj_epoch=$(date -u -d "${obj_date_str}" +%s 2>/dev/null || echo "0")
        if [[ "${obj_epoch}" -gt 0 && "${obj_epoch}" -lt "${CUTOFF_EPOCH}" ]]; then
            log "Deleting expired S3 WAL object: ${obj_name} (${_date})"
            aws s3 rm "${S3_DEST}${obj_name}" \
                --endpoint-url "${S3_ENDPOINT_URL}"
            EXPIRED_COUNT=$(( EXPIRED_COUNT + 1 ))
        fi
    done

log "Step 3 complete: retention sweep done"

log "=== WAL upload finished ==="
