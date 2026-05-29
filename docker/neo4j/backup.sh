#!/usr/bin/env bash
# =============================================================================
# GeoRAG Neo4j Backup Script — Offline Dump via Docker Socket
# =============================================================================
#
# WARNING: stops Neo4j for ~75-120s. Runs only in approved maintenance
# window (Sundays 03:00 UTC). First live run approved for 2026-04-26.
#
# Neo4j Community Edition does not support online backup (Enterprise-only).
# This script performs an offline dump by:
#   1. Stopping the running neo4j container via docker CLI
#   2. Running a one-shot neo4j container with the data volume mounted to
#      execute `neo4j-admin database dump`
#   3. Restarting the original container and waiting for healthy
#   4. Archiving the dump, uploading to S3, and sweeping retention
#
# Safety gate: ALLOW_WEEKLY_DUMP=1 is required (set by Ofelia label env or
# wrapper command). DRY_RUN=1 bypasses both the guard and the actual dump.
# On ANY failure: attempt docker start before exiting non-zero — never
# leave Neo4j down.
#
# Environment variables (injected by compose env block):
#   NEO4J_BACKUP_CONTAINER — container name to stop/start (default: georag-neo4j)
#   NEO4J_IMAGE            — exact image string for the one-shot dump container
#   AWS_ACCESS_KEY_ID      — SeaweedFS / S3 access key
#   AWS_SECRET_ACCESS_KEY  — SeaweedFS / S3 secret key
#   S3_ENDPOINT_URL        — SeaweedFS S3 endpoint (e.g. http://minio:8333)
#
# Optional:
#   NEO4J_DB_NAME          — database name (default: neo4j)
#   BACKUP_RETENTION_DAYS  — days to retain in S3 (default: 7)
#   ALLOW_WEEKLY_DUMP=1    — required safety gate for live runs
#   DRY_RUN=1              — log without executing, no container stop
#
# Usage (from Ofelia job-exec or manual drill):
#   docker exec georag-backup-agent bash -c 'DRY_RUN=1 /backup-scripts/neo4j/backup.sh'
#   docker exec georag-backup-agent bash -c 'ALLOW_WEEKLY_DUMP=1 /backup-scripts/neo4j/backup.sh'
# =============================================================================

set -euo pipefail

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
NEO4J_BACKUP_CONTAINER="${NEO4J_BACKUP_CONTAINER:-georag-neo4j}"
NEO4J_IMAGE="${NEO4J_IMAGE:-neo4j:2026-community@sha256:a5feb81d916c82d09186807ee8f8a523eb430d578fa6015f37ae72a07f976537}"
NEO4J_DB_NAME="${NEO4J_DB_NAME:-neo4j}"
AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID:-}"
AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY:-}"
S3_ENDPOINT_URL="${S3_ENDPOINT_URL:-}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
DRY_RUN="${DRY_RUN:-0}"
ALLOW_WEEKLY_DUMP="${ALLOW_WEEKLY_DUMP:-0}"

# Named volume expected to hold the neo4j data directory.
# Override NEO4J_DATA_VOLUME explicitly if your compose project name differs from the default
# (e.g. a non-standard COMPOSE_PROJECT_NAME produces a different volume prefix).
# If unset, the volume is resolved dynamically by scanning `docker volume ls`.
NEO4J_DATA_VOLUME="${NEO4J_DATA_VOLUME:-$(docker volume ls -q | grep -E '(^|_)neo4j_data$' | head -1)}"
if [ -z "${NEO4J_DATA_VOLUME}" ]; then
    log "ERROR: could not resolve Neo4j data volume; set NEO4J_DATA_VOLUME env explicitly" >&2
    exit 1
fi
BACKUP_STAGING_VOLUME="GeoRag_Intelligence_V1.0_backup_staging"

STAGING_BASE="/backup/staging/neo4j"
TIMESTAMP=$(date -u +"%Y-%m-%dT%H-%M-%SZ")
DUMP_FILENAME="${NEO4J_DB_NAME}.dump"
STAGING_DUMP="${STAGING_BASE}/${DUMP_FILENAME}"
ARTIFACT_NAME="neo4j-dump-${TIMESTAMP}.tar.gz"
ARTIFACT_PATH="${STAGING_BASE}/${ARTIFACT_NAME}"
S3_BUCKET="georag-backups"
S3_PREFIX="neo4j"
S3_DEST="s3://${S3_BUCKET}/${S3_PREFIX}/${ARTIFACT_NAME}"

# ---------------------------------------------------------------------------
# Logging helper — ISO 8601 timestamps to stderr
# ---------------------------------------------------------------------------
log() {
    echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*" >&2
}

log "=== Neo4j backup starting ==="
log "  Container: ${NEO4J_BACKUP_CONTAINER}"
log "  Database:  ${NEO4J_DB_NAME}"
log "  Image:     ${NEO4J_IMAGE}"
log "  Staging:   ${STAGING_BASE}"
log "  Artifact:  ${ARTIFACT_NAME}"
log "  S3 dest:   ${S3_DEST}"
log "  Retention: ${RETENTION_DAYS} days"
log "  Dry-run:   ${DRY_RUN}"

# ---------------------------------------------------------------------------
# Safety gate — require explicit opt-in for live offline dump
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" != "1" && "${ALLOW_WEEKLY_DUMP}" != "1" ]]; then
    log "ERROR: Neo4j offline dump must be invoked with ALLOW_WEEKLY_DUMP=1 or DRY_RUN=1" >&2
    log "ERROR: This is a safety gate — live dumps stop Neo4j for ~75-120s." >&2
    log "ERROR: Approved maintenance window: Sundays 03:00 UTC." >&2
    exit 1
fi

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
# Dry-run path — log intent, NO container stop
# ---------------------------------------------------------------------------
if [[ "${DRY_RUN}" == "1" ]]; then
    log "DRY_RUN=1 — skipping actual backup execution"
    log "Would run: docker stop ${NEO4J_BACKUP_CONTAINER}  (stops Neo4j ~75-120s)"
    log "Would run: docker run --rm -v ${NEO4J_DATA_VOLUME}:/data -v ${BACKUP_STAGING_VOLUME}:/backups ${NEO4J_IMAGE} neo4j-admin database dump ${NEO4J_DB_NAME} --to-path=/backups/neo4j/"
    log "Would run: docker start ${NEO4J_BACKUP_CONTAINER}"
    log "Would run: (wait for healthy, up to 180s)"
    log "Would run: tar czf ${ARTIFACT_PATH} -C ${STAGING_BASE} ${DUMP_FILENAME}"
    log "Would run: aws s3 cp ${ARTIFACT_PATH} ${S3_DEST} --endpoint-url ${S3_ENDPOINT_URL}"
    log "Would run: retention sweep on s3://${S3_BUCKET}/${S3_PREFIX}/"
    log "DRY_RUN complete — no files written, no S3 calls made, Neo4j NOT stopped"
    exit 0
fi

# ---------------------------------------------------------------------------
# Ensure staging directory
# ---------------------------------------------------------------------------
log "Step 1: Creating staging directory ${STAGING_BASE}"
mkdir -p "${STAGING_BASE}"

# ---------------------------------------------------------------------------
# Verify the data volume exists before stopping Neo4j
# ---------------------------------------------------------------------------
log "Step 2: Verifying Neo4j data volume: ${NEO4J_DATA_VOLUME}"
if ! docker volume inspect "${NEO4J_DATA_VOLUME}" > /dev/null 2>&1; then
    log "ERROR: Named volume '${NEO4J_DATA_VOLUME}' not found." >&2
    log "ERROR: Run 'docker volume ls | grep neo4j' to see available volumes." >&2
    exit 1
fi
log "Step 2 complete: volume confirmed"

# ---------------------------------------------------------------------------
# Step 3 — Stop Neo4j
# From this point on: always attempt restart on failure
# ---------------------------------------------------------------------------
log "Step 3: Stopping ${NEO4J_BACKUP_CONTAINER}..."
NEO4J_RESTART_NEEDED=1

cleanup_neo4j() {
    if [[ "${NEO4J_RESTART_NEEDED:-0}" == "1" ]]; then
        log "CLEANUP: Attempting to restart ${NEO4J_BACKUP_CONTAINER}..."
        docker start "${NEO4J_BACKUP_CONTAINER}" 2>/dev/null || \
            log "CLEANUP WARNING: Failed to restart ${NEO4J_BACKUP_CONTAINER} — manual intervention required"
    fi
}
trap cleanup_neo4j EXIT

docker stop "${NEO4J_BACKUP_CONTAINER}"
log "Step 3 complete: ${NEO4J_BACKUP_CONTAINER} stopped"

# ---------------------------------------------------------------------------
# Step 4 — Offline dump via one-shot neo4j container
# Mounts the neo4j data volume and the backup_staging volume.
# neo4j-admin database dump writes a .dump file to /backups/neo4j/
# (maps to /backup/staging/neo4j/ on the staging volume).
# ---------------------------------------------------------------------------
log "Step 4: Running offline dump via one-shot container..."

# Remove any stale dump file from a previous failed run
rm -f "${STAGING_DUMP}"

docker run --rm \
    --volume "${NEO4J_DATA_VOLUME}:/data" \
    --volume "${BACKUP_STAGING_VOLUME}:/backups" \
    --user neo4j \
    "${NEO4J_IMAGE}" \
    neo4j-admin database dump \
        "${NEO4J_DB_NAME}" \
        --to-path=/backups/neo4j/

log "Step 4 complete: dump written to ${STAGING_DUMP}"

# ---------------------------------------------------------------------------
# Step 5 — Restart Neo4j and wait for healthy
# ---------------------------------------------------------------------------
log "Step 5: Restarting ${NEO4J_BACKUP_CONTAINER}..."
docker start "${NEO4J_BACKUP_CONTAINER}"
NEO4J_RESTART_NEEDED=0   # restart issued — clear the trap flag

log "Step 5: Waiting for ${NEO4J_BACKUP_CONTAINER} to become healthy (up to 180s)..."
WAIT_START=$(date +%s)
WAIT_LIMIT=180
while true; do
    HEALTH=$(docker inspect --format '{{.State.Health.Status}}' "${NEO4J_BACKUP_CONTAINER}" 2>/dev/null || echo "unknown")
    if [[ "${HEALTH}" == "healthy" ]]; then
        log "Step 5 complete: ${NEO4J_BACKUP_CONTAINER} is healthy"
        break
    fi
    ELAPSED=$(( $(date +%s) - WAIT_START ))
    if [[ "${ELAPSED}" -gt "${WAIT_LIMIT}" ]]; then
        log "WARNING: ${NEO4J_BACKUP_CONTAINER} did not reach healthy within ${WAIT_LIMIT}s (status: ${HEALTH})"
        log "WARNING: Neo4j container was restarted — check 'docker logs ${NEO4J_BACKUP_CONTAINER}' for errors"
        break
    fi
    log "  Health: ${HEALTH} (${ELAPSED}s elapsed, waiting...)"
    sleep 5
done

# ---------------------------------------------------------------------------
# Step 6 — Archive the dump
# ---------------------------------------------------------------------------
log "Step 6: Archiving dump to ${ARTIFACT_PATH}..."

if [[ ! -f "${STAGING_DUMP}" ]]; then
    log "ERROR: Expected dump file not found at ${STAGING_DUMP}" >&2
    exit 1
fi

tar czf "${ARTIFACT_PATH}" \
    -C "${STAGING_BASE}" \
    "${DUMP_FILENAME}"

rm -f "${STAGING_DUMP}"

BACKUP_SIZE_BYTES=$(stat -c%s "${ARTIFACT_PATH}" 2>/dev/null || echo "0")
log "Step 6 complete: ${ARTIFACT_NAME} — ${BACKUP_SIZE_BYTES} bytes"

# ---------------------------------------------------------------------------
# Step 7 — Upload to SeaweedFS via AWS CLI
# ---------------------------------------------------------------------------
log "Step 7: Uploading to ${S3_DEST}..."
export AWS_ACCESS_KEY_ID
export AWS_SECRET_ACCESS_KEY

aws s3 cp \
    "${ARTIFACT_PATH}" \
    "${S3_DEST}" \
    --endpoint-url "${S3_ENDPOINT_URL}" \
    --no-progress

log "Step 7 complete: upload successful"

# ---------------------------------------------------------------------------
# Step 8 — Retention: delete objects older than RETENTION_DAYS
# ---------------------------------------------------------------------------
log "Step 8: Applying ${RETENTION_DAYS}-day retention on s3://${S3_BUCKET}/${S3_PREFIX}/"

CUTOFF_EPOCH=$(( $(date -u +%s) - RETENTION_DAYS * 86400 ))

aws s3 ls "s3://${S3_BUCKET}/${S3_PREFIX}/" \
    --endpoint-url "${S3_ENDPOINT_URL}" \
    | while read -r _date _time _size obj_name; do
        obj_date_str="${_date} ${_time}"
        obj_epoch=$(date -u -d "${obj_date_str}" +%s 2>/dev/null || echo "0")
        if [[ "${obj_epoch}" -gt 0 && "${obj_epoch}" -lt "${CUTOFF_EPOCH}" ]]; then
            log "Deleting expired object: ${obj_name} (${_date})"
            aws s3 rm "s3://${S3_BUCKET}/${S3_PREFIX}/${obj_name}" \
                --endpoint-url "${S3_ENDPOINT_URL}"
        fi
    done

log "Step 8 complete: retention sweep done"

# ---------------------------------------------------------------------------
# Step 9 — Remove local staging artifact
# ---------------------------------------------------------------------------
log "Step 9: Removing local staging artifact..."
rm -f "${ARTIFACT_PATH}"
log "Step 9 complete"

log "=== Neo4j backup finished: ${ARTIFACT_NAME} (${BACKUP_SIZE_BYTES} bytes) ==="
