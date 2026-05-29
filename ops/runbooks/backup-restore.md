# Backup and Restore Runbook

Documents what is backed up, when, where, and how to restore it. Use this when you need to verify backup health, trigger a manual backup, or recover data after a failure.

---

## Backup overview

| Store | Tool | Schedule (UTC) | S3 destination | Retention |
|---|---|---|---|---|
| PostgreSQL (base) | `pg_basebackup` | Daily 02:30 | `s3://georag-backups/postgres/` | 7 days |
| PostgreSQL (WAL) | `wal-upload.sh` | Every 5 min | `s3://georag-backups/pg-wal/` | 8 days |
| Qdrant | Snapshot API | Daily 03:00 | `s3://georag-backups/qdrant/<collection>/` | 7 days |
| Neo4j | Offline dump | Weekly Sunday 03:00 | `s3://georag-backups/neo4j/` | 7 days |
| SeaweedFS (object store) | None automated | — | — | See §SeaweedFS below |

All backups run inside `georag-backup-agent` (Alpine sidecar). Scheduling is via Ofelia (`georag-ofelia`). Both containers are in the `dev-data` / `dev-full` profiles.

Scripts live at:
- `docker/postgresql/backup.sh`
- `docker/postgresql/wal-upload.sh`
- `docker/neo4j/backup.sh`
- `docker/qdrant/backup.sh`

S3 credentials (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `S3_ENDPOINT_URL`) are injected from `.env` into the backup-agent container. If they are missing, all four scripts will exit 1 immediately with a clear error.

---

## Verify Ofelia has jobs loaded

```bash
docker compose logs ofelia 2>&1 | grep -i "registered\|loaded\|job"
```

Expected output (4 lines):

```
New job registered "pg-backup"        "0 30 2 * * *"
New job registered "neo4j-backup"     "0 45 2 * * *"  (ALLOW_WEEKLY_DUMP=1 guard — only fires Sunday)
New job registered "qdrant-backup"    "0 0 3 * * *"
New job registered "pg-wal-upload"    "@every 5m"
```

If zero jobs are registered, check that the `georag-backup-agent` container is healthy and the Ofelia labels are not disabled. See `ops/runbooks/service-outage.md §7`.

---

## Manual backup commands

All commands exec into `georag-backup-agent`. The `-lc` flag ensures the container's login shell loads the environment (including AWS credentials).

### PostgreSQL base backup

```bash
docker exec georag-backup-agent bash -lc '/backup-scripts/postgresql/backup.sh'
```

Produces: `pg-basebackup-<timestamp>.tar.gz` (~175 MiB). Uploads to `s3://georag-backups/postgres/`. Wall time: ~20–30 seconds.

Dry-run (logs intent, no I/O):

```bash
docker exec georag-backup-agent bash -lc 'DRY_RUN=1 /backup-scripts/postgresql/backup.sh'
```

### PostgreSQL WAL upload (manual flush — rarely needed)

```bash
docker exec georag-backup-agent bash -lc '/backup-scripts/postgresql/wal-upload.sh'
```

Syncs any WAL segments in the `pg_wal_archive` volume to `s3://georag-backups/pg-wal/`. Normally runs automatically every 5 minutes via Ofelia. Run manually if you suspect WAL is lagging before a planned maintenance window.

### Activate WAL archiving on existing cluster (one-time)

WAL archiving is wired up via `docker/compose.wal-archiving.yml` (overlay) plus the activation SQL at `docker/postgresql/init/Z_activate_wal_archiving.sql`. **Fresh-init clusters activate automatically.** Existing volumes — where the postgres entrypoint skips `/docker-entrypoint-initdb.d/` because `PG_VERSION` already exists in `PGDATA` — must be activated manually:

```bash
# 1. Apply the compose overlay so the pg_wal_archive volume is mounted.
#    The volume mount survives the activation; the bind-mount for the SQL
#    file is harmless on existing volumes (init scripts skipped).
docker compose \
    -f compose.yml \
    -f docker/compose.wal-archiving.yml \
    up -d postgresql

# 2. Wait for postgres to be healthy.
until docker exec georag-postgresql pg_isready -U georag > /dev/null 2>&1; do
    sleep 1
done

# 3. Run the activation SQL manually. Sets archive_mode, archive_command,
#    archive_timeout via ALTER SYSTEM (writes to postgresql.auto.conf).
docker exec georag-postgresql psql -U georag -d georag \
    -f /docker-entrypoint-initdb.d/Z_activate_wal_archiving.sql

# 4. Restart postgres so archive_mode takes effect (postmaster-level setting).
docker compose restart postgresql

# 5. Verify archiving is live.
docker exec georag-postgresql psql -U georag -d georag -c \
    "SELECT name, setting FROM pg_settings WHERE name LIKE 'archive_%' ORDER BY name;"
# Expected:
#   archive_command  | test ! -f /pg_wal_archive/%f && cp %p /pg_wal_archive/%f
#   archive_mode     | on
#   archive_timeout  | 60

# 6. Force a WAL switch and confirm the segment lands in /pg_wal_archive.
docker exec georag-postgresql psql -U georag -d georag -c "SELECT pg_switch_wal();"
sleep 5
docker exec georag-postgresql ls -lh /pg_wal_archive/ | head -5
# Expected: at least one file (16 MiB segment, e.g. 000000010000000000000005).

# 7. Confirm the next Ofelia tick uploads it (or run manually):
docker exec georag-backup-agent /backup-scripts/postgresql/wal-upload.sh
# Then verify in S3:
docker exec georag-backup-agent aws s3 ls "s3://georag-backups/pg-wal/" \
    --endpoint-url "$S3_ENDPOINT_URL" | tail -5
```

The PostgresArchiveCommandFailing and PostgresArchiveLagHigh Prometheus alerts (in `docker/prometheus/rules/postgres-alerts.yml`) become live the moment archive_mode flips on. They'll fire if `archive_command` starts failing or no WAL has been archived in 30+ minutes.

### PITR (point-in-time recovery)

Once archiving is active, restoring to a specific point in time uses pg_basebackup + WAL replay. See §B (full in-place restore) below for the basebackup-from-S3 procedure; for PITR add a `recovery.signal` file and `restore_command` to the recovery cluster pointing at `s3://georag-backups/pg-wal/`. Detailed PITR procedure: TODO once first restore drill is exercised against staging.

### Qdrant snapshot backup

```bash
docker exec georag-backup-agent bash -lc '/backup-scripts/qdrant/backup.sh'
```

Snapshots all collections, uploads per-collection to S3. Wall time: ~29 seconds for current dataset (~234 MiB across 5 collections).

Dry-run:

```bash
docker exec georag-backup-agent bash -lc 'DRY_RUN=1 /backup-scripts/qdrant/backup.sh'
```

### Neo4j offline dump

**Warning: stops the Neo4j container for approximately 75–120 seconds.** Do not run during active user sessions.

Requires the `ALLOW_WEEKLY_DUMP=1` safety guard. Without it the script exits 1 immediately:

```bash
docker exec georag-backup-agent bash -lc 'ALLOW_WEEKLY_DUMP=1 /backup-scripts/neo4j/backup.sh'
```

The script automatically restarts Neo4j and waits up to 180 seconds for it to become healthy before exiting. If Neo4j does not recover within 180 seconds, a warning is logged and the script exits — check `docker logs georag-neo4j` immediately.

Dry-run (no container stop):

```bash
docker exec georag-backup-agent bash -lc 'DRY_RUN=1 /backup-scripts/neo4j/backup.sh'
```

**Non-standard project name:** If you run the stack under a different `COMPOSE_PROJECT_NAME`, the Neo4j data volume will have a different prefix. Set `NEO4J_DATA_VOLUME` explicitly to override the dynamic lookup: `docker exec georag-backup-agent bash -lc 'NEO4J_DATA_VOLUME=myproject_neo4j_data ALLOW_WEEKLY_DUMP=1 /backup-scripts/neo4j/backup.sh'`.

---

## Restore procedures

### A — PostgreSQL restore (throwaway container — non-destructive verification)

Use this to verify a backup is valid without touching the live database.

```bash
# 1. Download the backup artifact from S3 into a temp location on the host.
#    Replace <artifact> with the actual filename from: aws s3 ls s3://georag-backups/postgres/
ARTIFACT=pg-basebackup-2026-04-19T18-36-44Z.tar.gz

docker run --rm \
  --network georag \
  --name test-pg-restore \
  -e AWS_ACCESS_KEY_ID=<your_key> \
  -e AWS_SECRET_ACCESS_KEY=<your_secret> \
  postgis/postgis:18-3.6-alpine \
  sh -c "
    apk add --no-cache aws-cli &&
    aws s3 cp s3://georag-backups/postgres/${ARTIFACT} /tmp/${ARTIFACT} \
      --endpoint-url http://minio:8333 &&
    mkdir -p /var/lib/postgresql/data &&
    cd /tmp &&
    tar xzf ${ARTIFACT} &&
    cd basebackup-* &&
    tar xzf base.tar.gz -C /var/lib/postgresql/data &&
    chown -R postgres /var/lib/postgresql/data &&
    su postgres -c 'pg_ctl start -D /var/lib/postgresql/data -o \"-c listen_addresses=\" -l /tmp/pg.log' &&
    sleep 3 &&
    su postgres -c 'psql -c \"SELECT count(*) FROM information_schema.tables\"'
  "
```

Expected: count returns 284 (as of Phase C baseline). Any non-zero count with no error = restore good. Wall time: ~23 seconds.

**The `--wal-method=stream` flag** in the current backup script makes the archive self-contained — `pg_resetwal` is no longer needed. If you encounter a WAL-related error on restore, the archive was created with an older version of the script (`--wal-method=none`). In that case add `pg_resetwal -f /var/lib/postgresql/data` before the `pg_ctl start` step.

Remove the test container when done:

```bash
docker rm -f test-pg-restore 2>/dev/null || true
```

### B — PostgreSQL full in-place restore (DESTRUCTIVE — destroys live data)

Only do this after a catastrophic data loss. This replaces the live `postgres_data` volume. Requires a maintenance window.

```bash
# DESTRUCTIVE — stops PostgreSQL and replaces its data volume.
# Run these commands in order. Do NOT skip steps.

# 1. Stop services that depend on PostgreSQL.
docker compose --profile dev-light --profile dev-data stop \
  laravel-octane laravel-horizon laravel-reverb fastapi dagster-daemon dagster-webserver

# 2. Stop PostgreSQL.
docker compose stop postgresql pgbouncer

# 3. Remove the data volume (POINT OF NO RETURN — live data is gone after this).
docker volume rm "$(docker volume ls -q | grep postgres_data)"

# 4. Start a throwaway restore container using the procedure from §A above,
#    but mount the named volume instead of a temp path:
#    Replace <artifact> with the actual S3 object name.
ARTIFACT=pg-basebackup-<timestamp>.tar.gz

docker run --rm \
  --network georag \
  -v georag_postgres_data:/var/lib/postgresql/data \
  -e AWS_ACCESS_KEY_ID=<your_key> \
  -e AWS_SECRET_ACCESS_KEY=<your_secret> \
  postgis/postgis:18-3.6-alpine \
  sh -c "
    apk add --no-cache aws-cli &&
    aws s3 cp s3://georag-backups/postgres/${ARTIFACT} /tmp/${ARTIFACT} \
      --endpoint-url http://minio:8333 &&
    cd /tmp && tar xzf ${ARTIFACT} && cd basebackup-* &&
    tar xzf base.tar.gz -C /var/lib/postgresql/data &&
    chown -R 70:70 /var/lib/postgresql/data
  "

# 5. Bring PostgreSQL back up.
docker compose up -d postgresql pgbouncer

# 6. Verify.
docker exec georag-postgresql pg_isready -U georag -d georag
docker exec georag-postgresql psql -U georag -c "SELECT count(*) FROM information_schema.tables;"

# 7. Restart application services.
docker compose --profile dev-light --profile dev-data up -d
```

**Note on `pg_hba.conf`:** The bind-mount at `docker/postgresql/pg_hba.conf` is activated via `-c hba_file=/etc/postgresql/pg_hba.conf`. A fresh volume restore will automatically use this file on next PostgreSQL start — no manual pg_hba editing is needed.

### C — Qdrant restore (snapshot recover API)

Qdrant snapshot restores use the upload API. The Qdrant image is distroless — run all restore commands from a sidecar helper container.

```bash
# Replace <collection> and <snapshot_name> with values from:
#   aws s3 ls s3://georag-backups/qdrant/<collection>/ --endpoint-url http://localhost:8333
COLLECTION=georag_reports
SNAPSHOT=georag_reports-2146751740141300-2026-04-19-21-02-22.snapshot

# 1. Download the snapshot from S3 using the backup-agent container.
docker exec georag-backup-agent \
  aws s3 cp \
    "s3://georag-backups/qdrant/${COLLECTION}/${SNAPSHOT}" \
    "/backup/staging/qdrant/${SNAPSHOT}" \
    --endpoint-url http://minio:8333

# 2. Upload the snapshot to the running Qdrant instance via the restore API.
#    priority=snapshot ensures the uploaded data overwrites any existing collection data.
docker exec georag-backup-agent \
  curl -sf -X POST \
    "http://qdrant:6333/collections/${COLLECTION}/snapshots/upload?priority=snapshot" \
    -H "Content-Type: multipart/form-data" \
    -F "snapshot=@/backup/staging/qdrant/${SNAPSHOT}" \
    | jq .

# 3. Verify the collection.
curl -sf "http://localhost:6333/collections/${COLLECTION}" | jq '.result.points_count'
# Expected: matches the point count at backup time (e.g., 18 for georag_reports as of Phase C)

# 4. Clean up staging.
docker exec georag-backup-agent rm -f "/backup/staging/qdrant/${SNAPSHOT}"
```

**If the collection already exists and you want to overwrite it:** The `priority=snapshot` parameter handles this. If you get a conflict error, delete the collection first:

```bash
curl -sf -X DELETE "http://localhost:6333/collections/${COLLECTION}" | jq .
# Then repeat the upload step above.
```

Wall time: ~2 seconds for a 321 KiB snapshot (Phase C baseline). Larger collections scale proportionally.

**Cluster awareness note:** These restore steps assume a single-node Qdrant deployment. If Qdrant is ever scaled to multiple nodes, snapshots become per-node and must be restored to the correct node. Document the node count before upgrading.

### D — Neo4j restore

Neo4j restore requires the container to be stopped. CE has no online restore capability.

The first confirmed live dump artifact is: `neo4j-dump-2026-04-19T18-55-00Z.tar.gz` (~51 MiB), in `s3://georag-backups/neo4j/`.

```bash
# 1. Download the dump archive.
ARTIFACT=neo4j-dump-<timestamp>.tar.gz

docker exec georag-backup-agent \
  aws s3 cp \
    "s3://georag-backups/neo4j/${ARTIFACT}" \
    "/backup/staging/neo4j/${ARTIFACT}" \
    --endpoint-url http://minio:8333

# 2. Stop Neo4j (and services that depend on it).
docker compose --profile dev-light --profile dev-data stop \
  fastapi laravel-horizon laravel-reverb neo4j-warmup neo4j

# 3. Extract the dump from the archive into the staging volume.
docker exec georag-backup-agent \
  sh -c "cd /backup/staging/neo4j && tar xzf ${ARTIFACT}"
# Produces: /backup/staging/neo4j/neo4j.dump

# 4. Run neo4j-admin database load via a one-shot container.
#    Uses the same digest-pinned image as the backup script.
NEO4J_IMAGE="neo4j:2026-community@sha256:a5feb81d916c82d09186807ee8f8a523eb430d578fa6015f37ae72a07f976537"
NEO4J_DATA_VOLUME="GeoRag_Intelligence_V1.0_neo4j_data"
BACKUP_STAGING_VOLUME="GeoRag_Intelligence_V1.0_backup_staging"

docker run --rm \
  --volume "${NEO4J_DATA_VOLUME}:/data" \
  --volume "${BACKUP_STAGING_VOLUME}:/backups" \
  --user neo4j \
  "${NEO4J_IMAGE}" \
  neo4j-admin database load neo4j \
    --from-path=/backups/neo4j/ \
    --overwrite-destination=true

# 5. Restart Neo4j and wait for healthy.
docker compose up -d neo4j
# Poll until healthy (up to 120s):
until [ "$(docker inspect --format '{{.State.Health.Status}}' georag-neo4j)" = "healthy" ]; do
  echo "Waiting for Neo4j..." && sleep 5
done

# 6. Verify.
docker exec georag-neo4j cypher-shell -a bolt://localhost:7687 \
  -u neo4j -p "${NEO4J_AUTH#neo4j/}" \
  "MATCH (n) RETURN count(n) AS node_count"
# Expected: non-zero count matching your graph size.

# 7. Restart dependent services.
docker compose --profile dev-light --profile dev-data up -d

# 8. Clean up staging.
docker exec georag-backup-agent rm -f /backup/staging/neo4j/neo4j.dump
```

Estimated total restore time (stop to healthy): under 5 minutes for the current dataset.

### E — SeaweedFS restore

SeaweedFS is single-node with no automated backup. The migration snapshot from 2026-04-18 is at `backups/minio-snapshot/` on the host. There is no ongoing S3 backup of SeaweedFS itself.

**Current status:** A restore drill for SeaweedFS is deferred pending a clear strategy decision (SeaweedFS-internal volume replication vs. offsite S3 sync to a secondary destination). This was flagged in Phase A (BK-07) and is not yet resolved.

**What this means in practice:** If the `minio_data` volume is lost, all objects in `georag-bronze` and `georag-exports` (raw ingested files) are unrecoverable from backup. The migration snapshot (`backups/minio-snapshot/`) can restore the state as of 2026-04-18.

```bash
# Emergency restoration from migration snapshot (restores 2026-04-18 state only).
# This is a last resort. Stop SeaweedFS first.
docker compose --profile dev-data stop minio

# Remove the corrupted volume.
docker volume rm "$(docker volume ls -q | grep minio_data)"

# Restart SeaweedFS (creates a fresh empty volume).
docker compose --profile dev-data up -d minio
# Wait for healthy, then:
docker compose up -d minio-init

# Re-upload the snapshot objects using mc or aws CLI pointed at the local snapshot:
# (This is a manual process — no scripted restore drill exists yet.)
```

---

## Measured timings (Phase C baselines, 2026-04-19)

| Store | Backup size | Backup wall time | Restore wall time | Baseline date |
|---|---|---|---|---|
| PostgreSQL | 174–183 MiB | ~20–30s | ~23s (throwaway) | 2026-04-19 |
| Qdrant (5 collections) | ~234 MiB total | ~29s | ~2.2s (single collection) | 2026-04-19 |
| Neo4j | ~51 MiB | ~75s (incl. stop/start) | <5 min (estimated) | 2026-04-19 (drill deferred to 2026-04-26) |
| WAL segments (first upload) | 80 MiB (5 segments) | ~5 min (first run) | N/A — used for PITR | 2026-04-19 |

---

## If the backup is missing

### PostgreSQL base backup missing

Basebackups older than 7 days are deleted automatically. If the most recent backup is missing entirely:

- **WAL archive buys you PITR** back to approximately 10 minutes before the loss (WAL uploads every 5 minutes, segments buffer locally). WAL coverage extends 8 days from the archive.
- Contact Kyle. A PostgreSQL specialist is needed for full WAL-only recovery if no base backup exists.
- The PG17 dumpall (`backups/pg17-dumpall-20260418-235253.sql`, 498 MiB) is the absolute fallback for the pre-migration state. Retain it until explicitly authorized for deletion.

### Qdrant backup missing

Qdrant has no WAL equivalent. If all snapshots for a collection are missing, the data since the last successful snapshot is unrecoverable. The collection must be rebuilt from source documents via the ingestion pipeline.

### Neo4j backup missing

Neo4j has no WAL equivalent in Community Edition. If the dump is missing, all graph data since the last successful Sunday dump is unrecoverable. The graph must be rebuilt from the ingestion pipeline.

### SeaweedFS backup missing

Raw document objects in `georag-bronze` (source PDFs, LAS files, etc.) are unrecoverable without a SeaweedFS backup. The migration snapshot covers 2026-04-18 state only.

---

## Known gotchas

**`pg_basebackup --wal-method=stream`:** The current backup script uses stream mode, making archives self-contained. Older archives created with `--wal-method=none` (pre-Phase C) require `pg_resetwal -f` before `pg_ctl start`. Check the archive timestamp to determine which mode was used — any artifact before 2026-04-19T19:00Z used `--wal-method=none`.

**`pg_hba.conf` is bind-mounted:** The replication ACL (`host replication all 172.19.0.0/16 scram-sha-256`) that allows `pg_basebackup` to connect lives in `docker/postgresql/pg_hba.conf`. Changes to replication rules belong in that file, not in the PostgreSQL data volume's `pg_hba.conf`. The `-c hba_file=/etc/postgresql/pg_hba.conf` flag in the PostgreSQL command ensures the bind-mounted file takes precedence.

**Neo4j offline dump requires container stop:** Community Edition does not support `STOP DATABASE` (Enterprise-only). The Ofelia-scheduled job stops the container, runs a one-shot dump container, and restarts. Total Neo4j downtime: ~75–120 seconds. Schedule maintenance windows accordingly.

**Qdrant snapshots are per-node:** Today the stack is single-node. If Qdrant is ever horizontally scaled, each node holds its own shards. Snapshots must be collected from each node separately. The current scripts do not handle multi-node topology.

**The backup-agent container runs `sleep infinity`:** `docker stop georag-backup-agent` will SIGKILL after the 30-second grace period because `sleep` does not handle SIGTERM. Any in-flight `docker exec` backup job (triggered by Ofelia) will also be killed. Plan stack stops to avoid overlapping with the 02:30–03:00 UTC backup window.

**AWS credentials must be set in `.env`:** `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `S3_ENDPOINT_URL` are passed from `.env` to the backup-agent container. If they are missing, all backup scripts fail at startup. Verify with:

```bash
docker exec georag-backup-agent env | grep -E 'AWS_|S3_ENDPOINT'
```

---

_Written 2026-04-19 during Module 1 Phase D. Update this file whenever the underlying procedure changes._
