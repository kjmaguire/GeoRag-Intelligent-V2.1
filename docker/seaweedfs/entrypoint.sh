#!/usr/bin/env sh
# =============================================================================
# docker/seaweedfs/entrypoint.sh
#
# SeaweedFS S3 server bootstrap. Renders /config/s3.json from MINIO_ROOT_USER /
# MINIO_ROOT_PASSWORD (the env-var convention is preserved from the MinIO era;
# the values are now read by SeaweedFS instead). Then runs `weed server` in
# all-in-one mode with the S3 API enabled.
#
# We keep MINIO_* env names because they are referenced from ~25 places across
# docker-compose.yml, .env.example, and Dagster/Laravel config — the cost of
# renaming them is far higher than the cosmetic benefit. The variables are
# documented as "S3-compatible object store credentials" wherever they live.
# =============================================================================

set -eu

: "${MINIO_ROOT_USER:?MINIO_ROOT_USER (S3 access key) is required}"
: "${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD (S3 secret key) is required}"

CONFIG_DIR=/config
mkdir -p "${CONFIG_DIR}"

cat > "${CONFIG_DIR}/s3.json" <<EOF
{
  "identities": [
    {
      "name": "${MINIO_ROOT_USER}",
      "credentials": [
        {
          "accessKey": "${MINIO_ROOT_USER}",
          "secretKey": "${MINIO_ROOT_PASSWORD}"
        }
      ],
      "actions": [
        "Admin",
        "Read",
        "Write",
        "List",
        "Tagging"
      ]
    }
  ]
}
EOF

chmod 600 "${CONFIG_DIR}/s3.json"

# Phase 0 storage-tier policy (master plan §22.1 + ADR-0001):
# The platform treats `hot`, `warm`, and `cold` as **logical** tiers in dev:
# all three are S3 buckets backed by the single physical /data volume here,
# and the Storage Tiering Agent (Phase 0 agent #3) moves objects between
# them based on `silver.storage_tier_policy`. Bucket creation lives in the
# minio-init compose service so this entrypoint stays small.
#
# In prod the three buckets are mapped to volumes with distinct -disk types
# (ssd / hdd / archive) — that's a Phase 11 hardening change, not a Phase 0
# requirement. Keeping single-volume here also avoids orphaning the existing
# /data state on the dev workstation.

# weed server: master + volume + filer + s3 in a single process.
# IMPORTANT: -volume is NOT default — without it, master has no writable
# volumes, the filer accepts metadata writes but the underlying bytes never
# persist (subtle: mc/SDK PUTs return 200 because the filer ACKs metadata,
# but `weed server` master logs `No writable volumes and no free volumes`
# and the data is silently lost on the next read).
# -s3.config points to the identity file rendered above.
# -dir is the data root for the underlying volume server.
# Default ports: 9333 master, 8080 volume, 8888 filer, 8333 s3.
#
# -volume.max=200 (was 32, bumped 2026-05-18): SeaweedFS pre-allocates ~7
# volumes per collection on first write. With the storage tiers (hot/warm/
# cold = 21), default + georag-backups + langfuse-events + langfuse-media +
# bronze + bronze-raster + exports active, 32 wasn't enough — Langfuse
# trace ingestion hit "No writable volumes and no free volumes left for
# {collection:langfuse-events}". 200 gives unlimited practical headroom;
# each unused volume is a small index entry only (no data cost).
exec weed server \
    -dir=/data \
    -master.volumeSizeLimitMB=1024 \
    -volume \
    -volume.max=200 \
    -filer \
    -s3 \
    -s3.port=8333 \
    -s3.config="${CONFIG_DIR}/s3.json"
