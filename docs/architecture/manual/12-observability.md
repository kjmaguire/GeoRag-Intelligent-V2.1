# Chapter 12 — Observability

> **Reconciled 2026-09-07** against `deploy/azure/alerts/create-alerts.sh`,
> `deploy/azure/README.md`, `deploy/azure/containerapps/probes.json`,
> `ops/runbooks/azure-oncall.md`, `src/fastapi/app/logging_config.py`,
> `src/fastapi/app/metrics.py`, `src/fastapi/app/middleware.py`,
> `src/fastapi/app/observability/otel.py`, `src/fastapi/app/main.py`,
> `src/fastapi/app/hatchet_workflows/worker.py`, `config/logging.php`,
> `config/pulse.php`, `app/Http/Controllers/Internal/MetricsController.php`
> and `docker-compose.yml`. The previous version of this chapter described
> a Prometheus / Alertmanager / Grafana / Loki / Promtail / Tempo / OTel
> collector stack that was removed on 2026-07-28 and is defined nowhere in
> the repository. Everything below is what exists.

There is no metrics server, no log aggregator and no trace backend in
this repository, in either environment. What the platform has instead is
four things:

| Surface | Dev (compose) | Azure (production) |
|---|---|---|
| **Logs** | container stdout/stderr, read with `docker compose logs`; Laravel also writes files under `storage/logs/` | container stdout/stderr → Log Analytics workspace `workspace-georag4ad7`, table `ContainerAppConsoleLogs_CL`; Postgres server logs via the `georag-pg-audit` diagnostic setting |
| **Metrics** | two unscraped Prometheus-format endpoints (FastAPI `/metrics`, Laravel `/metrics`) | the same two endpoints, still unscraped, plus Azure platform metrics (Container Apps, Flexible Server, Foundry, Storage) which are what the alerts actually use |
| **Traces** | a W3C `traceparent` id carried through headers, log lines and database rows; no span export | identical; the id is a join key across `ContainerAppConsoleLogs_CL` and Postgres rows |
| **Alerting** | none | Azure Monitor metric alerts and scheduled-query (log) rules routed to action group `georag-alerts-ag`, whose only receiver is one email address; no paging |

The durable record lives in Postgres: `silver.answer_runs`,
`silver.query_traces`, `audit.audit_ledger` and the few operational
tables listed in §7. The two Hatchet crons that watch answer quality and
cost burn read those tables and emit a marker log line, and Log Analytics
rules match the marker. That chain, Postgres → worker log → Log Analytics
rule → email, is the production alerting design as built.

## 1. Logs

### 1.1 Shape

**FastAPI** installs `JsonFormatter` from
[`app/logging_config.py`](../../../src/fastapi/app/logging_config.py) on
the root logger and the three `uvicorn.*` loggers. Every record becomes
one JSON document per line with `timestamp` (ISO 8601 UTC, millisecond),
`level`, `logger`, `message`, `module`, `func`, `line`, a `traceback` key
when `exc_info` is set, and every `extra={...}` keyword promoted to the
top level. Uvicorn's own access log is disabled;
`StructuredAccessLogMiddleware` in
[`app/middleware.py`](../../../src/fastapi/app/middleware.py) emits
exactly one `INFO` line per request with `request_id`, `traceparent`,
`trace_id`, `method`, `path` (no query string), `status`, `duration_ms`
and `client`, and echoes `X-Request-ID` and `traceparent` on the
response.

**The Hatchet worker** calls the same `configure_json_logging()` from
`configure_worker_logging()` in
[`worker.py`](../../../src/fastapi/app/hatchet_workflows/worker.py) at
process start (level from `LOG_LEVEL`, default `INFO`), then pins the
chatty third-party loggers (`azure.core` HTTP policy, `azure.identity`,
PDF and model libraries) to `WARNING`. This is recent: the docstring
records that on 2026-08-21 the worker emitted 23,993 console lines in 24 h
of which zero were JSON and zero carried a `trace_id`, while `fastapi-cc`
emitted 53,438 of which 53,380 were JSON. Before that date no Log
Analytics query could filter worker output by level, workspace, run or
trace.

**Laravel** uses Monolog's `JsonFormatter` (newline batch mode) on the
`single`, `daily` and `authz_audit_file` channels in
[`config/logging.php`](../../../config/logging.php). The default channel
is `stack`, whose members come from `LOG_STACK` and default to `single`,
so in compose the application log is the file
`storage/logs/laravel.log` inside the container rather than stdout. The
`stderr` channel exists but `LOG_STDERR_FORMATTER` is unset, so anything
routed to it is Monolog's default text line, not JSON. The dedicated
`authz_audit` channel is a stack of `authz_audit_file` (daily rotation,
30-day retention, `AUTHZ_AUDIT_RETENTION_DAYS`) plus `stderr`; the
`stderr` member was added 2026-08-21 because on Container Apps only
stdout/stderr reach Log Analytics and the file inside a replaced
container was being generated and thrown away. Every 403 the IDOR gates
emit goes through
[`AuthorizationAuditLogger`](../../../app/Support/AuthorizationAuditLogger.php)
as an `event=authz.deny` document on that channel.

Container-app environment variables are hand-managed on Azure (Ch 00 §5),
so which `LOG_STACK` the production Laravel apps run is not recorded in
the repository. If it is still the `single` default, the application log
on Azure is a file nobody can read; `.env.production.example` sets only
`LOG_LEVEL=info`.

### 1.2 Where they go

**Dev.** Nothing collects container output. `docker compose logs -f
<service>` is the tool. Postgres logs slow statements to its own stderr
(`log_min_duration_statement`, Ch 02 §1.6). There is no Loki, Promtail or
Grafana service in `docker-compose.yml`, and the `docker/loki`,
`docker/promtail`, `docker/grafana`, `docker/prometheus`,
`docker/alertmanager`, `docker/tempo` and `docker/otel-collector`
directories this chapter used to link do not exist.

**Azure.** Every Container App and Container App Job writes to
`ContainerAppConsoleLogs_CL` in `workspace-georag4ad7` (resource group
`georag`, region `canadacentral`). Two properties of that table decide
whether a query works at all:

- Container Apps populate `ContainerAppName_s`; Container App **Jobs**
  leave it empty and put the name in `ContainerJobName_s`. A rule written
  against `ContainerAppName_s == 'shutdown-scheduler-cc'` parses, runs,
  costs money and matches nothing. Every query in `create-alerts.sh` was
  executed against the live workspace before being written down.
- The payload is the raw line in `Log_s`. JSON lines can be parsed with
  `parse_json(Log_s)`; the worker's pre-2026-08-21 lines cannot.

A third trap from the on-call runbook: stdout inside these containers is
block-buffered until the process exits, so stdout timestamps cluster at
the moment a container died. stderr is real-time, which is why the
scheduler sweep scripts write all progress to stderr.

The workspace has `dailyQuotaGb = 2`, applied by hand
([`deploy/azure/README.md`](../../../deploy/azure/README.md) "Environment
settings applied by hand"): the 30-day peak was 0.185 GB/day and the mean
0.086, so the cap bounds a runaway at roughly $166/month. Retention is
the workspace default and is not recorded in the repository.

Postgres server logs reach the same workspace through the
`georag-pg-audit` diagnostic setting (categories `PostgreSQLLogs` and
`PostgreSQLFlexSessions`). `create-alerts.sh` step 6 used to try to
create a second setting named `pg-to-law` and failed every time, because
Azure refuses two settings sending the same category to the same sink;
the script now detects the existing setting and prints what it ships.
Widening it to `AllMetrics` and `PostgreSQLFlexQueryStoreRuntime` is
left as a deliberate cost decision, and `log_min_duration_statement` on
the Flexible Server is not set, so `PostgreSQLLogs` carries startup and
checkpoint chatter and no slow queries.

### 1.3 Marker lines that are alert signals

Because nothing ships application metrics to Azure Monitor, the
production alerting design uses distinctive log lines as the signal. The
markers a rule can match on:

| Marker | Emitted by | Meaning | Rule in `create-alerts.sh` |
|---|---|---|---|
| `ANSWER_QUALITY_REGRESSION` | `answer_quality_watch` (Hatchet cron `30 14 * * *`) | yesterday's refusal rate, guard-fire rate, zero-evidence rate or mean confidence moved past threshold against the trailing week | `answer-quality-regression`, Sev 2 |
| `COST_BURN_THRESHOLD_EXCEEDED` | `cost_burn_watcher` (`*/5 * * * *`) | a workspace spent past its hourly ceiling; at 2× the watcher suspends its LLM activity | `cost-burn-threshold-exceeded`, Sev 1 |
| `QDRANT_PARTIAL_LOSS` | `embed_pending_passages` sweep | Qdrant holds >2 % fewer points for a project than `silver.document_passages` records as embedded | `qdrant-partial-loss`, Sev 2 |
| `shutdown sweep INCOMPLETE` / `startup sweep INCOMPLETE` / `FATAL:` | scheduler jobs (stderr) | a nightly sweep reported a failed action or could not authenticate | `scheduler-sweep-failed`, Sev 1 |
| `sweep complete` | scheduler jobs | verdict line; its **absence** for 25 h is the dead-man signal | `scheduler-sweep-missing`, Sev 1 |
| `finished step run:` | Hatchet SDK in the worker | a step completed; zero over hours while the app is Running means the worker is not consuming | none (runbook §3 query only) |

The `answer_quality_watch` thresholds are in its module: a minimum
sample of 20 answers in both windows, a rise of 15 percentage points on
any of the three rates, or a confidence drop of 0.15. A window below the
sample floor reports `insufficient_sample`, which is deliberately not an
alert.

## 2. Metrics

### 2.1 The FastAPI registry

[`app/metrics.py`](../../../src/fastapi/app/metrics.py) defines 47
series on the default `prometheus_client` registry: 29 counters, 7
gauges and 11 histograms, all prefixed `georag_`. Families:

| Family | Examples |
|---|---|
| Query path | `georag_queries_total{tier,outcome,backend}`, `georag_query_duration_seconds`, `georag_first_token_latency_seconds`, `georag_llm_calls_per_query`, `georag_llm_call_budget_exceeded_total`, `georag_context_truncations_total` |
| Routing and failover | `georag_routing_decisions_total`, `georag_llm_failovers_total`, `georag_escalation_*` |
| Retrieval | `georag_retrieval_chunks_returned`, `georag_rerank_degraded_total`, `georag_source_trust_boost_*`, `georag_tool_duration_seconds`, `georag_tool_result_count`, `georag_partial_tool_failures_total` |
| Guards | `georag_hallucination_guard_layer_fires_total`, `georag_out_of_scope_refusals_total`, `georag_answer_runs_*` |
| Cost | `georag_llm_cost_usd_total`, `georag_llm_output_tokens_total`, `georag_prompt_cache_input_tokens_total`, `georag_prompt_input_tokens_total` |
| Ingestion | `georag_ingestion_run_duration_seconds`, `georag_ingestion_runs_total`, `georag_ingestion_stale_runs_total`, `georag_ingestion_active_started_count`, `georag_embed_pending_passages`, `georag_ocr_pages_total` |
| Reliability gauges | `georag_mv_refresh_lag_seconds`, `georag_outbox_lag_seconds`, `georag_qdrant_spotcheck_miss_rate`, `georag_pg_pool_saturation` |

`GET /metrics` in [`app/main.py`](../../../src/fastapi/app/main.py)
serves that registry with no authentication; its docstring argues this
is fine because "Prometheus lives on the same internal network", which
was true until 2026-07-28. On Azure only `laravel-octane-cc` has
external ingress, so the endpoint is reachable from inside the
environment and from nothing else. **Nothing scrapes it in either
environment.** The counters
increment, the process restarts, the counts vanish. Two consequences
follow from that:

- `reliability_metrics_publisher` runs every minute (Ch 07 §2.2) to
  refresh `georag_mv_refresh_lag_seconds` and
  `georag_outbox_lag_seconds` "between scrapes". It does the query and
  updates two gauges nobody reads.
- The Laravel → FastAPI bridge
  [`routers/metrics_ingestion_events.py`](../../../src/fastapi/app/routers/metrics_ingestion_events.py)
  (`POST /internal/v1/metrics`, service-key gated, whitelist of declared
  metrics) exists so `DebounceWorkspaceMvRefresh` can record its emission
  latency histogram in the FastAPI registry. Same registry, same fate.

`answer_quality_watch` was written in explicit response to this: rather
than shipping the four quality counters to Azure Monitor custom metrics
(the audit's recommendation, "blocked on an Azure change nobody has
made"), it reads the same facts from `silver.answer_runs`, which has
history. That is the pattern to follow for any new production signal:
persist it, then match a log line.

### 2.2 The Laravel exposition endpoint

[`MetricsController`](../../../app/Http/Controllers/Internal/MetricsController.php)
hand-rolls Prometheus text format at `GET /metrics` (route name
`metrics`, `service.key` middleware, CSRF and Sanctum middleware
removed). It used to be gated on `request()->ip()` being RFC-1918, which
production's trust-every-proxy configuration turned into "send one
`X-Forwarded-For` header"; the 2026-08-20 review confirmed Horizon queue
depths and authz-deny counters were readable from the internet, and the
gate became the shared service key.

Series: `horizon_queue_depth` per queue, `octane_workers_total` and
`octane_workers_busy`, `pulse_exception_total` and `slow_queries_total`
over the last 5 minutes, `cache_hit_ratio` per store,
`laravel_authz_deny_total` by reason, `reverb_broadcasts_total`, and
`dagster_runs_total` by terminal status. Dagster was retired 2026-07-28
and its tree deleted 2026-08-28, so the last series reads a table that
exists only where `georag_dagster` was provisioned (Ch 02 §1.4) and
should be removed. Like the FastAPI endpoint, nothing scrapes this one.

### 2.3 Azure platform metrics

These are the metrics production alerting is built on, because they are
collected at no cost and without any application change:

| Resource | Metrics used | Rules |
|---|---|---|
| each Container App | `Restarts`; `Requests` split by `statusCodeCategory` (only on `laravel-octane-cc`, the sole external ingress) | 8 per-app restart-count alerts (baseline), `laravel-octane-cc-5xx`, `laravel-octane-cc-dead-air` |
| `georag-pg-cc` Flexible Server | CPU, storage, connections | 3 baseline alerts, including `georag-pg-cc-down`, which fires by design every night and is suppressed by the `georag-pg-shutdown-window` processing rule |
| `georag-foundry-cc` (Cognitive Services) | `TotalTokens`, `ClientErrors`, `ServerErrors` | `georag-foundry-cc-client-errors` (>50 / 15 min), `georag-foundry-cc-server-errors` (>5 / 15 min). Foundry blocked 1,421 of 2,524 calls on 2026-08-17 and nothing noticed |
| `georagblobcc` storage account | `Transactions` | `georagblobcc-transaction-storm` (>500,000 / 6 h): the 2026-08-17..20 Qdrant optimizer loop on a full `qdrant-storage` share generated 10.8 M transactions in a day; after the quota fix, 45,357 |

## 3. Traces

### 3.1 The trace id as a join key

Tracing as built is W3C Trace Context propagation without span export:

1. [`InjectTraceparent`](../../../app/Http/Middleware/InjectTraceparent.php)
   reads or mints `traceparent` (`00-<32 hex>-<16 hex>-01`, always
   sampled) on every inbound Laravel request, stores it on the request
   attributes and echoes it on the response. The frontend does **not**
   originate it; `resources/js` contains no `traceparent` handling. The
   earlier claim in this chapter that the browser started the trace was
   wrong.
2. `StreamQueryFromFastApi` forwards `traceparent` and `X-Request-ID`
   (the query id) on `POST /internal/queries`. Since 2026-08-21 only:
   before that it sent neither, every chat request got a fresh trace id
   in FastAPI, and the middleware docstring that claimed otherwise is the
   reason nobody noticed.
3. `StructuredAccessLogMiddleware` accepts a valid v00 `traceparent` or
   mints one, and attaches `trace_id` (the 32-hex slice) to every log
   record of that request.
4. `silver.answer_runs.trace_id` and `root_span_id` (migration
   `2026_04_21_100000_create_answer_runs.php`, indexed on `trace_id`)
   give the chat turn a permanent home;
   [`AuditEmitter`](../../../app/Services/Audit/AuditEmitter.php) writes
   the same id into `audit.audit_ledger` rows; the support cockpit's
   `ops.support_ticket_traces` links tickets to trace ids.

So a trace id joins a `ContainerAppConsoleLogs_CL` line from
`laravel-octane-cc`, the lines from `fastapi-cc`, the `answer_runs` row
and the audit rows. Hatchet workflow inputs do not carry it: the
`trace_id` field in ingestion payloads that the previous version of this
chapter described is not in the trigger contracts in
`routers/shadow_trigger.py`. Ingestion audit rows instead put the Hatchet
`workflow_run_id` in the ledger's `trace_id` column, so an ingestion
trace joins to the Hatchet run and not to the upload request.

### 3.2 OpenTelemetry: present, never configured

[`app/observability/otel.py`](../../../src/fastapi/app/observability/otel.py)
is a lazy, idempotent `install_tracer_provider()` that builds an OTLP
`BatchSpanProcessor` when `OTEL_EXPORTER_OTLP_ENDPOINT` is set and
returns a no-op tracer otherwise. Only the Hatchet worker calls it, at
`main()`; `app/main.py` never does. `OTEL_EXPORTER_OTLP_ENDPOINT` is set
in no compose service, no `.env.example`, no `.env.production.example`
and no Azure YAML, and no collector exists to receive it. Every
`get_tracer()` span in the PDF parser and the worker lands on the
default no-op provider. `opentelemetry-api` and the OTLP HTTP exporter
are still in `requirements.lock.txt` for that reason.

### 3.3 `silver.query_traces` (the plan §0e trace object)

This is the one trace store that exists and is written. Three migrations
create and extend the table
(`2026_05_26_220000_create_silver_query_traces.php`,
`..._220100_provision_..._for_test_db.php`,
`2026_05_28_010000_add_context_prep_audit_and_multi_turn_resolution_to_query_traces.php`).
[`services/trace_writer.py`](../../../src/fastapi/app/services/trace_writer.py)
buffers `RetrievalTrace` objects on an asyncio queue and a background
coroutine, started in the FastAPI lifespan after the Postgres pool is
ready, flushes every 5 s or 50 traces. `enqueue_trace()` is called from
the agentic-retrieval persist path in
[`agent/agentic_retrieval/nodes.py`](../../../src/fastapi/app/agent/agentic_retrieval/nodes.py)
after the answer row is written; a full queue drops the trace with a
warning; write failures log and never propagate. `otel_trace_id` is
denormalised from `answer_runs.trace_id`, so the two rows join without
any OTel backend.

[`docs/architecture/trace_logging_design.md`](../trace_logging_design.md)
is the design; its status line still reads "schema migration drafted
(not applied); write-path not implemented", which has been false since
the migrations and writer landed.

### 3.4 Langfuse (optional, dev only)

The overlay
[`docker/compose.langfuse.yml`](../../../docker/compose.langfuse.yml)
(Ch 02 §7) runs `langfuse-web`, `langfuse-worker`, ClickHouse 26.3 and a
one-shot `langfuse-init`. The FastAPI lifespan creates
`app.state.langfuse_client` only when `LANGFUSE_HOST`,
`LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are all non-empty;
otherwise the client is `None` and the two emit sites,
`_emit_langfuse_trace()` in
[`agents/wrapper.py`](../../../src/fastapi/app/agents/wrapper.py) (the
§35.1 eleven-field contract for the Phase 0 agents) and the generation
observation in
[`agent/llm_calls.py`](../../../src/fastapi/app/agent/llm_calls.py),
no-op. `LANGFUSE_TRACING` in `.env.example` is read by nothing.
`.env.production.example` sets no `LANGFUSE_*` variable, so production
has no LLM trace store; the support cockpit's
`build_langfuse_trace_url()` produces a link to a UI that is not
deployed. The `hatchet-worker` compose service carries the Langfuse
variables with `LANGFUSE_BASE_URL` left at the in-network hostname for
the reason the compose comment gives (no lifespan hook to swap it).

## 4. Dashboards

| Dashboard | Where | Access | State |
|---|---|---|---|
| Horizon | `/horizon` on `laravel-octane` | `viewHorizon` gate from `HORIZON_ADMIN_EMAILS`; empty list means nobody (fail closed) | job lists and failed-job detail work; the metrics graphs are blank because `horizon:snapshot` needs a scheduler and there is none (Ch 07 §1) |
| Laravel Pulse | `/pulse` on `laravel-octane` | Pulse's `Authorize` middleware with **no** `viewPulse` gate defined, so only `APP_ENV=local` may view | recorders run everywhere (`PULSE_ENABLED` defaults true, nothing sets it), writing `pulse_*` tables created by `2026_04_09_173734_create_pulse_tables.php` on the default connection, trimmed at 7 days. On Azure (`APP_ENV=production`) Pulse records and nobody can look. The `Servers` recorder needs `php artisan pulse:check`, which no service runs, so the server panel is empty in every environment. There are no custom recorders; `app/Pulse/` does not exist |
| Hatchet | dev: `http://localhost:8889` on `hatchet-lite`; Azure: `hatchet-cc` exposes TCP ingress on 7077 only, so the UI is unreachable | Hatchet's own login | workflow runs, step logs, cron schedules |
| Azure portal | Log Analytics, the alert rules, per-resource metric charts | Azure RBAC | the only production dashboard |

## 5. Alerting (Azure Monitor)

Everything routes to action group `georag-alerts-ag`. Its only receiver
is one email address. There is no on-call rotation, no PagerDuty account
(`PAGERDUTY_INTEGRATION_KEY` was always empty and the dispatcher module
is gone), no Slack webhook (`LOG_SLACK_WEBHOOK_URL` unset) and no latency
alert of any kind.

**Baseline measured 2026-08-21** (the header of
[`create-alerts.sh`](../../../deploy/azure/alerts/create-alerts.sh)):
15 metric alerts (8 per-app restart counters, 3 on `georag-pg-cc`, 2
each on `fastapi-cc` and `hatchet-worker-cc`), 4 scheduled-query rules
(`qdrant-cc-optimizer-stuck`, `georag-ingest-failed`,
`georag-fastapi-critical`, `georag-worker-exception-spike`), one
processing rule (`georag-pg-shutdown-window`) and zero activity-log
alerts.

**What the script adds.** It is idempotent (`create` upserts by name),
prints commands by default and mutates only with `--apply`. Whether and
when it was last applied is not recorded in the repository.

| Rule | Kind | Sev | Signal |
|---|---|---|---|
| `scheduler-sweep-failed` | log, hourly / 1 h | 1 | §1.3 sweep failure lines |
| `scheduler-sweep-missing` | log, hourly / 1 d | 1 | no `sweep complete` from either job in 25 h |
| `laravel-octane-cc-5xx` | metric, 5 m / 15 m | 2 | >5 5xx responses (the 48 h baseline had ten, all in one hour) |
| `laravel-octane-cc-dead-air` | metric, 5 m / 30 m | 1 | zero requests |
| `suppress-during-maintenance` | processing rule | | strips actions from dead-air during the nightly window |
| `georag-pg-shutdown-window` | processing rule | | re-created with the same derived window for `georag-pg-cc-down`; the 2026-08-20 original hard-coded 00:00–10:15 UTC, wrong in both directions after the crons moved on 2026-08-21 |
| `georag-foundry-cc-client-errors` | metric, 5 m / 15 m | 2 | `ClientErrors` > 50 |
| `georag-foundry-cc-server-errors` | metric, 5 m / 15 m | 2 | `ServerErrors` > 5 |
| `georagblobcc-transaction-storm` | metric, 1 h / 6 h | 3 | `Transactions` > 500,000 |
| `answer-quality-regression` | log, hourly / 1 d | 2 | `ANSWER_QUALITY_REGRESSION` |
| `cost-burn-threshold-exceeded` | log, 15 m / 1 h | 1 | `COST_BURN_THRESHOLD_EXCEEDED` |
| `qdrant-partial-loss` | log, hourly / 6 h | 2 | `QDRANT_PARTIAL_LOSS` |

The suppression window is derived from the scheduler crons in
`deploy/azure/containerapps/*-job.yaml` at run time (currently
06:00–14:30 UTC), so a cron change carries both processing rules with
it. The three `georag-document-intel-cc-*` rules became dead on
2026-09-02 (ADR-0019) and must be deleted by hand; the script lists the
commands and does not run them.

**Known holes**, all already stated in the repository:

- No alert on a worker traceback or lost heartbeat (Ch 07 §5 and §7);
  `georag-worker-exception-spike` is a rate rule, not a presence rule.
- No alert on the answer path's latency, and no synthetic check of the
  public ingress beyond the request counters.
- `laravel-octane-cc` runs 1/1 replicas, so every deploy is a
  user-visible outage that no rule distinguishes from a failure.
- Blob storage is locally redundant with no backup workflow; there is
  nothing to alert on because there is nothing to fail (Ch 02 §8).

## 6. Health probes

| Service | Dev compose healthcheck | Azure probe (`probes.json`, applied by `apply-probes.sh`) |
|---|---|---|
| `postgresql` | `pg_isready -U georag -d georag` | managed (Flexible Server) |
| `pgbouncer` | `psql -p 6432 -d pgbouncer -c 'SHOW POOLS'` | not deployed |
| `redis` | `redis-cli ping` | `redis-cc`: TCP 6379, liveness 30 s × 3, readiness 10 s × 3 |
| `laravel-octane` | `curl -f http://localhost:80/up` | `laravel-octane-cc`: configured before `probes.json` existed; not recorded in the repo |
| `laravel-horizon` | `php artisan horizon:status` reports `running` or `paused` | `laravel-horizon-cc`: HTTP `/up` (liveness 30 s × 4) and `/ready` (readiness 15 s × 3) on 8080, served by `docker/horizon-health.php` (Ch 07 §1) |
| `laravel-reverb` | `curl -f http://localhost:8080/up` | `laravel-reverb-cc`: HTTP `/up` on 8080, 30 s × 3 / 10 s × 3 |
| `fastapi` | `curl -f http://localhost:8000/health` | `fastapi-cc`: configured before `probes.json`; not recorded |
| `reranker`, `embedding`, `sparse` | `curl -f http://localhost:8000/health` | not deployed (Foundry) |
| `qdrant` | `/readyz` over `/dev/tcp` (the image has no curl) | `qdrant-cc`: configured before `probes.json`; not recorded |
| `minio` (SeaweedFS) | `wget http://127.0.0.1:9333/cluster/status` | not deployed (Blob) |
| `hatchet-lite` | `wget http://localhost:8888/api/ready`, 90 s start period | `hatchet-cc`: TCP 7077 only, because Container Apps probes cannot speak gRPC; 45 s initial delay for the engine's own schema migration |
| `hatchet-worker` | `grep app.hatchet_workflows.worker /proc/1/cmdline`, which proves the process exists and nothing else | `hatchet-worker-cc`: the SDK health server on 8001 (`HATCHET_CLIENT_WORKER_HEALTHCHECK_ENABLED=true`, event-loop block threshold 30 s) answering `/health`; liveness 30 s × 5, readiness 15 s × 3. This is the probe that catches a hung worker holding its queue lease |
| `martin` | `wget --spider http://127.0.0.1:3000/health` | `martin-cc`: not in `probes.json` (the file predates the app) |

FastAPI's `/health` returns 200 whenever the process is up. `/ready`
round-trips Postgres (`SELECT 1`), Qdrant (`get_collections`) and Redis
(`PING`) and returns 503 with the per-store result if any fails; the
Neo4j check was removed 2026-07-28 and the docstring still lists it.
Compose uses `/health` for every FastAPI-image service. The
`HATCHET_CLIENT_WORKER_HEALTHCHECK_*` variables exist only in the Azure
probe file, so the dev worker has no equivalent signal.

## 7. The durable record in Postgres

| Table | Written by | What it holds for observability |
|---|---|---|
| `silver.answer_runs` | FastAPI persist path | one row per answer: `rejection_reason`, `hallucination_guard_results` (NULL = chain did not run, `{}` = ran clean), `confidence`, `latency_ms`, `answer_retrieval_items`, tokens, backend and model, `trace_id` / `root_span_id`. The source for `answer_quality_watch` and for `ops/runbooks/refusal-rate-spike.md` |
| `silver.query_traces` | `trace_writer` (§3.3) | the §0e per-turn retrieval trace |
| `silver.ingest_progress` | ingestion workflows via `_progress` | one row per ingestion run with its status and `outcome_detail`; what `stale_run_detector` sweeps and the IngestionRuns UI reads |
| `audit.audit_ledger` | Laravel `AuditEmitter`, FastAPI agents and watchers | hash-chained, monthly-partitioned by pg_partman (`database/raw/phase0/20-layer-b-audit-ledger.sql`); the trigger in `90-audit-hash-chain-trigger.sql` computes each row's hash |
| `audit.audit_ledger_verification_runs` | `audit_ledger_verify` (Hatchet cron `0 2 * * *`) calling `audit.run_verification()` | one verdict row per nightly walk of the previous 24 h; forks land in `audit.audit_ledger_chain_fork_quarantine` |
| `gold.mv_refresh_log` | `mv_refresh` workflows | per-view refresh timing; feeds the unread `georag_mv_refresh_lag_seconds` gauge |
| `outbox.pending_propagations` | the two Phase 0 outbox writers (Ch 07 §2.5) | propagation lag |
| `usage.usage_events`, `usage.workspace_cost_ceilings` | LLM call sites; admin UI | inputs to `cost_burn_watcher`; a breach writes a `cost.burn.alert` ledger row and an `AdminSurfaceUpdated` broadcast to the admin alerts inbox, which reaches nobody who is not already looking at it (hence the Log Analytics rule) |
| `ops.support_ticket_traces` | support cockpit | ticket ↔ trace-id links |

The audit chain has a history worth knowing. Migration
`2026_08_20_030000_restore_canonical_audit_verification_schema.php`
records that `audit_ledger_verify` failed on every run observed in Log
Analytics from 2026-08-11 to 2026-08-20 with
`UndefinedColumnError: column "workflow_run_id" ... does not exist`: the
minimal test-DB mirror of the verification table had become the
production schema, because CD runs `laravel-migrate-job` and never
`db:apply-raw` (Ch 00 §5). The migration adds the missing columns,
widens the status check and installs `audit.recompute_hash`,
`verify_hash_chain` and `run_verification` verbatim from the raw SQL.
Before it, the ledger's hash chain had never actually been verified on
Azure. Nothing alerts on `verification_runs.status = 'failed'` today;
the Grafana panel and Alertmanager rule this chapter used to cite never
existed on Azure.

## 8. Broadcast events

Eight events implement `ShouldBroadcastNow` and go straight to Reverb
(Ch 07 §1): `QueryStreamEvent`, `IngestionProgressBroadcast`,
`WorkspaceDataUpdated`, `Admin\AdminSurfaceUpdated`,
`Admin\IngestionReviewDispositionChanged`, `Admin\ReportBuildProgress`,
`User\UserInboxUpdated` and `Workspace\WorkspaceActivityBroadcast`. They
are a UX channel. The only one that doubles as an operator signal is
`AdminSurfaceUpdated`, which carries the cost-burn and other admin inbox
alerts, and §7 explains why a Log Analytics rule backs it. Ingestion
progress is both broadcast and persisted: `IngestionProgressBroadcast`
carries the frame and `silver.ingest_progress` (migration
`2026_05_24_230000_create_silver_ingest_progress.php`, status vocabulary
in `app/ingest_status.py`) keeps the row, so a run can be replayed
without the socket. There is no `audit-ledger.{workspace_id}` channel.

## 9. Runbooks

Current, Azure-era, under `ops/runbooks/`:

- [`azure-oncall.md`](../../../ops/runbooks/azure-oncall.md): the
  incident runbook. Maintenance window first, then the five failure
  modes, with the Kusto queries for each and the job-log column trap.
- [`refusal-rate-spike.md`](../../../ops/runbooks/refusal-rate-spike.md):
  what `ANSWER_QUALITY_REGRESSION` measures and how to find the cause,
  keyed on `silver.answer_runs`.
- [`secret-rotation.md`](../../../ops/runbooks/secret-rotation.md) and
  [`raw-sql-layer.md`](../../../ops/runbooks/raw-sql-layer.md).

`ops/runbooks/_archived/` holds 41 compose-era files that reference
Prometheus, Alertmanager, Grafana and `docker logs`; its README says not
to follow them.

## 10. Stale references still in the code

Listed so the next tidy has a checklist. None affects behaviour.

- `app/metrics.py` and `main.py::metrics()` docstrings describe a
  Prometheus scrape every 15 s from `docker/prometheus/prometheus.yml`
  and "project-scoped debugging through structured logs + Loki".
- `app/logging_config.py` and the `StructuredAccessLogMiddleware`
  docstring say the JSON shape exists "so Loki / Promtail can ingest"
  it; the reason is now Log Analytics `parse_json`.
- `main.py::ready()` still lists a Neo4j round-trip in its docstring.
- `MetricsController` exposes `dagster_runs_total`.
- `reliability_metrics_publisher` says it keeps gauges fresh "between
  scrapes".
- `LANGFUSE_TRACING` in `.env.example` is unread.
- `docs/architecture/trace_logging_design.md` says the write path is
  not implemented.
