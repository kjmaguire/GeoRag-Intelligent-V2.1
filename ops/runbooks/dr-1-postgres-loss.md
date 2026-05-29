# DR Runbook 1 — Postgres data loss (§26.5 scenario 1)

**Status:** Production-shape (doc-phase 185). Upgraded from the
doc-phase 104 skeleton by Phase H3.

This runbook drives recovery when the primary PostgreSQL instance
loses data (corrupt WAL, partial replication failure, dropped table
by mistake, etc.). The §G.2 `restore_workspace` Hatchet workflow is
the cross-store consistency engine; this runbook drives the
single-store Postgres restoration that feeds into it.

---

## Scope

- **In scope:** PostgreSQL (silver, gold, audit, public_geoscience,
  targeting, eval, ops, workflow schemas).
- **Out of scope:** Neo4j / Qdrant / Redis / SeaweedFS losses
  (separate runbooks: dr-2 through dr-5).

## Detection signals

| Signal | Source | Action |
|---|---|---|
| `pg_replication_lag_seconds > 300` (5 min) | Prometheus alert | Triage replica health |
| Application logs: "relation does not exist", FK violations | Loki / `docker compose logs fastapi` | Confirm schema vs migration head |
| `audit.verify_hash_chain(window_start, window_end)` returns FALSE | DBA shell | Hash break = tampering OR corruption — escalate to dr-3 if intentional |
| Workspace data_version reading lower than yesterday | `SELECT workspace_id, data_version FROM silver.workspaces` | Indicates regression of data_version counter |
| `bench dump_size_mb` falls > 20% day-over-day | Ops cron | Possible row deletion |

## RTO / RPO targets

| Tier | RTO | RPO |
|---|---|---|
| Detection → read-only mode | **15 min** | n/a |
| Hot replica promote | **30 min** | **≤ 30 sec** streaming replication |
| Full WAL replay from cold backup | **4 hr** | **≤ 1 hr** WAL archival cadence |
| Full base backup restore | **8 hr** | **≤ 24 hr** daily basebackup |

Targets pending Kyle final sign-off — current defaults match §28.

---

## Procedure

### Phase A — Triage (target: 15 minutes)

1. **Confirm scope.** From a read replica:
   ```bash
   docker compose exec -T postgresql psql -U "$POSTGRES_USER" \
     -d "$POSTGRES_DB" -c \
     "SELECT relname, n_live_tup FROM pg_stat_user_tables
      WHERE schemaname IN ('silver','gold','audit','workflow','ops')
      ORDER BY relname;"
   ```
   Cross-reference row counts against last night's `pg_dump --schema-only`
   manifest stored in `seaweedfs://georag-backups/postgres/`.

2. **Engage read-only mode.** Block ingestion + sign-offs without
   breaking chat retrieval:
   ```bash
   docker compose exec -T fastapi python -c "
   import asyncio, os, redis.asyncio as redis
   async def main():
       r = redis.from_url(f'redis://:{os.environ[\"REDIS_PASSWORD\"]}@redis:6379/0')
       await r.set('georag:flags:read_only_mode', '1', ex=86400)
   asyncio.run(main())"
   ```
   The orchestrator + Hatchet workers consult this flag before any
   write path (per §11.2 contract — wired in doc-phase 134).

3. **Open incident.** Insert into `ops.support_tickets`:
   ```sql
   INSERT INTO ops.support_tickets
       (ticket_id, workspace_id, severity, category, status,
        description, reported_by_user_id, reported_at)
   VALUES
       (gen_random_uuid(), $WORKSPACE_UUID, 'critical',
        'data_loss', 'open',
        'DR-1 Postgres data loss — see runbook',
        $OPERATOR_USER_ID, NOW())
   RETURNING ticket_id;
   ```
   Then fire `support_packet` via the cockpit `/admin/support-cockpit`
   to assemble the diagnostic bundle. If `KESTRA_URL` is set the
   bundle also fans out to Slack / SeaweedFS archive automatically.

4. **Stop Dagster + Hatchet workers.** Prevents new writes during
   restoration:
   ```bash
   docker compose stop hatchet-worker-ai hatchet-worker-ingestion dagster
   ```

### Phase B — Restore (target: 3 hours)

**B1. Identify the restoration target.**

Pick the most recent clean snapshot before the corruption timestamp:
```bash
docker compose exec -T fastapi python -c "
import asyncio, boto3, os
s3 = boto3.client('s3',
    endpoint_url=os.environ['SEAWEEDFS_ENDPOINT_URL'],
    aws_access_key_id=os.environ['SEAWEEDFS_ACCESS_KEY'],
    aws_secret_access_key=os.environ['SEAWEEDFS_SECRET_KEY'])
resp = s3.list_objects_v2(Bucket='georag-backups', Prefix='postgres/basebackup/')
print('\n'.join(sorted(o['Key'] for o in resp.get('Contents', []) if o['LastModified'])))"
```

**B2. Provision restore target.**

Spin up a fresh postgres container alongside the primary:
```bash
docker compose -f docker-compose.yml -f docker/compose.restore.yml \
    up -d postgresql-restore
```
The compose overlay maps `/var/lib/postgresql/data` to a fresh volume.

**B3. Replay WAL up to a moment before corruption.**

```bash
docker compose exec -T postgresql-restore bash -c "
  set -euo pipefail
  cat > /tmp/recovery.conf <<EOF
restore_command = 'aws s3 cp s3://georag-backups/postgres/wal/%f %p'
recovery_target_time = '\${CORRUPTION_TIMESTAMP_UTC}'
recovery_target_inclusive = false
EOF
  pg_ctl stop -D \$PGDATA -m fast
  cp /tmp/recovery.conf \$PGDATA/
  pg_ctl start -D \$PGDATA"
```

`CORRUPTION_TIMESTAMP_UTC` is the latest hash-chain-verified
timestamp from Phase A. Subtract 60 seconds for safety margin.

**B4. Sanity checks on the restored instance.**

```sql
-- Hash chain continuous from prior head
SELECT audit.verify_hash_chain(NULL, NULL);
-- Row counts vs replica
SELECT relname, n_live_tup FROM pg_stat_user_tables
  WHERE schemaname IN ('silver','gold','audit')
  ORDER BY relname;
-- Workspace data_versions look sane (not regressed)
SELECT workspace_id, data_version, updated_at FROM silver.workspaces
  ORDER BY updated_at DESC LIMIT 10;
```

Compare each row count to the pre-restore replica. > 1% drop on any
table is a red flag.

### Phase C — Cross-store reconciliation (target: 1 hour)

Once Postgres is restored, the §G.2 `restore_workspace` Hatchet
workflow drives the other 4 stores back into agreement.

**For EACH affected workspace_id:**

```bash
docker compose exec -T fastapi python -c "
import asyncio, os
from uuid import UUID, uuid4
from app.hatchet_workflows.restore_workspace import (
    restore_workspace_execute, RestoreWorkspaceInput,
)

async def main():
    inp = RestoreWorkspaceInput(
        workspace_id=UUID('$WORKSPACE_UUID'),
        snapshot_manifest_uri='s3://georag-backups/manifests/\${MANIFEST}.json',
        initiated_by_user_id=$OPERATOR_USER_ID,
        restore_request_id=uuid4(),
        dry_run=True,
    )
    out = await restore_workspace_execute.aio_mock_run(inp)
    print(out.model_dump_json(indent=2))
asyncio.run(main())"
```

Inspect the dry-run output. If `live_counts` matches expected per-store
counts, re-run with `dry_run=False` (currently an explicit gate —
that wall comes down when Kyle approves the production-write path).

**Verification:**

```sql
-- Cross-store FK integrity check (G.2 contract)
SELECT consistency_check_results FROM audit.audit_ledger
  WHERE action_type = 'workspace_restore'
    AND target_id = '$WORKSPACE_UUID'
  ORDER BY created_at DESC LIMIT 1;
```

### Phase D — Cutover (target: 30 minutes)

1. **Stop application stack.**
   ```bash
   docker compose stop fastapi laravel-octane caddy
   ```

2. **DNS / connection-string flip.** Update `.env`:
   ```
   POSTGRES_DIRECT_HOST=postgresql-restore
   ```

3. **Restart stack.**
   ```bash
   docker compose up -d fastapi laravel-octane caddy
   ```

4. **Drop read-only flag.**
   ```bash
   docker compose exec -T fastapi python -c "
   import asyncio, os, redis.asyncio as redis
   async def main():
       r = redis.from_url(f'redis://:{os.environ[\"REDIS_PASSWORD\"]}@redis:6379/0')
       await r.delete('georag:flags:read_only_mode')
   asyncio.run(main())"
   ```

5. **Verify normal traffic flow.**
   ```bash
   docker compose exec -T fastapi python tmp/f5c_golden_eval_runner.py
   ```
   Expected: 22/22. Anything less = a downstream regression caught by
   the eval; pause + investigate before declaring the incident closed.

6. **Restart Dagster + Hatchet workers** (resume ingestion):
   ```bash
   docker compose start hatchet-worker-ai hatchet-worker-ingestion dagster
   ```

7. **Verify audit hash-chain continues cleanly:**
   ```sql
   SELECT audit.verify_hash_chain(NOW() - INTERVAL '1 hour', NOW());
   ```

---

## Post-mortem

After restoration completes:

1. Update the incident ticket: `status='resolved'`, `resolved_at=NOW()`.
2. Record a `decision_lessons_learned` row linked to the ticket via
   `record_decision()` (§9.10 / §21).
3. Fire `field_outcome_learning` against the incident's
   target_recommendations if drilling was paused. (This rare path
   uses the same workflow as §G.1 outcome ingestion.)
4. Run `pg_basebackup` + WAL-archive snapshot of the restored
   instance so the next DR drill has a known-good starting point.

## Open questions for Kyle

1. **Promote policy:** when streaming replica is the cutover target,
   automatic promote (pgpool / repmgr) or operator-gated? Default
   today: operator-gated.
2. **WAL retention window:** 24h / 72h / 7d? Larger window = larger
   SeaweedFS cost; smaller window = tighter RPO bound on cold-backup
   restoration. Default proposal: 72h.
3. **Per-store reconciliation order:** G.2 currently goes
   PG → Neo4j → Qdrant → Redis → SeaweedFS. Confirm this matches
   §11.2's intended dependency graph.
