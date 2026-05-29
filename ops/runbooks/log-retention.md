# Log + Audit Retention

**Last updated:** 2026-04-22 (Module 10 Chunk 10.6)

This runbook documents the lifecycle of every log/audit artifact in the
GeoRAG stack: where it lives, how long it's kept, who can read it, and how
to rotate or purge.

## Quick reference

| Source | Storage | Retention | Read by | Purge mechanism |
|--------|---------|-----------|---------|-----------------|
| `query_audit_log` table | PostgreSQL `public` schema | **Indefinite** (compliance) | Admins, NI 43-101 trail | Manual SQL when client retention policy expires |
| Laravel `authz_audit-*.log` | `storage/logs/` (host bind mount) | **30 days** | Promtail → Loki | Daily Monolog rotation, `AUTHZ_AUDIT_RETENTION_DAYS` env |
| Laravel `laravel.log` | `storage/logs/` | 14 days (default) | Promtail → Loki | Daily Monolog rotation, `LOG_DAILY_DAYS` env |
| FastAPI stdout/stderr | Docker container logs | Docker default (no rotation) | Promtail → Loki | `docker logs --no-trunc` until container recycle; Loki holds ingested copy 30d |
| Loki ingest | `loki_data` volume | **30 days** | Grafana | `compactor.retention_period: 720h` in `loki-config.yaml` |
| Prometheus TSDB | `prometheus_data` volume | 7 days | Grafana | `--storage.tsdb.retention.time=7d` in compose |
| FastAPI `request` logs | StructuredAccessLogMiddleware → Promtail | 30 days (via Loki) | Grafana | Inherits Loki retention |
| Authz cache counters | Redis | TTL-less (drift on Redis restart) | MetricsController | `Cache::flush()` admin op |

## Why query_audit_log keeps everything

NI 43-101 mandates an auditable trail for every claim made in a technical
report. GeoRAG's chat answers cite drill data; the queries that produced
those answers must be retrievable for the lifetime of the report — typically
the operating life of the project. We therefore **never auto-purge**
`query_audit_log` rows.

PII protection: `query_text` and `response_text` are encrypted at rest via
Laravel's `encrypted` cast (Module 9 Chunk 9.6 carry-forward); only the
`audit_id`, `user_id`, `project_id`, `workspace_id` (Module 9 Chunk 9.8),
and `query_text_hash` (deterministic SHA-256) are queryable without
decrypting.

To purge selectively (e.g. when a client offboards):

```sql
-- Delete all audit rows belonging to a workspace.
-- Runs in a transaction so it can be aborted if the count looks wrong.
BEGIN;
SELECT COUNT(*) FROM query_audit_log WHERE workspace_id = '<workspace-uuid>';
-- Confirm count, then:
DELETE FROM query_audit_log WHERE workspace_id = '<workspace-uuid>';
COMMIT;
```

## authz_audit channel — why 30 days

Module 9 Chunk 9.8 captures every 403 (cross-tenant attempt, missing
pivot, unauthenticated probe). 30 days is the SOC 2 minimum + enough
window to triage a slow-burn IDOR probing attempt. Adjust via:

```env
# .env
AUTHZ_AUDIT_RETENTION_DAYS=30
```

Loki retention (separate from Monolog daily rotation) is set in
`docker/loki/loki-config.yaml` `limits_config.retention_period: 720h`.
Both knobs must agree; if you raise Monolog to 90d, raise Loki to 2160h.

## Promtail scrape paths

Promtail runs in the dev-monitor profile and tails:

- `/laravel-logs/authz_audit-*.log` (mounted from host `./storage/logs`)
- Docker container stdout/stderr via `/var/run/docker.sock`

The authz_audit pipeline parses the `authz.deny` JSON payload into Loki
labels (`event`, `actor_user_id`, `target_workspace_id`, `target_resource`,
`reason`). The Grafana authz dashboard reads these via LogQL once the
Loki datasource resolves.

Promtail position cursor lives in `promtail_positions` named volume so
restarts resume mid-file rather than re-shipping older lines.

## Operational checks

```bash
# Loki reachable + ingesting?
curl -s http://localhost:3100/ready
curl -s http://localhost:3100/loki/api/v1/labels | jq .

# Promtail target list (should include the authz_audit job + every container)?
docker logs georag-promtail | grep "added Docker target" | wc -l

# Most recent authz.deny event (last 5 min)?
curl -sG http://localhost:3100/loki/api/v1/query \
  --data-urlencode 'query=count_over_time({channel="authz_audit"} |= "authz.deny" [5m])' \
  | jq '.data.result'

# Retention compactor running?
docker logs georag-loki | grep -i compaction | tail -5
```

## Common operator scenarios

### "I need to investigate a 403 spike from 2 weeks ago"

1. Open Grafana → "GeoRAG — Authorization (authz.deny)" dashboard.
2. Time range: last 14 days.
3. The "Loki transition path" panel describes the LogQL query to drill
   down. Copy into Explore → Loki and filter by `actor_user_id` / `reason`.

### "Storage is full because Loki kept too much"

1. Confirm Loki is the culprit: `du -sh /var/lib/docker/volumes/*loki*`.
2. Drop retention to e.g. 14 days in `loki-config.yaml`
   (`retention_period: 336h`), restart Loki.
3. Compactor purges within ~10 min.
4. Long-term fix: scale storage OR move Loki to S3 chunk store
   (single-binary mode → microservices mode).

### "Customer wants their audit trail purged"

Run the SQL DELETE shown above against `query_audit_log`. There is **no
script** — every purge is a deliberate admin action with explicit count
preview. If client volume warrants automation, file a backlog item — V1
intentionally requires the manual ceremony.

### "We're moving to Doppler / Vault — where do I rotate the encryption key?"

Module 9 Chunk 9.6 documents APP_KEY rotation in
`ops/runbooks/secret-management.md`. Rotating APP_KEY makes all currently-
encrypted `query_text` / `response_text` rows unreadable; coordinate with
ops to re-encrypt via `php artisan crypt:rotate` (Laravel built-in) before
retiring the old key.
