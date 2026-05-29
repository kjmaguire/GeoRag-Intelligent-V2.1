# DR Runbook 5 — Partial-outage degraded operation (§26.5 scenario 5)

**Status:** Production-shape (doc-phase 185). Upgraded from the
doc-phase 104 skeleton by Phase H3.

The "day-in-the-life" partial-degradation runbook. Single component
is unhealthy (vLLM crashed, Qdrant is OOMing, Neo4j slow due to
disk pressure, Hatchet AI pool stuck) but the rest of the system
is fine. Goal: keep the unaffected paths serving while we fix the
affected one — no full DR drill needed.

The Phase G overnight + Phase H work means most components ALREADY
have graceful degradation paths wired:
- LLM failover (Anthropic → vLLM, vLLM → Ollama)
- Tool partial-failure rescue (per-store timeout in
  `parallel_branches`)
- Cache rehydration + partial-source fallback
- §10 support cockpit + dispatchers

This runbook tells you which knob to turn for each common failure
mode.

---

## Scope

- **In scope:** Any SINGLE-component outage where the rest of the
  stack is healthy. Examples: vLLM down, Neo4j slow, Qdrant
  collection corrupted, Hatchet AI pool stuck, Kestra unhealthy.
- **Out of scope:** Multi-component cascade (escalate to dr-2);
  full region loss (dr-4); adversarial compromise (dr-3).

## Decision tree — which component is unhealthy?

| If you see... | Then... | Procedure |
|---|---|---|
| `httpx.HTTPStatusError: 502/503` from vLLM | LLM call path | **§A — LLM failover** |
| `httpx.ConnectError: Name or service not known: vllm` | vLLM container is gone | **§A — LLM failover** |
| Slow Neo4j queries / `ServiceUnavailable` in tool_results | Neo4j is degraded | **§B — Graph bypass** |
| Qdrant timeouts in `_run_documents` | Qdrant is degraded | **§C — Document retrieval bypass** |
| Hatchet workflow_runs stuck in `started` | AI pool worker is stuck | **§D — Hatchet worker restart** |
| Kestra dispatcher returning `kestra_network_error` | Kestra container is unhealthy | **§E — Kestra restart** |
| Redis evictions spiking / cache miss rate 100% | Redis OOM or full | **§F — Redis recovery** |
| SeaweedFS PUT failures from renderers | SeaweedFS unhealthy | **§G — SeaweedFS bypass** |
| Caddy returning 502 for some routes | Caddy upstream stuck | **§H — Caddy reload** |

---

## §A — LLM failover (vLLM down or unhealthy)

The orchestrator's `_call_llm` dispatcher already supports
cross-backend failover when `LLM_BACKEND_FALLBACK` is configured.
Verify the failover ladder fires; if not, flip the primary.

1. **Confirm the failover ladder is wired:**
   ```bash
   docker compose exec -T fastapi python -c "
   from app.config import settings
   print('LLM_BACKEND:', settings.LLM_BACKEND)
   print('LLM_BACKEND_FALLBACK:', settings.LLM_BACKEND_FALLBACK)
   print('VLLM_URL:', settings.VLLM_URL)
   print('LLM_PRIMARY_URL:', settings.LLM_PRIMARY_URL)
   print('ANTHROPIC_API_KEY set:', bool(settings.ANTHROPIC_API_KEY))"
   ```
   `LLM_BACKEND_FALLBACK` must be `downshift` (Anthropic-primary
   → DeepSeek/Ollama fallback) or `local_llm` (Anthropic
   → vLLM/Ollama). If it's empty, set it temporarily.

2. **Verify failover events firing:** §16.2 LLM-pipeline dashboard
   → "Failovers (req/s)" panel. Spike right after vLLM went down
   = failover working.

3. **If failover isn't kicking in,** force-flip the backend:
   ```bash
   docker compose exec -T fastapi sh -c '
       echo "LLM_BACKEND=anthropic" >> /tmp/override.env
       echo "Restart fastapi to pick this up; or use docker compose env vars."
   '
   # Then operator chooses how to deploy the override (compose env
   # file, k8s configmap, etc.).
   ```

4. **Try to restart vLLM in parallel:**
   ```bash
   docker compose restart vllm
   ```
   Cold-start ~50s. Watch `docker compose logs -f vllm` for the
   "Application startup complete" line.

5. **Drop the override** when vLLM is back healthy.

---

## §B — Graph bypass (Neo4j degraded)

1. **Enable graph-bypass flag** so the orchestrator's graph branch
   returns empty results rather than hanging on Neo4j:
   ```bash
   docker compose exec -T fastapi python -c "
   import asyncio, os, redis.asyncio as redis
   async def main():
       r = redis.from_url(f'redis://:{os.environ[\"REDIS_PASSWORD\"]}@redis:6379/0')
       await r.set('georag:flags:retrieval_graph_bypass', '1', ex=14400)
   asyncio.run(main())"
   ```
   4-hour TTL so the flag auto-expires if you forget to clear it.

2. **Symptom impact:** queries that require graph traversal
   (deposit-type, formation→host-rock, operator-chain queries)
   produce empty graph context. The §G overnight cache work means
   spatial/docs results still serve cleanly.

3. **Restart / repair Neo4j:**
   ```bash
   docker compose logs --tail=50 neo4j | grep -iE "error|warn|disk|memory"
   docker compose restart neo4j
   ```
   If Neo4j won't come back, run dr-2 procedure §B-Neo4j for full
   rebuild from silver.

4. **Drop bypass flag** when Neo4j is healthy:
   ```bash
   docker compose exec -T fastapi python -c "
   import asyncio, os, redis.asyncio as redis
   async def main():
       r = redis.from_url(f'redis://:{os.environ[\"REDIS_PASSWORD\"]}@redis:6379/0')
       await r.delete('georag:flags:retrieval_graph_bypass')
   asyncio.run(main())"
   ```

---

## §C — Document retrieval bypass (Qdrant degraded)

Similar shape to §B:

1. Enable bypass: `georag:flags:retrieval_qdrant_bypass`.
2. Document retrieval falls back to BM25-only via PostGIS full-text.
   Precision drops ~15-25% per §10.6 measurement but citations
   still ground correctly.
3. Restart / rebuild Qdrant (see dr-2 §B-Qdrant for full re-embed).
4. Drop the flag when healthy.

---

## §D — Hatchet worker restart (AI pool stuck)

1. **Diagnose** — which workflow is stuck?
   ```sql
   SELECT run_id, workflow_kind, status, started_at,
          NOW() - started_at AS age
     FROM workflow.workflow_runs
    WHERE status = 'started'
      AND started_at < NOW() - INTERVAL '1 hour'
    ORDER BY started_at;
   ```

2. **Mark stuck rows as failed** so future similar requests don't
   pile up on the dead-letter queue:
   ```sql
   UPDATE workflow.workflow_runs
      SET status = 'failed',
          ended_at = NOW(),
          failure_reason = 'partial outage — operator marked stuck '
                          'after worker restart per dr-5'
    WHERE run_id IN (...);
   ```

3. **Restart the worker:**
   ```bash
   docker compose restart hatchet-worker-ai
   # or
   docker compose restart hatchet-worker-ingestion
   ```

4. **Re-fire any work** that was lost via the cockpit / API.

---

## §E — Kestra restart (the most common partial outage)

This is what hit yesterday — Kestra's Hikari pool stalled after
postgresql restarted out from under it. The fix is a clean restart:

1. ```bash
   docker compose restart kestra
   ```

2. **Wait for healthcheck.** Takes ~30s.

3. **Verify the support_packet dispatcher path:** fire a test ticket
   from the cockpit; the result modal should show
   `kestra_dispatch.dispatched=true` once the env vars are
   configured per `phase_g_followup_kestra_pagerduty_wired.md`.

---

## §F — Redis recovery (OOM / full)

1. **Inspect memory state:**
   ```bash
   docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" \
       --no-auth-warning INFO memory | head -10
   ```

2. **If evictions are the cause** (allkeys-lru hitting the ceiling),
   bump `REDIS_MAXMEMORY`:
   ```bash
   docker compose exec -T redis redis-cli -a "$REDIS_PASSWORD" \
       --no-auth-warning CONFIG SET maxmemory 2gb
   ```
   Update `.env` to persist across restarts.

3. **If keys leaked,** flush the cache namespace (NOT FLUSHALL —
   that would hit Laravel sessions too):
   ```bash
   docker compose exec -T fastapi python -c "
   import asyncio, os, redis.asyncio as redis
   async def main():
       r = redis.from_url(f'redis://:{os.environ[\"REDIS_PASSWORD\"]}@redis:6379/0')
       async for k in r.scan_iter('georag:rag_cache:*', count=1000):
           await r.delete(k)
       async for k in r.scan_iter('georag:graph_entities:*', count=1000):
           await r.delete(k)
   asyncio.run(main())"
   ```

4. **Cache warms naturally** as traffic resumes.

---

## §G — SeaweedFS bypass (renderer fallback)

The Report Builder + PDF renderer already fall back to inline
data URIs when SeaweedFS PUTs fail. No action needed beyond:

1. Restart SeaweedFS:
   ```bash
   docker compose restart minio
   ```
   (Container name is `minio` from before the SeaweedFS ADR-0001
   migration; the binary inside is SeaweedFS in S3 mode.)

2. Verify with `docker compose logs --tail=20 minio | grep -i ready`.

---

## §H — Caddy reload (ingress 502)

1. Validate the config:
   ```bash
   docker compose exec -T caddy caddy validate --config /etc/caddy/Caddyfile
   ```

2. Reload (zero-downtime; preserves in-flight connections):
   ```bash
   docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile
   ```

3. If Caddy itself is unhealthy:
   ```bash
   docker compose restart caddy
   ```

---

## Post-mortem

Partial outages typically resolve in < 30 min and don't warrant a
full customer-facing post-mortem. Internal record:
- Open a ticket in `ops.support_tickets` with `category='partial_outage'`
- Record the affected component + minutes of degradation
- `record_decision('partial_outage_recovery', ...)` so the next
  occurrence has the playbook history

If degradation lasted > 4 hours OR multiple components were
involved, escalate to a §11.5 SLA-grade post-mortem.

## Open questions for Kyle

1. **Flag TTL defaults.** Today: graph_bypass / qdrant_bypass each
   have 4h TTLs. Confirm or adjust.
2. **Auto-detection of partial outages.** Today operator-triggered.
   §11.5 alert manager could auto-set bypass flags when a
   component's healthcheck fails for > N minutes. Default
   proposal: operator-gated until §11.5 lands.
3. **Cockpit one-click toggles** for the bypass flags — currently
   redis-cli only. A 5-button "degraded modes" panel in the cockpit
   would slash the time-to-mitigation by ~5x. Worth a small ticket
   in §10 follow-up.
