# Chapter 12 — Observability

> **Reconciled 2026-09-07** against `deploy/azure/alerts/create-alerts.sh`,
> `deploy/azure/README.md`, `deploy/azure/containerapps/probes.json`,
> the then-current `ops/runbooks/azure-oncall.md`,
> `src/fastapi/app/logging_config.py`,
> `src/fastapi/app/metrics.py`, `src/fastapi/app/middleware.py`,
> `src/fastapi/app/observability/otel.py`, `src/fastapi/app/main.py`,
> `src/fastapi/app/hatchet_workflows/worker.py`, `config/logging.php`,
> `config/pulse.php`, `app/Http/Controllers/Internal/MetricsController.php`
> and `docker-compose.yml`. The previous version of this chapter described
> a Prometheus / Alertmanager / Grafana / Loki / Promtail / Tempo / OTel
> collector stack that was removed on 2026-07-28 and is defined nowhere in
> the repository. Everything below is what exists.
>
> **⚠️ 2026-09-08 — production moved from Azure Container Apps to AWS
> ([ADR-0022](../../adr/0022-aws-replaces-azure-as-the-production-cloud.md)).**
> Every production reference below — Container Apps, Azure Blob, Azure AI
> Foundry, Log Analytics, Flexible Server, the `-cc` app names — is now
> HISTORY. What replaced each is in
> [deploy/aws/README.md](../../../deploy/aws/README.md) and
> [deploy/aws/MIGRATION-PLAN.md](../../../deploy/aws/MIGRATION-PLAN.md).
> Everything this chapter says about the **dev stack** and about
> **application behaviour** is unaffected and still accurate; only the
> question of where production runs has changed. The chapter is left as
> written rather than half-edited, on the same principle §7 of Ch 00
> states: a dated notice is honest, and a partial rewrite is the drift
> this manual exists to prevent.


There is no metrics server, no log aggregator and no trace backend in
this repository, in either environment. What the platform has instead is
four things:

| Surface | Dev (compose) | Production (AWS) |
|---|---|---|
| **Logs** | container stdout/stderr, read with `docker compose logs`; Laravel also writes files under `storage/logs/` | container stdout/stderr → CloudWatch log group `/ecs/georag` (30-day retention). The two nightly sweeps write to `/ecs/georag/scheduler` (90 days) — a separate group on purpose, see §1.3 |
| **Metrics** | two unscraped Prometheus-format endpoints (FastAPI `/metrics`, Laravel `/metrics`) | the same two endpoints, still unscraped, plus free platform metrics (ALB, RDS, Bedrock) which are what the alarms actually use |
| **Traces** | a W3C `traceparent` id carried through headers, log lines and database rows; no span export | identical; the id is a join key across CloudWatch Logs Insights and Postgres rows |
| **Alerting** | none | CloudWatch alarms and log metric filters → one SNS topic with a single email subscriber; no paging |

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
`stderr` member was added 2026-08-21 because only stdout/stderr reach the
platform's log store and the file inside a replaced container was being
generated and thrown away. That reasoning is unchanged on ECS: the
`awslogs` driver reads the two streams and nothing else. Every 403 the IDOR gates
emit goes through
[`AuthorizationAuditLogger`](../../../app/Support/AuthorizationAuditLogger.php)
as an `event=authz.deny` document on that channel.

Which `LOG_STACK` the production Laravel apps run is now answerable from
the repository — `deploy/aws/terraform/config.tf` is the task environment,
where Azure had ~55 hand-set variables per app that drifted freely (Ch 00
§5). It should be set explicitly: on the `single` default the application
log is a file inside a container nobody can read, which is what the
`stderr` member above exists to avoid.

### 1.2 Where they go

**Dev.** Nothing collects container output. `docker compose logs -f
<service>` is the tool. Postgres logs slow statements to its own stderr
(`log_min_duration_statement`, Ch 02 §1.6). There is no Loki, Promtail or
Grafana service in `docker-compose.yml`, and the `docker/loki`,
`docker/promtail`, `docker/grafana`, `docker/prometheus`,
`docker/alertmanager`, `docker/tempo` and `docker/otel-collector`
directories this chapter used to link do not exist.

**Production.** Every ECS task writes to the `/ecs/georag` CloudWatch log
group through the `awslogs` driver, one stream prefix per service, with
30-day retention. The two nightly sweeps write to `/ecs/georag/scheduler`
instead, at 90 days.

**That split is deliberate, and it is the fix for a trap worth recording.**
On Azure everything landed in one table, and Container Apps populated
`ContainerAppName_s` while Container App **Jobs** left it empty and put
the name in `ContainerJobName_s`. A rule written the obvious way — against
`ContainerAppName_s == 'shutdown-scheduler-cc'` — parsed, ran, cost money
and matched nothing, silently, forever. Every KQL query was therefore
executed against the live workspace before being written down. A separate
log group removes the class: the sweeps' filters cannot accidentally scope
to the wrong field, because there is no field to get wrong.

One trap that DID survive, because it is a property of the container
runtime rather than of any cloud: **stdout is block-buffered until the
process exits; stderr is real-time.** stdout timestamps cluster at the
moment a container died, which is how a truncated sweep once printed
"shutdown sweep complete" — the line was already in the buffer and the
flush on teardown made a killed run look finished. The sweep scripts write
all progress to stderr for exactly this reason.

Retention is the only cost control, in place of Azure's hand-applied
`dailyQuotaGb = 2`. The measured 30-day peak was 0.185 GB/day, mean 0.086,
so volume was never the problem the cap was solving — an unbounded runaway
was. Retention bounds the stored total instead of refusing ingestion, which
is the better trade for a signal that alerts read.

RDS logs are NOT shipped to CloudWatch. Enabling the `postgresql` log
export is one Terraform argument and a deliberate cost decision, left
where Azure left it; `log_min_duration_statement` is unset either way, so
today there would be startup and checkpoint chatter and no slow queries.

### 1.3 Marker lines that are alert signals

Because nothing scrapes the two `/metrics` endpoints, the production
alerting design uses distinctive log lines as the signal — CloudWatch log
metric filters now, Azure Monitor scheduled queries before. **Renaming a
marker silently disables its alarm**, in either cloud. The markers a rule
can match on:

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
was true until 2026-07-28. Only `laravel-octane` is reachable through the
ALB, so the endpoint is reachable from inside the VPC and from nothing
else. **Nothing scrapes it in either
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
than shipping the four quality counters to the platform as custom metrics
(the audit's recommendation, at the time "blocked on an Azure change
nobody has made" — CloudWatch's `PutMetricData` makes it possible now, and
it is still not done), it reads the same facts from `silver.answer_runs`,
which has history. That is the pattern to follow for any new production signal:
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

### 2.3 Free platform metrics

These are the metrics production alerting is built on, because they are
collected at no cost and without any application change:

| Source | Metrics used | Alarms |
|---|---|---|
| ALB (`AWS/ApplicationELB`) | `HTTPCode_Target_5XX_Count`, `HealthyHostCount` | `georag-octane-5xx` (>10 / 5 min), `georag-octane-dead-air` (healthy hosts < 1 for 2×5 min). Azure had **no** error-rate or availability rule on its equivalent at all |
| RDS (`AWS/RDS`) | `CPUUtilization`, `FreeStorageSpace` | `georag-pg-cpu` (>85% for 3×5 min), `georag-pg-storage` (<10 GiB) |
| Bedrock (`AWS/Bedrock`) | `InvocationClientErrors`, `InvocationServerErrors`, `InvocationThrottles` | `georag-bedrock-client-errors` (>50 / 15 min), `-server-errors` (>5 / 15 min), `-throttles` (>100 for 2×15 min) |

The two Bedrock error thresholds are the Foundry ones, carried across with
their **measured** values rather than reinvented: Foundry blocked 1,421 of
2,524 calls on 2026-08-17 and nothing noticed, which is what earned the
rule its existence. Bedrock publishes the direct equivalents, so the
numbers port. (Reaching Cohere's API directly would have meant rebuilding
both application-side, which is one of the things that decided the route —
ADR-0022 §11.)

**Two Azure alarm classes have no successor, deliberately.** The per-app
`Restarts` counters are replaced by `HealthyHostCount`, which can tell
"crash-looped and served nothing" from "restarted once and recovered" —
the restart counter could not. And `georagblobcc-transaction-storm` is
gone with the failure it watched: it existed because a fixed-quota Azure
Files share stalled Qdrant's optimizer into 10.8 M storage transactions in
a day, and EFS has no quota to exhaust (Ch 02).

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
| Laravel Pulse | `/pulse` on `laravel-octane` | Pulse's `Authorize` middleware with **no** `viewPulse` gate defined, so only `APP_ENV=local` may view | recorders run everywhere (`PULSE_ENABLED` defaults true, nothing sets it), writing `pulse_*` tables created by `2026_04_09_173734_create_pulse_tables.php` on the default connection, trimmed at 7 days. In production (`APP_ENV=production`) Pulse records and nobody can look. The `Servers` recorder needs `php artisan pulse:check`, which no service runs, so the server panel is empty in every environment. There are no custom recorders; `app/Pulse/` does not exist |
| Hatchet | dev: `http://localhost:8889` on `hatchet-lite`; production: the `hatchet` service is reachable only over Cloud Map inside the VPC and is not behind the ALB, so the UI is unreachable | Hatchet's own login | workflow runs, step logs, cron schedules |
| CloudWatch console | Logs Insights over `/ecs/georag`, the alarms, ALB/RDS/Bedrock metric charts | AWS IAM | the only production dashboard |

## 5. Alerting (CloudWatch)

Everything routes to one SNS topic with a single email subscriber. There
is no on-call rotation, no PagerDuty account (`PAGERDUTY_INTEGRATION_KEY`
was always empty and the dispatcher module is gone), no Slack webhook
(`LOG_SLACK_WEBHOOK_URL` unset) and no latency alert of any kind.

**This is a rewrite, not a port.** Azure Monitor's KQL scheduled-query
rules and CloudWatch's metric-filter-plus-alarm model are different enough
that every query and threshold had to be re-expressed; what carried over
is the *reasoning* and the measured numbers, not the definitions. It is
also, unlike its predecessor, **applied by Terraform** — the Azure rules
came from a script that printed commands by default and whose last
application date the repository does not record.

Everything below is in
[`deploy/aws/terraform/alerts.tf`](../../../deploy/aws/terraform/alerts.tf).

| Alarm | Kind | Sev | Signal |
|---|---|---|---|
| `georag-scheduler-sweep-failed` | log filter, 1 h | 1 | a sweep reported a failed action (§1.3) |
| `georag-scheduler-sweep-missing` | log filter, 1 d | 1 | no `sweep complete` from either sweep in 25 h |
| `georag-bedrock-endpoint-not-inservice` | log filter | 1 | **new on AWS.** A Marketplace endpoint failed to come back after the nightly delete: no chat and no OCR, invisible to every invocation metric because there are no invocations to fail |
| `georag-octane-5xx` | metric, 5 m | 2 | >10 `HTTPCode_Target_5XX_Count` |
| `georag-octane-dead-air` | metric, 2×5 m | 1 | `HealthyHostCount` < 1 |
| `georag-octane-dead-air-alerting` | composite | 1 | the alarm that actually pages: dead-air AND not inside the maintenance window |
| `georag-maintenance-window` | log filter | — | not an alert. It goes ALARM when the shutdown sweep completes, and is the suppressor input to the composite above |
| `georag-bedrock-client-errors` | metric, 15 m | 2 | `InvocationClientErrors` > 50 |
| `georag-bedrock-server-errors` | metric, 15 m | 2 | `InvocationServerErrors` > 5 |
| `georag-bedrock-throttles` | metric, 2×15 m | 2 | `InvocationThrottles` > 100 |
| `georag-pg-cpu` | metric, 3×5 m | 2 | `CPUUtilization` > 85% |
| `georag-pg-storage` | metric, 5 m | 2 | `FreeStorageSpace` < 10 GiB |
| `georag-answer-quality-regression` | log filter, 1 d | 2 | `ANSWER_QUALITY_REGRESSION` |
| `georag-cost-burn-threshold-exceeded` | log filter, 1 h | 1 | `COST_BURN_THRESHOLD_EXCEEDED` |
| `georag-qdrant-partial-loss` | log filter, 6 h | 2 | `QDRANT_PARTIAL_LOSS` |

**Suppression is still derived, not written out again.** Azure needed two
alert-processing rules keyed on a window spelled out separately from the
crons; here the composite alarm's suppressor is an alarm driven by the
shutdown sweep's own completion marker, and `local.maintenance_window_hours`
is computed from the two cron expressions. A schedule change carries the
suppression with it, which is the point — the 2026-08-20 Azure rule
hard-coded 00:00–10:15 UTC and was wrong in both directions once the crons
moved a day later.

The log-filter alarms match **marker log lines**, so renaming a marker
silently disables its alarm. Nothing enforces that link; it is the
sharpest edge in this chapter.

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

Production probes are ECS container health checks in
[`deploy/aws/terraform/services.tf`](../../../deploy/aws/terraform/services.tf)
(`local.service_healthcheck`), applied by Terraform rather than by a
hand-run script against a JSON file. They are the compose commands, which
are already proven against these exact images.

| Service | Dev compose healthcheck | Production (ECS container check) |
|---|---|---|
| `postgresql` | `pg_isready -U georag -d georag` | managed (RDS) |
| `pgbouncer` | `psql -p 6432 -d pgbouncer -c 'SHOW POOLS'` | not deployed |
| `redis` | `redis-cli ping` | same |
| `laravel-octane` | `curl -f http://localhost:80/up` | same, plus the ALB target-group check on `/up` |
| `laravel-horizon` | `php artisan horizon:status` reports `running` or `paused` | same |
| `laravel-reverb` | `curl -f http://localhost:8080/up` | same, plus the ALB target-group check |
| `fastapi` | `curl -f http://localhost:8000/health` | same |
| `reranker`, `embedding` | `curl -f http://localhost:8000/health` | not deployed (Bedrock) |
| `sparse` | `curl -f http://localhost:8000/health` | same — SPLADE++ has no managed equivalent, so this one IS deployed |
| `qdrant` | `/readyz` over `/dev/tcp` (the image has no curl) | same. Do not "simplify" it to curl |
| `minio` (SeaweedFS) | `wget http://127.0.0.1:9333/cluster/status` | not deployed (S3) |
| `hatchet-lite` | `wget http://localhost:8888/api/ready`, 90 s start period | same, 90 s start period for the engine's own schema migration. Azure could only do TCP 7077, because Container Apps probes cannot speak gRPC and the app exposed nothing else |
| `hatchet-worker` | `grep app.hatchet_workflows.worker /proc/1/cmdline`, which proves the process exists and nothing else | the SDK health server on 8001 (`HATCHET_CLIENT_WORKER_HEALTHCHECK_ENABLED`, block threshold set explicitly to 30 s because the SDK default of 5 s would flap on a large embed batch). **This is the probe that catches a hung worker holding its queue lease** — a wedged worker finishes nothing and looks entirely fine to a process check |
| `martin` | `wget --spider http://127.0.0.1:3000/health` | same. Azure had no probe for it at all; `probes.json` predated the app |

The distinction the worker row draws is the reason these exist. ECS only
replaces a task whose **process** has exited, so a running-but-wedged
container is a healthy task forever — which is exactly the state the
on-call runbook's "ingestion has stopped moving" section sends you hunting
through logs for.

FastAPI's `/health` returns 200 whenever the process is up. `/ready`
round-trips Postgres (`SELECT 1`), Qdrant (`get_collections`) and Redis
(`PING`) and returns 503 with the per-store result if any fails; the
Neo4j check was removed 2026-07-28 and the docstring still lists it.
Compose uses `/health` for every FastAPI-image service, and the dev worker
still has only the `/proc/1/cmdline` grep — the SDK health server is
enabled in production and not in compose.

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
Before it, the ledger's hash chain had never actually been verified in
production. Nothing alerts on `verification_runs.status = 'failed'`
today; the Grafana panel and Alertmanager rule this chapter used to cite
never existed in any deployment. Unchanged by the cloud move.

## 8. Broadcast events

Eight events implement `ShouldBroadcastNow` and go straight to Reverb
(Ch 07 §1): `QueryStreamEvent`, `IngestionProgressBroadcast`,
`WorkspaceDataUpdated`, `Admin\AdminSurfaceUpdated`,
`Admin\IngestionReviewDispositionChanged`, `Admin\ReportBuildProgress`,
`User\UserInboxUpdated` and `Workspace\WorkspaceActivityBroadcast`. They
are a UX channel. The only one that doubles as an operator signal is
`AdminSurfaceUpdated`, which carries the cost-burn and other admin inbox
alerts, and §7 explains why a log-based alarm backs it. Ingestion
progress is both broadcast and persisted: `IngestionProgressBroadcast`
carries the frame and `silver.ingest_progress` (migration
`2026_05_24_230000_create_silver_ingest_progress.php`, status vocabulary
in `app/ingest_status.py`) keeps the row, so a run can be replayed
without the socket. There is no `audit-ledger.{workspace_id}` channel.

## 9. Runbooks

Current, under `ops/runbooks/`:

- [`aws-oncall.md`](../../../ops/runbooks/aws-oncall.md): the incident
  runbook. Maintenance window first, then the failure modes, with the CLI
  and Logs Insights queries for each. It keeps the Azure-era traps that
  are properties of the runtime rather than of the cloud (stdout
  buffering, judging results by state rather than exit code) and says
  plainly which failure classes are GONE and why — knowing an incident
  cannot happen any more is worth as much as knowing one can.
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
