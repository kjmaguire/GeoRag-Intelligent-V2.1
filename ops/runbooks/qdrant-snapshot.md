# Qdrant Snapshot Runbook
<!-- What: Per-collection snapshot commands, S3 upload, retention, restore to throwaway, and multi-node awareness -->
<!-- When: Nightly (02:00 UTC via Ofelia) for all collections; manually before any collection schema changes -->
<!-- Authority: 02-data-stores-hardening.md §6 Phase D; ops/baselines/2026-04-19-infra-baselines.md C3 -->
<!-- Produced by: devops-engineer agent (Claude Sonnet 4.6) -->
<!-- Date: 2026-04-20 (Module 2 Phase D) -->

---

## Snapshot Overview

Qdrant snapshots are created live — no downtime required. A snapshot captures the collection at a
consistent point in time and produces a single `.snapshot` file. Unlike Neo4j dump, Qdrant
snapshots are fully online operations.

**Deployment posture:** GeoRAG V1 runs Qdrant in single-node mode (`/cluster` returns
`{"status":"disabled"}`). All snapshot commands target a single node. Multi-node considerations
are documented below but do not apply to the current deployment.

**Scheduled backup:** Ofelia job-exec runs the Qdrant backup script nightly at 02:00 UTC.

---

## Live Collections and Sizes

As of 2026-04-20 baseline:

| Collection | Points | Snapshot size (Phase C) | Notes |
|------------|--------|------------------------|-------|
| `pg_drillhole_collar` | 33,490 | 117.4 MiB | Largest — use for timing benchmarks |
| `pg_mineral_occurrence` | 22,229 | ~34 MiB (estimated) | |
| `pg_mine` | 140 | small | |
| `pg_resource_potential_zone` | 82 | small | |
| `georag_reports` | 18 | 321 KiB | Grows significantly after Module 3 |

---

## Create a Snapshot Manually

```bash
# Trigger snapshot creation
COLLECTION="pg_drillhole_collar"
SNAPSHOT=$(curl -s -X POST "http://localhost:6333/collections/${COLLECTION}/snapshots" | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print(d['result']['name'])")
echo "Created: $SNAPSHOT"

# Download snapshot file
curl -s "http://localhost:6333/collections/${COLLECTION}/snapshots/${SNAPSHOT}" \
  -o "/tmp/${SNAPSHOT}"

# Upload to SeaweedFS
docker exec georag-backup-agent aws s3 cp "/tmp/${SNAPSHOT}" \
  "s3://georag-backups/qdrant/${COLLECTION}/${SNAPSHOT}" \
  --endpoint-url http://minio:8333
```

The backup script (`/backup-scripts/qdrant/backup.sh` inside `georag-backup-agent`) runs this for
all 5 collections automatically.

---

## List Existing Snapshots

```bash
# All snapshots for a collection
curl -s "http://localhost:6333/collections/pg_drillhole_collar/snapshots" | \
  python3 -c "import sys,json; d=json.load(sys.stdin); [print(s['name'], s['size']) for s in d['result']]"

# S3 inventory
docker exec georag-backup-agent aws s3 ls \
  s3://georag-backups/qdrant/ --recursive --endpoint-url http://minio:8333
```

---

## Retention

Default retention: 7 days. The backup script deletes S3 objects in `s3://georag-backups/qdrant/`
older than `QDRANT_BACKUP_RETENTION_DAYS` (default 7) after each successful backup run.

Local snapshots (inside Qdrant container) are not auto-deleted by the backup script — Qdrant stores
them in its internal snapshots directory. After confirming S3 upload success, the backup script
deletes the local snapshot via:
```bash
curl -X DELETE "http://qdrant:6333/collections/${COLLECTION}/snapshots/${SNAPSHOT_NAME}"
```

---

## Restore to Throwaway Container (Verification Pattern)

From `ops/baselines/2026-04-19-infra-baselines.md` C3 (Module 1 Phase C drill):

```bash
# 1. Create isolated network
docker network create throwaway-qdrant-net

# 2. Start throwaway Qdrant (different port to avoid conflict with live)
docker run -d --name test-qdrant-restore \
  --network throwaway-qdrant-net \
  -p 16333:6333 \
  qdrant/qdrant:v1.17.0

# 3. Download snapshot from S3 using a helper container with both network access
docker run -d --name test-qdrant-helper \
  --network throwaway-qdrant-net \
  -e AWS_ACCESS_KEY_ID="${AWS_ACCESS_KEY_ID}" \
  -e AWS_SECRET_ACCESS_KEY="${AWS_SECRET_ACCESS_KEY}" \
  --network georag \
  alpine:3.20 sleep 300

# 4. Download snapshot to helper
docker exec test-qdrant-helper aws s3 cp \
  "s3://georag-backups/qdrant/georag_reports/<snapshot-name>.snapshot" \
  /tmp/restore.snapshot \
  --endpoint-url http://minio:8333

# 5. Upload snapshot to throwaway Qdrant
docker exec test-qdrant-helper curl -X POST \
  "http://test-qdrant-restore:6333/collections/georag_reports/snapshots/upload?wait=true" \
  -H "Content-Type: multipart/form-data" \
  -F "snapshot=@/tmp/restore.snapshot"

# 6. Verify
curl -s "http://localhost:16333/collections/georag_reports" | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print('points:', d['result']['points_count'])"

# 7. Cleanup
docker rm -f test-qdrant-restore test-qdrant-helper
docker network rm throwaway-qdrant-net
```

Module 1 Phase C restore drill result: `georag_reports` (18 points, 321 KiB snapshot) — total
time from S3 download to verified: ~2.2 seconds.

---

## Restore to Live Collection (Production Recovery)

Only use this procedure after a confirmed data loss event. This **overwrites the live collection**.

```bash
# 1. Download snapshot from S3 to a local path accessible by the backup-agent
docker exec georag-backup-agent aws s3 cp \
  "s3://georag-backups/qdrant/${COLLECTION}/<snapshot-name>.snapshot" \
  /tmp/restore.snapshot \
  --endpoint-url http://minio:8333

# 2. Upload snapshot to live Qdrant (this recreates the collection)
docker exec georag-backup-agent curl -X PUT \
  "http://qdrant:6333/collections/${COLLECTION}/snapshots/recover" \
  -H "Content-Type: application/json" \
  -d "{\"location\": \"file:///tmp/restore.snapshot\"}"

# 3. Verify point count
curl -s "http://localhost:6333/collections/${COLLECTION}" | \
  python3 -c "import sys,json; d=json.load(sys.stdin); print('points:', d['result']['points_count'])"
```

---

## Multi-Node Awareness (Future)

GeoRAG V1 runs single-node Qdrant. If cluster mode is introduced:

- Snapshots are **per-shard** in cluster mode — each shard must be snapshot independently.
- `POST /collections/{name}/snapshots` in cluster mode creates shard-level snapshots on each node.
- A full collection backup requires snapshots from all nodes in the shard map.
- Restore requires uploading the shard snapshot to the same shard (determined by the peer_id).
- The Qdrant documentation section "Distributed deployment / Snapshots" covers the multi-shard
  procedure. Do not use the single-node upload endpoint for cluster restores.

Current posture: document the cluster caveat here; do not implement until cluster mode is enabled.

---

## Provenance

- Date: 2026-04-20
- Module: 2 Phase D
- Produced by: devops-engineer agent (Claude Sonnet 4.6)
- Restore drill source: ops/baselines/2026-04-19-infra-baselines.md C3
- Authority: 02-data-stores-hardening.md §6 Phase D
