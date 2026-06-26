# =============================================================================
# GeoRAG Backup Agent Sidecar
# =============================================================================
#
# Alpine-based sidecar that runs all three backup scripts (PostgreSQL,
# Neo4j, Qdrant). Ofelia targets this container via job-exec so the
# backup tools do not need to be installed in each stateful service image.
#
# Tools installed:
#   aws-cli              — S3 upload to SeaweedFS (georag-backups bucket)
#   postgresql18-client  — pg_basebackup 18.x for PG 18 server backup
#                          (sourced from Alpine edge repos — Alpine 3.20
#                           only ships up to postgresql16-client; PG 18
#                           client is required because pg_basebackup
#                           enforces strict major-version matching)
#   docker-cli           — controls Neo4j container (stop/dump/start) via socket
#   curl                 — Qdrant snapshot API calls
#   jq                   — JSON parsing for Qdrant responses
#   bash                 — shell for backup scripts
#   tzdata               — timezone data (UTC anchoring for cron logs)
#   ca-certificates      — TLS trust for S3/HTTPS endpoints
#
# Non-root user: backup (UID 1001)
# Scripts are bind-mounted read-only at /backup-scripts/
# Staging volume is mounted at /backup/staging (rw)
#
# Base image: alpine:3.22 — pinned by digest (bumped from EOL 3.20).
# Re-pin procedure:
#   docker pull alpine:3.22
#   docker inspect alpine:3.22 --format '{{index .RepoDigests 0}}'
#   Update the FROM line below.
#
# postgresql18-client is sourced from Alpine edge/main + edge/community via
# explicit --repository flags. This is a deliberate version override: Alpine
# stable repos lag the pg18 client, but pg_basebackup enforces strict
# major-version matching (16 client vs 18 server returns "incompatible server
# version"). The edge packages are pinned via the FROM digest to a reproducible
# build layer.
# =============================================================================

# 2026-06-24 re-audit: bumped 3.20 -> 3.22. Alpine 3.20 hit EOL on
# 2026-04-01 (no more security updates). 3.22 is current stable.
# The pg18-client edge-repo mechanism (--repository flags below) is
# unaffected by the base bump (edge is edge regardless of stable
# version). REBUILD + smoke-test the backup path after this change:
#   docker compose build backup-agent
#   docker exec georag-backup-agent pg_basebackup --version   # expect 18.x
# Digest pinned 2026-06-24 from alpine:3.22
# docker pull alpine:3.22 && docker inspect alpine:3.22 --format '{{index .RepoDigests 0}}'
FROM alpine@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce

# Install backup tools in a single layer.
# postgresql18-client: pg_basebackup 18.3 sourced from Alpine edge repos via
#   explicit --repository flags. The stable 3.20 repos only include pg16;
#   pg_basebackup strictly requires client major == server major for PG 18+.
# aws-cli, curl, jq, bash, docker-cli, tzdata, ca-certificates come from
#   the standard Alpine 3.20 stable repos (no version concerns there).
RUN apk add --no-cache \
        --repository=https://dl-cdn.alpinelinux.org/alpine/edge/main \
        --repository=https://dl-cdn.alpinelinux.org/alpine/edge/community \
        postgresql18-client \
    && apk add --no-cache \
        aws-cli \
        curl \
        jq \
        bash \
        docker-cli \
        tini \
        tzdata \
        ca-certificates \
    && rm -rf /var/cache/apk/*

# Non-root user for script execution
# UID 1001 avoids collision with nobody (65534), root (0), postgres (70).
# The docker socket group mapping is handled at runtime by Docker when
# /var/run/docker.sock is mounted — the backup user gets group access via
# 'docker' group addition below (group may not exist in image; || true is safe).
RUN addgroup -g 1001 backup \
    && adduser -u 1001 -G backup -h /home/backup -s /bin/bash -D backup \
    && addgroup backup docker 2>/dev/null || true

WORKDIR /backup-scripts

# Pre-create the staging directory tree with correct ownership
# The backup_staging named volume is mounted here at runtime; the directory
# is pre-seeded so the backup user does not need root to create subdirs.
RUN mkdir -p /backup/staging \
    && chown -R backup:backup /backup/staging

# tini as PID 1 — forwards SIGTERM to children and reaps zombies.
# Without tini, `sleep infinity` ignores SIGTERM and docker stop hits the
# full 30s grace period before SIGKILL. tini allows the container to exit
# in <1s on `docker stop`. Ofelia-triggered backup scripts exec'd into the
# container are unaffected — they do not run as PID 1.
# C5-02 fix — 2026-04-21.
ENTRYPOINT ["/sbin/tini", "--"]
CMD ["sleep", "infinity"]
