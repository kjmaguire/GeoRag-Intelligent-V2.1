# On-Call Playbook

**Module 10 Chunk 10.8** — first-30-minutes triage script for any
non-Kyle operator who gets a page.

## Acknowledge the alert

Alertmanager routes to the configured webhook (per
`ops/runbooks/secret-management.md`). The message includes:
- Alert name (e.g. `FastAPIHighLatencyP95`).
- Severity (warning / critical).
- Service (fastapi, laravel, postgres, redis, neo4j, qdrant, martin).
- The PromQL expression that fired.
- Annotation with operator-facing description.

Click through to **Alertmanager UI** at `http://<host>:9093` and silence
or acknowledge so duplicates don't pile up while you triage.

## First five minutes — triage tree

```
Alert?
│
├─ *LowAvailability (any service)
│  → service is DOWN. Goto: SERVICE-OUTAGE.
│
├─ *HighLatency / *SlowSearch / *HighLatencyP95
│  → service responding but degraded. Goto: LATENCY-DEGRADED.
│
├─ *HighErrorRate / *RejectedConnections
│  → service responding but rejecting. Goto: ERROR-RATE-HIGH.
│
├─ AuthzDenyBurst
│  → possible probing. Goto: AUTHZ-AUDIT-TRIAGE (own runbook).
│
├─ chaos-regression issue auto-opened
│  → weekly chaos run regressed. Triage within the week.
│  Read chaos.yml output artifact + commits since last green run.
│
└─ Anything else
   → check Grafana → "GeoRAG — Service Health" dashboard for the affected
     row, then drill into per-service dashboard.
```

## SERVICE-OUTAGE branch

For each service, the recovery semantics differ. Pick the right runbook:

| Service down | Runbook |
|--------------|---------|
| FastAPI | `service-outage.md` § FastAPI |
| Laravel Octane | `service-outage.md` § Laravel |
| PostgreSQL | `service-outage.md` § PostgreSQL |
| Neo4j | `service-outage.md` § Neo4j |
| Qdrant | `service-outage.md` § Qdrant |
| Redis | `service-outage.md` § Redis |
| Martin | `service-outage.md` § Martin |
| Reverb | `service-outage.md` § Reverb |
| SeaweedFS | `service-outage.md` § SeaweedFS |

If multiple services are down simultaneously, the cause is usually:
1. Host-level outage (disk full, OOM, kernel panic). Check `dmesg`, `df`, `top`.
2. Network partition. Check `docker network inspect georag`.
3. Power / VM lifecycle. The platform host bounced — restart compose.
4. Recently-deployed bad config. **Roll back via `deploy-rollback.md`.**

## LATENCY-DEGRADED branch

p95 elevated but service is up. Most common causes ranked:

1. **LLM saturated** — Ollama/vLLM queue depth growing. Check Ollama
   logs, GPU utilization. If GPU is pegged, the queue clears in minutes.
2. **Postgres index bloat** — recent ingest run inflated indexes.
   `VACUUM ANALYZE` on the affected table.
3. **Qdrant cold cache** — Qdrant restarted recently and its index
   hasn't reloaded. Wait 10 min OR run `POST /collections/<name>/cluster`.
4. **Neo4j page cache cold** — same shape; warm via APOC script
   (`docs/RUNBOOK.md` § Neo4j).
5. **Network egress congestion** — tile proxy or external API. Check
   `Server-Timing: db;dur=` in the affected route's response.
6. **Workload spike** — real growth. Check Prometheus `query_duration`
   histogram total count rate. If 2× baseline, you're seeing growth, not
   regression. Goto capacity planning (`ops/baselines/capacity-planning.md`).

## ERROR-RATE-HIGH branch

5xx rate > 5% sustained. Triage:

```bash
# 1. Which endpoint?
curl -sG http://localhost:9090/api/v1/query \
    --data-urlencode 'query=topk(10, sum by (handler) (rate(fastapi_requests_total{code=~"5.."}[5m])))'

# 2. Recent log entries for that endpoint.
# Open Grafana → Explore → Loki:
{service="fastapi"} | json | status>=500 | line_format "{{.path}} {{.status}} {{.error_class}}"
| limit 50

# 3. If it's an unhandled exception, check Pulse:
docker compose exec laravel-octane php artisan pulse:work   # surface live exceptions
```

If the error class is new, it's likely a recently-deployed bug. Goto
`deploy-rollback.md`.

## AUTHZ-AUDIT-TRIAGE branch

→ own runbook: `ops/runbooks/authz-audit-triage.md`

## Communication

While triaging, post status updates every 15 min to the on-call channel:

```
[STATUS] {{ alert.name }} ack'd at {{ ack_time }}.
Investigating: {{ hypothesis }}.
ETA to resolution: {{ eta }}.
```

If the issue is customer-facing and >15 min, escalate per the per-tenant
SLA (V1: best-effort; V1.5+: contractual).

## Escalation

If you can't resolve in 30 min:

1. **Page Kyle** via the channel agreed at deploy time.
2. While waiting, capture state for the post-mortem:
   - Grafana screenshot of the affected dashboard at the alert window.
   - `docker compose ps`.
   - Last 200 lines from the affected service's container logs.
   - Any DB queries from `pg_stat_statements` that look anomalous.
3. Push the snapshot to `ops/incidents/<date>-<short-name>/` directory
   (create if missing).

## Post-incident

Within 24h of resolution, author a postmortem at
`ops/incidents/<date>-<short-name>/postmortem.md` covering:
- Timeline (alert fired → ack → mitigation → resolution).
- Root cause.
- What we did.
- What we'll do to prevent recurrence (concrete tickets).

Use the `engineering:incident-response` skill template if available.

## Cross-references

- `ops/runbooks/service-outage.md` — per-service recovery.
- `ops/runbooks/authz-audit-triage.md` — authz spike investigation.
- `ops/runbooks/refusal-rate-spike.md` — RAG quality regression triage.
- `ops/runbooks/deploy-rollback.md` — last-resort revert.
- `ops/baselines/capacity-planning.md` — when an alert is real growth.
- Grafana dashboards (Module 10 Chunk 10.5):
  - `GeoRAG — Service Health` (overview)
  - `GeoRAG — RAG Quality` (latency + refusal + tool perf)
  - `GeoRAG — Authorization (authz.deny)` (security events)
  - `GeoRAG — Laravel Queue & Octane` (Laravel-side health)
