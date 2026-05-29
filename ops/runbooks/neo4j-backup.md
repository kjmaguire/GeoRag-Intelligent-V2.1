# Neo4j Backup Runbook
<!-- What: Offline dump procedure for Neo4j Community Edition 2026.03.1 — the ONLY available backup method -->
<!-- When: Weekly (Sunday 03:00 UTC via Ofelia); also manually before any schema migrations -->
<!-- Authority: 02-data-stores-hardening.md §6 Phase D; ops/audit/2026-04-19-datastores-audit.md N4J-05 -->
<!-- Produced by: devops-engineer agent (Claude Sonnet 4.6) -->
<!-- Date: 2026-04-20 (Module 2 Phase D) -->

---

## Critical: Online Backup Is Not Available

`neo4j-admin database backup` is **Enterprise Edition only**. On Community Edition 2026.03.1, the
`backup` subcommand does not exist in `neo4j-admin database`. Only `dump` is available.

`neo4j-admin database dump` requires the target database to be **stopped or in the maintenance
state** — it cannot run against a live, read-write database. This means Neo4j must be taken offline
for the duration of the backup.

This finding was confirmed in `ops/audit/2026-04-19-datastores-audit.md` (N4J-05). The Module 1
Phase B backup script's detection logic (`backup --help` exit 0) was a false positive.

**Expected downtime per backup:** approximately 30–60 seconds for the current graph size (~333 MiB
store, 56K nodes). Ofelia schedules the backup at Sunday 03:00 UTC to minimize impact.

---

## What the Backup Script Does

Script location (inside `georag-backup-agent`): `/backup-scripts/neo4j/backup.sh`

Steps:
1. **Stop Neo4j**: `docker stop georag-neo4j` via Docker socket
2. **Run dump**: `docker run --rm --volumes-from georag-neo4j neo4j:2026-community neo4j-admin database dump neo4j --to-path=/dumps/`
3. **Compress**: gzip the dump file
4. **Upload to S3**: `aws s3 cp` to `s3://georag-backups/neo4j/`
5. **Start Neo4j**: `docker start georag-neo4j`
6. **Wait for healthy**: poll `/db/data/` until Neo4j reports ready (up to 120s)
7. **Start warmup**: trigger `docker start georag-neo4j-warmup` to re-run warmup Cypher

Retention: 7 days. The script deletes S3 objects older than `NEO4J_BACKUP_RETENTION_DAYS` (default 7).

---

## Manual Invocation

```bash
# DRY_RUN mode (prints intent without executing):
docker exec georag-backup-agent sh -c 'DRY_RUN=1 bash /backup-scripts/neo4j/backup.sh'

# Live run (Neo4j goes offline briefly):
docker exec georag-backup-agent bash /backup-scripts/neo4j/backup.sh
```

Required environment variables (set in `docker-compose.yml` for `georag-backup-agent`):
- `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `S3_ENDPOINT_URL` — SeaweedFS credentials
- `NEO4J_CONTAINER_NAME` — defaults to `georag-neo4j`
- `NEO4J_AUTH` — `neo4j/<password>` for post-dump health verification

---

## Weekly Schedule (Ofelia)

Configured in `docker-compose.yml` as an Ofelia `job-exec` label on `georag-backup-agent`:

```yaml
labels:
  ofelia.job-exec.neo4j-backup.schedule: "0 3 * * 0"   # Sunday 03:00 UTC
  ofelia.job-exec.neo4j-backup.command: "bash /backup-scripts/neo4j/backup.sh"
  ofelia.job-exec.neo4j-backup.container: "georag-backup-agent"
```

Verify Ofelia schedule is registered:
```bash
docker logs georag-ofelia 2>&1 | grep neo4j
```

---

## Restore Procedure

For the full restore procedure including throwaway container pattern, see:
`ops/runbooks/backup-restore.md` (Module 1 Phase D) — Neo4j section.

Summary of restore steps:
1. Stop the live `georag-neo4j` container
2. Download the dump from `s3://georag-backups/neo4j/` to a local path
3. Run `neo4j-admin database load` from the dump file against the stopped instance
4. Start `georag-neo4j` and wait for healthy
5. Run the warmup script manually: `docker start georag-neo4j-warmup`

**Throwaway restore** (for verification without touching live):
```bash
# Start a throwaway Neo4j with the same volume type
docker run -d --name test-neo4j-restore \
  -e NEO4J_AUTH=neo4j/testpassword \
  -p 17474:7474 -p 17687:7687 \
  neo4j:2026-community

# Load dump into it
docker cp /path/to/neo4j-dump.dump test-neo4j-restore:/tmp/
docker exec test-neo4j-restore neo4j-admin database load neo4j \
  --from-path=/tmp/ --overwrite-destination

# Verify
docker exec test-neo4j-restore cypher-shell -u neo4j -p testpassword \
  "MATCH (n) RETURN count(n) AS total;"

# Cleanup
docker rm -f test-neo4j-restore
```

---

## Recovery: Dump Landed Mid-Transaction

This scenario does not apply. `neo4j-admin database dump` requires the database to be offline
before it starts. There is no "mid-transaction" risk — the dump is always a consistent snapshot
of a stopped database. If Neo4j was forcibly killed mid-write before the backup window, the
transaction log ensures recovery consistency on the next start (Neo4j replays uncommitted
transactions on startup). The dump captures post-recovery state.

---

## Neo4j Heap Restart Note

`NEO4J_server_memory_heap_initial__size=4G` has been set in `docker-compose.yml` but the JVM
heap change requires a full Neo4j restart to take effect. The live heap was previously at initial=2G,
max=4G (Phase A finding N4J-02). The next scheduled restart window (or the weekly backup window,
which takes Neo4j offline anyway) is the correct time to verify the heap is at 4G initial.

Verify after restart:
```bash
docker exec georag-neo4j bash -c "ps aux | grep java | grep -oP '\-Xms\K\S+'"
# or via cypher:
docker exec georag-neo4j cypher-shell -u neo4j -p "$NEO4J_PASS" \
  "CALL dbms.listConfig() YIELD name, value WHERE name CONTAINS 'heap' RETURN name, value;"
```

---

## Provenance

- Date: 2026-04-20
- Module: 2 Phase D
- Produced by: devops-engineer agent (Claude Sonnet 4.6)
- Finding source: ops/audit/2026-04-19-datastores-audit.md N4J-05
- Related: ops/runbooks/backup-restore.md (Module 1 Phase D)
