# DR Runbook 2 — Cross-store divergence (§26.5 scenario 2)

**Status:** Production-shape (doc-phase 185). Upgraded from the
doc-phase 104 skeleton by Phase H3.

Recovery procedure when **Postgres is intact** but Neo4j / Qdrant /
SeaweedFS / Redis have drifted. This is the most common DR scenario:
an outbox dispatcher stalls, a Qdrant collection is dropped by mistake,
Neo4j replay falls behind during a Dagster materialisation storm,
etc. Postgres remains the source of truth; the runbook drives
re-projection of the downstream stores.

---

## Scope

- **In scope:** Neo4j, Qdrant, SeaweedFS, Redis when Postgres is
  authoritative.
- **Out of scope:** Postgres-side losses (see dr-1).

## Detection signals

| Signal | Source | Action |
|---|---|---|
| `outbox_pending_rows > 1000` sustained | §16.2 Workflows-Outbox dashboard | Identify topic; usually a consumer is offline |
| `outbox.propagation_attempts.dead_letter_count` spikes | DBA query / dashboard | Triage dead-letter envelopes for root cause |
| Application errors: "chunk not found in Qdrant" | Loki / Sentry | Qdrant collection inconsistent vs silver chunks |
| Application errors: "edge target not in Postgres" | Loki | Neo4j carries stale node IDs vs silver |
| `bench validate_cross_store` returns non-zero diffs | Ops cron | Multi-store FK diff detected |
| Citation freshness alert: `data_version` mismatch silver↔Qdrant | Prometheus | Index drift |
| Redis cache miss rate spike (drop from ~70% to < 10%) | §16.2 LLM pipeline dashboard | Redis flush / eviction storm |

## RTO / RPO

| Tier | RTO | RPO |
|---|---|---|
| Detection → degraded mode | **5 min** | n/a (Postgres unchanged) |
| Single-store rebuild (Qdrant or Neo4j) | **2 hr** | **0** — re-projection from Postgres source-of-truth |
| All four stores rebuild | **6 hr** | **0** |

Effective RPO is **0** because Postgres is the canonical source. The
loss is downstream-only.

---

## Procedure

### Phase A — Triage (target: 10 minutes)

1. **Identify which store(s) have drifted.** From the cockpit /
   FastAPI:
   ```bash
   docker compose exec -T fastapi python -c "
   import asyncio, os
   from uuid import UUID, uuid4
   from app.hatchet_workflows.restore_workspace import (
       restore_workspace_execute, RestoreWorkspaceInput,
   )
   async def main():
       out = await restore_workspace_execute.aio_mock_run(
           RestoreWorkspaceInput(
               workspace_id=UUID('$WORKSPACE_UUID'),
               snapshot_manifest_uri='s3://georag-backups/manifests/latest.json',
               initiated_by_user_id=$OPERATOR_USER_ID,
               restore_request_id=uuid4(),
               dry_run=True,
           ),
       )
       print(out.model_dump_json(indent=2))
   asyncio.run(main())"
   ```
   The `consistency_check_results.live_counts` block names the
   divergent store(s). E.g., `qdrant_points` < expected indicates
   Qdrant collection is missing rows.

2. **Engage degraded mode** for read-heavy paths affected by the
   drift:
   - **Qdrant down/inconsistent** → set
     `RETRIEVAL_QDRANT_BYPASS=true` so the orchestrator falls back to
     PostGIS-only retrieval (citations still grounded, recall drops).
   - **Neo4j down/inconsistent** → set
     `RETRIEVAL_GRAPH_BYPASS=true` so the graph branch returns empty
     rather than stale data. Partial-source fallback in the cache
     handles it correctly.
   - **SeaweedFS down** → the renderer falls back to inline data URIs
     automatically; no flag needed.
   - **Redis stale** → just flush:
     ```bash
     docker compose exec -T fastapi python -c "
     import asyncio, os, redis.asyncio as redis
     async def main():
         r = redis.from_url(f'redis://:{os.environ[\"REDIS_PASSWORD\"]}@redis:6379/0')
         async for k in r.scan_iter('georag:rag_cache:*', count=200):
             await r.delete(k)
     asyncio.run(main())"
     ```

### Phase B — Targeted rebuild (target: 1-3 hours per store)

#### B-Qdrant — re-embed chunks from silver

The §10.7 `index_chunks` Dagster asset is the canonical rebuilder.
Trigger a full backfill:
```bash
docker compose exec -T dagster dagster asset materialize \
    --select index_chunks \
    --partition "$WORKSPACE_ID"
```
Throughput target: ~500 chunks/min on the dev workstation.
For ~50k-chunk workspaces, expect ~100 min.

#### B-Neo4j — replay graph from silver via outbox

The graph projection lives in
`app/hatchet_workflows/index_neo4j.py`. Trigger it via Hatchet:
```bash
docker compose exec -T fastapi python -c "
import asyncio
from uuid import UUID
from hatchet_sdk import new_client_from_env
async def main():
    client = new_client_from_env()
    handle = await client.aio.workflows.run(
        'index_neo4j',
        {'workspace_id': '$WORKSPACE_UUID', 'mode': 'full_rebuild'},
    )
    print(f'execution_id={handle.workflow_run_id}')
asyncio.run(main())"
```
The workflow drops the workspace's graph slice + re-inserts from
silver. Idempotent.

#### B-SeaweedFS — re-fetch from bronze + recompute

Bronze documents are immutable in `bronze:` URIs. Re-derived
silver/gold blobs (e.g., parsed-text artifacts) come from re-running
the §04p parser stack:
```bash
docker compose exec -T dagster dagster asset materialize \
    --select silver_parser_run_artifacts \
    --partition "$WORKSPACE_ID"
```

#### B-Redis — natural warmth

Cache. No rebuild needed — let traffic warm it. The first-query
penalty after a flush is small (~1.5s extra per identical-query
group).

### Phase C — Verification (target: 30 minutes)

1. **Re-run `restore_workspace` dry-run.** Confirm `live_counts`
   matches expected counts per store.
2. **Drop degraded mode flags:**
   ```bash
   docker compose exec -T fastapi python -c "
   import asyncio, os, redis.asyncio as redis
   async def main():
       r = redis.from_url(f'redis://:{os.environ[\"REDIS_PASSWORD\"]}@redis:6379/0')
       for flag in (
           'georag:flags:retrieval_qdrant_bypass',
           'georag:flags:retrieval_graph_bypass',
       ):
           await r.delete(flag)
   asyncio.run(main())"
   ```
3. **Drain outbox dead-letter queue:**
   ```sql
   UPDATE outbox.dead_letter_queue
      SET status = 'retired', retired_at = NOW(),
          retired_reason = 'manual cleanup post-dr-2 incident <ticket-id>'
    WHERE topic = '<affected_topic>'
      AND created_at < (SELECT MAX(occurred_at) FROM audit.audit_ledger
                          WHERE action_type = 'workspace_restore'
                            AND target_id = '$WORKSPACE_UUID');
   ```
4. **Run the eval pack** to confirm orchestrator retrieves cleanly:
   ```bash
   docker compose exec -T fastapi python tmp/f5c_golden_eval_runner.py
   ```
   Expected: 22/22. Anything less = the rebuild produced a drift
   that the eval surfaces.

## Post-mortem

Standard: ticket close + `decision_lessons_learned`. If outbox
dead-letters were involved, add a finding under
`silver.audit_findings` so the next operator triaging the dashboard
sees what caused it.

## Open questions for Kyle

1. **Degraded-mode acceptable duration.** Qdrant-bypass = BM25-only
   retrieval; precision degrades ~15-25% per §10.6 measurement.
   Default ceiling proposal: 4 hours before declaring an incident
   "service-degraded" per §11.5 SaaS SLA.
2. **Re-embed throughput target.** Single-tenant L40S workstation:
   ~500 chunks/min today. Per-tenant SaaS: parallelise across
   workspace; ~5000 chunks/min/host realistic.
3. **Cache warmth-up SLO.** After a Redis flush, how long until cache
   hit rate returns to baseline 70%? Empirically ~30 min on
   moderate traffic.
