#!/usr/bin/env bash
# =============================================================================
# GeoRAG PostgreSQL Backup Script
# =============================================================================
#
# Runs inside georag-backup-agent (Alpine sidecar). Connects to the
# PostgreSQL service over the network via pg_basebackup.
#
# Physical base backup (pg_basebackup) produces a consistent, compressed
# directory-format archive that is then tar'd, uploaded to SeaweedFS via
# AWS CLI S3 API, and cleaned from staging.
#
# Coverage: pg_basebackup is CLUSTER-LEVEL — it captures every logical
# database on the server in one consistent snapshot. As of Phase 2 that
# is `georag`, `hatchet`, and `activepieces`. New logical DBs added in
# future phases are automatically included; no script change needed.
# (Phase 2 R-P2-8 — verified via scripts/phase2_rp28_backups_verify.sh.)
#
# NOTE: pg_basebackup connects directly to postgresql:5432 — NOT PgBouncer,
# because pg_basebackup requires a replication connection that PgBouncer
# does not proxy. The PGHOST env var in the sidecar is set to 'postgresql'.
#
# Environment variables (injected by compose env block):
#   PGHOST             — PG host (set to 'postgresql' by compose)
#   PGPORT             — PG port (default: 5432)
#   PGUSER             — PG superuser for pg_basebackup (default: georag)
#   PGPASSWORD         — PG password
#   PGDATABASE         — PG database name (informational only for basebackup)
#   AWS_ACCESS_KEY_ID  — SeaweedFS / S3 access key
#   AWS_SECRET_ACCESS_KEY — SeaweedFS / S3 secret key
#   S3_ENDPOINT_URL    — SeaweedFS S3 endpoint (e.g. http://minio:8333)
#
# Optional:
#   DRY_RUN=1              — log what would be done without executing
#   BACKUP_RETENTION_DAYS  — days to keep (default: 7)
#
# Usage (from Ofelia job-exec or manual drill):
#   docker exec georag-backup-agent /backup-scripts/postgresql/backup.sh
#   docker exec georag-backup-agent bash -c 'DRY_RUN=1 /backup-scripts/postgresql/backup.sh'
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
PGHOST="${PGHOST:-postgresql}"
PGPORT="${PGPORT:-5432}"
PGUSER="${PGUSER:-georag}"
PGPASSWORD="${PGPASSWORD:-}"
PGDATABASE="${PGDATABASE:-georag}"
AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-}"
AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-}"
S3_ENDPOINT_URL="${S3_ENDPOINT_URL:-}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
DRY_RUN="${DRY_RUN:-0}"

STAGING_BASE="/backup/staging/postgres"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H-%M-%SZ")
BASEBACKUP_DIR="${STAGING_BASE}/basebackup-${TIMESTAMP}"
ARTIFACT_NAME="pg-basebackup-${TIMESTAMP}.tar.gz"
STAGING_PATH="${STAGING_BASE}/${ARTIFACT_NAME}"
S3_BUCKET="georag-backups"
S3_PREFIX="postgres"
S3_DEST="s3://${S3_BUCKET}/${S3_PREFIX}/${ARTIFACT_NAME}"

# ---------------------------------------------------------------------------
# Logging helper — ISO 8601 timestamps to stderr
# ---------------------------------------------------------------------------
log() {
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*" >&2
}

log "=== PostgreSQL backup starting ==="
log "  Host:      ${PGHOST}:${PGPORT}"
log "  User:      ${PGUSER}"
log "  Database:  ${PGDATABASE}"
log "  Artifact:  ${ARTIFACT_NAME}"
log "  Staging:   ${STAGING_PATH}"
log "  S3 dest:   ${S3_DEST}"
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

# ---------------------------------------------------------------------------
# Dry-run path — log intent without executing
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" == "1" ]]; then
    log "DRY_RUN=1 — skipping actual backup execution"
    log "Would run: pg_basebackup -h ${PGHOST} -p ${PGPORT} -U ${PGUSER} -Ft -z --wal-method=stream -D ${BASEBACKUP_DIR}"
    log "Would run: tar -czf ${STAGING_PATH} -C ${STAGING_BASE} basebackup-${TIMESTAMP}"
    log "Would run: rm -rf ${BASEBACKUP_DIR}"
    log "Would run: aws s3 cp ${STAGING_PATH} ${S3_DEST} --endpoint-url ${S3_ENDPOINT_URL}"
    log "Would run: aws s3 ls s3://${S3_BUCKET}/${S3_PREFIX}/ --endpoint-url ${S3_ENDPOINT_URL} (then delete objects older than ${RETENTION_DAYS}d)"
    log "DRY_RUN complete — no files written, no S3 calls made"
    exit 0
fi

# ---------------------------------------------------------------------------
# Step 1 — Ensure staging directory exists
# ---------------------------------------------------------------------------
log "Step 1: Creating staging directory ${STAGING_BASE}"
mkdir -p "${STAGING_BASE}"

# ---------------------------------------------------------------------------
# Step 2 — pg_basebackup (networked, directory format)
# pg_basebackup -Ft produces a tar-format backup in the target directory.
# -z compresses each tar file with gzip.
# --wal-method=stream: stream WAL concurrently with the base backup so the
#   archive is self-contained and can be restored without pg_resetwal.
#   WAL is now archived separately to s3://georag-backups/pg-wal/ for PITR.
#   Module 1 Phase C prep (BK-03 fix, 2026-04-19) — replaces --wal-method=none.
# -D <dir>: write to directory (not stdout) so we can control the layout.
# The backup dir is then re-archived into a single artifact for S3.
# ---------------------------------------------------------------------------
log "Step 2: Running pg_basebackup (networked, directory format, --wal-method=stream)..."
export PGPASSWORD

pg_basebackup \
    -h "${PGHOST}" \
    -p "${PGPORT}" \
    -U "${PGUSER}" \
    -Ft \
    -z \
    --wal-method=stream \
    -D "${BASEBACKUP_DIR}"

log "Step 2a: Archiving basebackup directory to single artifact..."
tar -czf "${STAGING_PATH}" \
    -C "${STAGING_BASE}" \
    "basebackup-${TIMESTAMP}"

rm -rf "${BASEBACKUP_DIR}"

BACKUP_SIZE_BYTES=$(stat -c%s "${STAGING_PATH}" 2>/dev/null || echo "0")
log "Step 2 complete: ${ARTIFACT_NAME} — ${BACKUP_SIZE_BYTES} bytes"

# ---------------------------------------------------------------------------
# Step 3 — Upload to SeaweedFS via AWS CLI
# ---------------------------------------------------------------------------
log "Step 3: Uploading to ${S3_DEST}..."
export AWS_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY

aws s3 cp \
    "${STAGING_PATH}" \
    "${S3_DEST}" \
    --endpoint-url "${S3_ENDPOINT_URL}" \
    --no-progress

log "Step 3 complete: upload successful"

# ---------------------------------------------------------------------------
# Step 4 — Retention: delete objects older than RETENTION_DAYS
# ---------------------------------------------------------------------------
log "Step 4: Applying ${RETENTION_DAYS}-day retention on s3://${S3_BUCKET}/${S3_PREFIX}/"

# Compute cutoff as epoch seconds via shell arithmetic — avoids both BSD `date -v`
# and GNU `date -d "N days ago"` which busybox (Alpine) does not support.
CUTOFF_EPOCH=$(( $(date -u +%s) - RETENTION_DAYS * 86400 ))

aws s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}/" \
    --endpoint-url "${S3_ENDPOINT_URL}" \
    | while read -r _date _time _size obj_name; do
        obj_date_str="${_date} ${_time}"
        # AWS CLI s3 ls timestamps are UTC; date -u -d parses ISO-like format.
        # On Alpine bash (GNU bash) 'date -d' works; busybox ash does not.
        # The sidecar image ships bash so this is safe.
        obj_epoch=$(date -u -d "${obj_date_str}" +%s 2>/dev/null || echo "0")
        if [[ "${obj_epoch}" -gt 0 && "${obj_epoch}" -lt "${CUTOFF_EPOCH}" ]]; then
            log "Deleting expired object: ${obj_name} (${_date})"
            aws s3 rm "s3://${S3_BUCKET}/${S3_PREFIX}/${obj_name}" \
                --endpoint-url "${S3_ENDPOINT_URL}"
        fi
    done

log "Step 4 complete: retention sweep done"

# ---------------------------------------------------------------------------
# Step 5 — Remove local staging artifact (S3 is the durable copy)
# ---------------------------------------------------------------------------
log "Step 5: Removing local staging artifact..."
rm -f "${STAGING_PATH}"
log "Step 5 complete"

log "=== PostgreSQL backup finished: ${ARTIFACT_NAME} (${BACKUP_SIZE_BYTES} bytes) ==="
