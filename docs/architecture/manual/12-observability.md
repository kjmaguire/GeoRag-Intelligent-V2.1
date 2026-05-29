# Chapter 12 — Observability

Six signals — metrics, logs, traces, LLM traces, audit, broadcast events —
each with its own pipeline.

## 1. Metrics (Prometheus + exporters)

### Prometheus

[docker-compose.yml:2502](../../../docker-compose.yml).
- 7-day retention.
- Config: [docker/prometheus/prometheus.yml](../../../docker/prometheus/prometheus.yml).
- Rules: [docker/prometheus/rules/](../../../docker/prometheus/rules/) — including `martin-alerts.yml`.

### Scrape targets

| Target | Path | Source |
|---|---|---|
| FastAPI | `:8000/metrics` | `app/metrics.py` (prometheus_client) |
| Laravel Pulse | `:80/pulse/metrics` | Pulse recorders |
| postgres_exporter | `:9187/metrics` | [docker-compose.yml:2600](../../../docker-compose.yml) |
| redis_exporter | `:9121/metrics` | [docker-compose.yml:2571](../../../docker-compose.yml) |
| neo4j_exporter | `:9105/metrics` | Custom JMX bridge ([docker/neo4j-exporter/](../../../docker/neo4j-exporter/)) |
| Qdrant | `:6333/metrics` | Native |
| Martin | `:3000/metrics` | Native (since 1.7) |
| vLLM | `:8000/metrics` | Native |
| Hatchet workers | `:8895/metrics` | OTel collector export |
| OTel collector | `:8888/metrics` | Self-metrics |
| Tempo | `:3200/metrics` | Native |
| Loki | `:3100/metrics` | Native |
| Promtail | `:9080/metrics` | Native |
| Grafana | `:3000/metrics` | Native |
| Alertmanager | `:9093/metrics` | Native |

### Alertmanager

[docker-compose.yml:2542](../../../docker-compose.yml).
- Config: [docker/alertmanager/alertmanager.yml](../../../docker/alertmanager/alertmanager.yml).
- Webhook receiver — operator wires Slack / PagerDuty / email.

### Grafana dashboards

[docker/grafana/dashboards/](../../../docker/grafana/dashboards/). Notable
dashboards:

- DB cache ratios (Postgres pg_stat_statements + pg_stat_kcache)
- Redis memory + AOF latency
- Queue depth (Horizon + Hatchet)
- Qdrant latency
- Neo4j page cache + bolt thread pool
- LLM throughput (tokens/sec, vLLM KV pool occupancy)
- Authz / audit dashboard (LogQL `count_over_time` on `authz_audit-*.log`)
- Martin tile cache hit ratio + request rate
- Workspace cost burn (from `usage.usage_aggregates_daily`)
- Ingest throughput (parses/sec, OCR latency, embedding queue depth)

### Provisioning

[docker/grafana/provisioning/](../../../docker/grafana/provisioning/) —
datasources auto-mounted (Prometheus + Loki + Tempo + Postgres). Dashboards
loaded read-only from the dashboards dir.

## 2. Logs (Loki + Promtail)

### Promtail

[docker-compose.yml:2706](../../../docker-compose.yml).
- Tails container stdout via the Docker socket
  (`/var/run/docker.sock:/var/run/docker.sock:ro`).
- Tails Laravel `storage/logs/authz_audit-*.log` (bind-mounted RO).
- Forwards to `loki:3100` with `service` + `channel` labels.

Config: [docker/promtail/promtail-config.yaml](../../../docker/promtail/promtail-config.yaml).

### Loki

[docker-compose.yml:2679](../../../docker-compose.yml).
Config: [docker/loki/loki-config.yaml](../../../docker/loki/loki-config.yaml).

### Log channels

- Laravel app log: `service=laravel, channel=app` (via the `stack` driver).
- Laravel authz audit log: `service=laravel, channel=authz_audit` (the
  `count_over_time` LogQL panel uses this label specifically).
- FastAPI: `service=fastapi`. Per-request `request_id` field in JSON.
- Hatchet workers: `service=hatchet-worker-ingestion|ai`.
- Dagster: `service=dagster-daemon|webserver`.
- Postgres: `service=postgres`. `log_min_duration_statement=1000` →
  every slow query in the channel.
- Neo4j: `service=neo4j`. `dbms.logs.query.enabled=INFO`, threshold 1000 ms.

## 3. Traces (OpenTelemetry + Tempo)

### OTel collector

[docker-compose.yml:2440](../../../docker-compose.yml).
- Image distroless → no healthcheck (alerts on absence of `:8888/metrics` samples).
- Config: [docker/otel-collector/otel-collector-config.yaml](../../../docker/otel-collector/otel-collector-config.yaml).
- Receivers: OTLP gRPC `:4317`, OTLP HTTP `:4318`.
- Exporters: Tempo (traces), Prometheus (metrics).

### Tempo

[docker-compose.yml:2475](../../../docker-compose.yml).
- Image `grafana/tempo:2.6.1`.
- Local-disk block storage on `tempo_data` volume.
- Config: [docker/tempo/tempo-config.yaml](../../../docker/tempo/tempo-config.yaml).
- UI / API: `:3200`.

### Span sources

| Service | How |
|---|---|
| FastAPI | OTel auto-instrumentation in `app/main.py` lifespan; per-request span via FastAPI middleware ([app/middleware.py](../../../src/fastapi/app/middleware.py)) |
| Hatchet workers | Bootstrap at `worker.py:main()` calls `install_tracer_provider()` with `service.name` from `OTEL_SERVICE_NAME` |
| Dagster | `install_tracer_provider()` at import of `definitions.py`; `OTEL_SERVICE_NAME=georag-dagster-daemon` / `georag-dagster-webserver` |
| Laravel | Spans emitted from controllers via the Laravel OTel package (see [app/Providers/](../../../app/Providers/)) |
| vLLM | Native OTLP support — emits per-request gen latency spans |
| Postgres / Neo4j / Qdrant / Redis | No direct tracing; correlated via `trace_id` propagation in request headers |

### `silver.query_traces` — plan-§0e trace object (new 2026-05-26)

Sibling of the OTel/Tempo trace path. Plan §0e mandates a denormalised
trace object per chat turn that survives independently of the OTel
backend, so analytics can run against PG without scraping Tempo.

- Schema: [2026_05_26_220000_create_silver_query_traces.php](../../../database/migrations/2026_05_26_220000_create_silver_query_traces.php).
- Writer: [src/fastapi/app/services/trace_writer.py](../../../src/fastapi/app/services/trace_writer.py).
  - `enqueue_trace()` — buffered. Background coroutine flushes every
    **5 s** or **50 traces**, whichever first.
  - `flush_buffer()` / `write_trace()` — direct (used by tests + replay).
  - Failures log at WARNING and never propagate — observability never
    fails a user query.
- Called from the LangGraph `persist_node` (see [Ch 06 §2](06-retrieval-and-agents.md)).
- `otel_trace_id` column denormalised from `silver.answer_runs.trace_id`
  so a single SQL query joins the §0e trace with the Tempo span tree.

### `trace_id` propagation

- Frontend originates the `trace_id` (W3C trace context).
- Laravel propagates through `Http::client()` headers (`traceparent`).
- FastAPI honours the header and uses the same trace_id for its spans.
- Hatchet workflows carry `trace_id` in their `input` payload (FastAPI
  inserts it when triggering).
- `silver.answer_runs.trace_id` + `silver.answer_runs.root_span_id` give
  the chat trace a permanent home.

## 4. LLM traces (Langfuse)

Self-hosted Langfuse — opt-in stack via
[docker/compose.langfuse.yml](../../../docker/compose.langfuse.yml).

| Service | Image | Port |
|---|---|---|
| `langfuse-web` | `langfuse/langfuse:3` | 3000 (UI + ingest) |
| `langfuse-worker` | `langfuse/langfuse-worker:3` | (no host port) |
| `clickhouse` | `clickhouse/clickhouse-server:24.10-alpine` | (internal) |

### Wiring

`LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`,
`LANGFUSE_BASE_URL` set on every container that calls LLMs:
- `laravel-octane` ([docker-compose.yml:602-608](../../../docker-compose.yml))
- `laravel-horizon` (663-668)
- `laravel-reverb` (752-757)
- `fastapi` (1021-1029)
- `hatchet-worker-ai` (2247-2260) — with `LANGFUSE_BASE_URL` overridden to
  in-network `http://langfuse-web:3000` because the worker has no lifespan
  hook to swap it at import time
- `dagster-daemon` / `dagster-webserver`

### What gets traced

- Every Pydantic AI agent call.
- Every reranker call (cost attribution).
- Every fusion call.
- Token counts, prompt vars, retrieval payloads → joined to `trace_id`.

### Browser deep-link

`LANGFUSE_BASE_URL=http://localhost:3001` — Laravel's SupportCockpit page
uses this for deep-links from `silver.answer_runs.trace_id`.

### Memory: Langfuse + Reverb env trap

The `hatchet-worker-ai` Langfuse base URL is *deliberately* the in-network
hostname (not localhost) because the worker has no lifespan-hook URL swap —
see [docker-compose.yml:2247-2256](../../../docker-compose.yml) for the
multi-paragraph rationale.

## 5. Laravel Pulse

[config/pulse.php](../../../config/pulse.php). Built-in recorders for:
- Slow queries
- Cache hit ratio
- Queue depth (per queue)
- Slow requests
- Slow outgoing HTTP requests

Custom recorders under [app/Pulse/](../../../app/Pulse/) (FastAPI bridge
latency, citation lifecycle counters, workspace cost burn).

Dashboard at `/pulse` (Gate-protected via `Gate::define('viewPulse', ...)`).

## 6. Audit ledger (the durable trail)

`audit.audit_ledger` — hash-chained, monthly partitioned via pg_partman.

Recipe: [docs/audit_ledger_hash_recipe.md](../../audit_ledger_hash_recipe.md).
Trigger: [phase0/90-audit-hash-chain-trigger.sql:71](../../../database/raw/phase0/90-audit-hash-chain-trigger.sql).

Verification:
- Daily `audit_ledger_verify` Hatchet workflow ([app/hatchet_workflows/audit_ledger_verify.py](../../../src/fastapi/app/hatchet_workflows/audit_ledger_verify.py))
  re-walks the previous 24 h chain.
- Writes one row to `audit.audit_ledger_verification_runs` with the verdict.
- Chain forks land in `audit.audit_ledger_chain_fork_quarantine`.
- Alerting on `verification_runs.status=failed` (Grafana panel +
  Alertmanager rule).

## 7. Sentry (currently OFF)

[project_sentry_removed_2026_05_21](../notes/INDEX.md#project_sentry_removed_2026_05_21):
`sentry/sentry-laravel` is **not** installed. `.env` wiring is commented
out. Re-enabling requires `composer require` + worker restarts, not just
flipping the env vars.

FastAPI side: `SENTRY_DSN` env is wired but the SDK is gated on a
non-empty DSN ([docker-compose.yml:1013-1020](../../../docker-compose.yml)) —
empty DSN → SDK no-ops at boot.

## 8. Dagster metrics

[project_parked_items_2026_05_25](../notes/INDEX.md#project_parked_items_2026_05_25)
notes the recent config gap fix for dagster_metrics.

Dagster exposes:
- Run history → `dagster_run_metrics_*` (asset materialisation, op duration)
- Sensor backoff counters
- Daemon liveness gauge

Scraped by Prometheus via the daemon's `/metrics` endpoint (on the same
port as the webserver UI when enabled).

## 9. Broadcast events (Reverb) as an observability source

Although Reverb is primarily a UX channel, certain events double as
observability hooks:

- `ingestion-progress.{workspace_id}` is consumed by the IngestionRuns
  UI but also recorded in `silver.ingest_progress` for forensic replay.
- `audit-ledger.{workspace_id}` lets the AuditLog page tail the chain
  in real time.

## 10. Health endpoint matrix

| Service | Endpoint | Container path |
|---|---|---|
| laravel-octane | `/up` | host:80 |
| fastapi | `/health` | host:8000 |
| laravel-reverb | `/up` | host:8085 → cont:8080 |
| hatchet-lite | `/api/ready` | host:8889 → cont:8888 |
| neo4j | `cypher-shell RETURN 1` | bolt:7687 |
| qdrant | `/readyz` | host:6333 |
| martin | `/health` | host:3002 → cont:3000 |
| vllm | `/health` | host:8001 → cont:8000 |
| prometheus | `/-/healthy` | host:9090 |
| loki | `/ready` | host:3100 |
| tempo | `/ready` | host:3200 |
| grafana | `/api/health` | host:3000 |
| alertmanager | `/-/healthy` | host:9093 |
| kestra | `/health` (mgmt port 8081) | internal |
| postgres-exporter | `/metrics` (acts as health) | host:9187 |
| redis-exporter | `/health` | host:9121 |
| neo4j-exporter | `/metrics` (Python http.client probe) | host:9105 |
| otel-collector | (none — Prometheus absence alert) | host:13133 |
| seaweedfs | `/cluster/status` (master :9333) | internal |

## 11. SLI / SLO surface

Reliability metrics published by `reliability_metrics_publisher` Hatchet
workflow:
- `georag_ingest_p95_seconds` per workspace
- `georag_query_p95_seconds`
- `georag_citation_success_ratio` (citation passes / answers)
- `georag_hallucination_block_ratio` (Layer 2-6 rejections)
- `georag_outbox_lag_seconds`
- `georag_audit_chain_intact` (boolean, falls to 0 on fork)

Each has a Grafana panel and an Alertmanager rule with sensible bounds.
