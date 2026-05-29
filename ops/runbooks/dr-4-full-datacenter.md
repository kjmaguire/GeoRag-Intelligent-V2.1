# DR Runbook 4 — Full datacenter / hosting region loss (§26.5 scenario 4)

**Status:** Production-shape (doc-phase 185). Upgraded from the
doc-phase 104 skeleton by Phase H3.

Recovery when the primary deployment region is unreachable
(cloud-region outage, datacenter fire, network partition long enough
to declare the region lost). The recovery target is a DR replica
region that's been kept warm via streaming replication + immutable
SeaweedFS snapshots.

---

## Scope

- **In scope:** Full primary-region unavailability across all
  GeoRAG components (Postgres, Neo4j, Qdrant, Redis, SeaweedFS,
  Hatchet, Kestra, Dagster, Caddy ingress).
- **Out of scope:** Partial outages (dr-5), single-store failures
  (dr-1, dr-2), adversarial compromise (dr-3).

## Detection signals

| Signal | Source | Action |
|---|---|---|
| All Prometheus targets down for > 5 min | Prometheus alert manager | Confirm via off-cluster check (Pingdom / UptimeRobot) |
| Caddy returns 502 from external network probe | External monitoring | Region-level outage candidate |
| Cloud provider status page acknowledges region issue | Provider status | Confirms scope |
| Kestra + Hatchet workers all unreachable from operator host | `docker compose ps` returns empty | Region is gone |

## RTO / RPO

| Tier | RTO | RPO |
|---|---|---|
| Detection → confirm region-level | **5 min** | n/a |
| DNS cutover to DR region | **10 min** | n/a |
| Postgres replica promote | **30 min** | **≤ 1 min** streaming replication |
| Full app + workers warm in DR region | **2 hr** | n/a |
| Resume ingestion + Hatchet workflows | **+1 hr** beyond full-warm | n/a |

Effective RPO is **≤ 1 min** when streaming replication is healthy.
Falls back to **≤ 1 hr** if streaming was lagging (which itself
should have triggered a `pg_replication_lag` alert per dr-1).

---

## Procedure

### Phase A — Confirm region loss (target: 5 minutes)

1. **Confirm via two external sources** that the region is genuinely
   unreachable (not just our monitoring host's network):
   - `curl -m 5 https://status.<provider>.com` — provider status page
   - Off-cluster ping / TCP probe on the region's public endpoint
   - Operator-team DM check ("anyone else unable to reach the
     primary region?")

2. **Notify customers immediately.** A `region_outage` audit ledger
   row goes in via the DR region's separate audit chain — this
   chain is independent of the primary's:
   ```sql
   -- Connected to DR region postgres
   INSERT INTO audit.audit_ledger
       (id, action_type, actor_kind, target_schema, target_table,
        target_id, payload)
   VALUES (gen_random_uuid(), 'region.outage_declared', 'operator',
           'ops', 'incidents', gen_random_uuid()::text,
           jsonb_build_object('primary_region', '<region>',
                              'declared_at', NOW()));
   ```
   The status-page automation reads this signal + posts publicly.

3. **Open incident ticket** in the DR region's `ops.support_tickets`
   (the primary region's is unreachable). Tag `severity='critical'`.

### Phase B — DNS cutover (target: 10 minutes)

The cutover is DNS-level for V1 (no anycast yet — that's §11.6
hardening when SaaS lands).

1. **Update DNS records.** GeoRAG owns:
   - `app.georag.example` (primary cockpit URL)
   - `api.georag.example` (FastAPI public surface)
   - `tile.georag.example` (Martin tile server)

   Set each `A` record to the DR region's load-balancer IP. TTL is
   60s in primary config, so the global flip completes in ≤ 5 min.

2. **Verify the cutover.** From an external host:
   ```bash
   dig +short app.georag.example
   curl -m 5 https://app.georag.example/api/health
   ```
   Expected: returns the DR region's load-balancer IP + a 200 from
   `/api/health`.

### Phase C — Postgres replica promote (target: 30 minutes)

The DR region's PostgreSQL is a streaming-replica slave. Promote
it to primary:

1. **Pause replication apply** on the slave so we know its position:
   ```bash
   docker compose -p georag-dr exec -T postgresql psql -U \
       $POSTGRES_USER -d $POSTGRES_DB -c "SELECT pg_wal_replay_pause();"
   ```

2. **Promote** the slave:
   ```bash
   docker compose -p georag-dr exec -T postgresql pg_ctl promote \
       -D /var/lib/postgresql/data
   ```

3. **Verify the new primary** is taking writes:
   ```sql
   SELECT pg_is_in_recovery();  -- expect FALSE
   SELECT pg_current_wal_lsn();  -- expect a fresh LSN
   ```

4. **Re-pin replication source** for any further replicas (if you've
   provisioned a 3rd region as DR-of-DR; otherwise skip).

### Phase D — Warm the application stack in DR region (target: 1.5 hours)

The DR region keeps Postgres warm but the app stack (FastAPI,
Laravel, workers, Caddy) runs at minimal capacity. Scale up:

1. **Bring the stack to production size:**
   ```bash
   docker compose -p georag-dr -f docker-compose.yml \
       -f docker/compose.dr-region.yml up -d --scale fastapi=3 \
                                              --scale hatchet-worker-ai=2 \
                                              --scale hatchet-worker-ingestion=2
   ```

2. **Wait for healthchecks** to clear. Cold-start takes ~50s for
   vLLM (model load) + ~10s for FastAPI + ~5s for everything else.
   Total: ~2 min to all-healthy.

3. **Run the eval pack** as the smoke test:
   ```bash
   docker compose -p georag-dr exec -T fastapi python tmp/f5c_golden_eval_runner.py
   ```
   Expected: 22/22. Anything less = the DR region is behind on
   some asset materialisation.

4. **Verify cross-store consistency** for the active workspaces:
   ```bash
   docker compose -p georag-dr exec -T fastapi python -c "
   import asyncio, os
   from uuid import UUID, uuid4
   from app.hatchet_workflows.restore_workspace import (
       restore_workspace_execute, RestoreWorkspaceInput,
   )
   async def main():
       for ws_id in ['<workspace-1>', '<workspace-2>']:  # populate from ops table
           out = await restore_workspace_execute.aio_mock_run(
               RestoreWorkspaceInput(
                   workspace_id=UUID(ws_id),
                   snapshot_manifest_uri='s3://georag-backups-dr/manifests/latest.json',
                   initiated_by_user_id=$OPERATOR_USER_ID,
                   restore_request_id=uuid4(),
                   dry_run=True,
               ),
           )
           print(f'ws={ws_id} success={out.success} '
                 f'pg_rows={out.consistency_check_results[\"live_counts\"][\"postgres\"]}')
   asyncio.run(main())"
   ```

### Phase E — Re-establish streaming replication when primary returns (target: +4 hours after primary recovery)

When the primary region comes back online (could be hours or days
later):

1. **Do NOT auto-failback.** The DR region is now authoritative; the
   primary region's data is at best stale, at worst corrupted by
   whatever caused the outage. Treat the primary as a NEW replica.

2. **Drain writes** from the primary region's data:
   ```bash
   docker compose -p georag-primary exec -T postgresql pg_ctl stop -m smart
   ```

3. **Take a basebackup of the now-authoritative DR region:**
   ```bash
   docker compose -p georag-dr exec -T postgresql pg_basebackup \
       -U $POSTGRES_USER \
       -D /var/lib/postgresql/dr-baseline \
       -Fp -Pv -X stream
   ```

4. **Re-image the primary region's postgres** with this basebackup,
   then start it in replica mode pointed at the DR region.

5. **Allow streaming replication to catch up** (LSN gap close).
   Typically 1-4 hours depending on the write volume during the
   outage.

6. **Decide whether to fail back.** Failback is a coordinated event;
   reverse the dr-4 procedure with primary↔DR roles swapped. Not
   urgent — leaving DR as the primary is fine indefinitely.

---

## Post-mortem

Standard incident-response post-mortem. Specific to this scenario:
- **MTTR breakdown** by phase (A/B/C/D individually)
- **Customer-visible degradation window** — should be ≤ 10 min
  (Phase A + B). Anything beyond that means the DNS TTL or
  load-balancer config didn't propagate fast enough.
- **Data loss accounting** — any writes accepted by the primary AFTER
  streaming replication paused, before promote? List by audit row.

## Open questions for Kyle

1. **Number of DR regions.** Today: 1. Adding a 3rd region buys
   shorter-RPO failover but doubles ops complexity. Default
   proposal: stay at 1 until the SaaS topology lands.
2. **Failback default** — automatic when LSN gap < N hours, or
   manual always? Default proposal: manual always (low frequency
   event; humans should authorise).
3. **DR region steady-state size.** Today: 1 fastapi + 1
   hatchet-worker-ai (just enough to keep credentials hot). Larger
   warm pool = faster recovery, more idle cost.
