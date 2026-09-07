# Chapter 07 — Orchestration

> **Reconciled 2026-09-07** against `config/horizon.php`, the three classes
> in `app/Jobs/`, the `POOLS` registry in
> `src/fastapi/app/hatchet_workflows/worker.py` (51 workflows), every
> `on_crons=` declaration, the two trigger routers, the Azure scheduler
> jobs under `deploy/azure/containerapps/`, the GitHub Actions schedules,
> and `docs/hatchet_review_2026_08_21.md` for what Azure was observed to
> do. Review findings are marked open or closed against today's code (§7).

Hard rule 7: **no overlap.** Two orchestrators exist, and two schedulers
outside the application.

| What | Owns | Triggered by |
|---|---|---|
| **Laravel Horizon** (Redis queues) | Three short, user-triggered jobs: query streaming, export generation, a debounced view refresh | `dispatch()` from three controllers |
| **Hatchet** (`hatchet-lite` + one worker) | Everything durable: ingestion, embedding, every cron, the outbox, the Phase 0 agents, reports and training skeletons | FastAPI trigger endpoints, in-process `aio_run_no_wait`, 29 cron expressions |
| GitHub Actions `schedule:` | Nightly eval gate, weekly chaos / coverage / CodeQL | cron on the runner |
| Azure Container Apps Jobs | Nightly shutdown and morning startup of the production tier | cron, DST-guarded |

There is **no Laravel scheduler**: `routes/console.php` registers only the
`inspire` command, so nothing runs `schedule:run` and nothing can hide a
recurrence there. Removed and defined nowhere: Dagster (2026-07-28, tree
deleted 2026-08-28), Kestra and Caddy (2026-07-28), Ofelia and the backup
agent (2026-08-23), Activepieces (Phase 3).

---

## 1. Laravel Horizon

- **Process**: [docker/horizon-entrypoint.sh](../../../docker/horizon-entrypoint.sh)
  starts the HTTP health listener (`docker/horizon-health.php`, port 8080)
  then `exec`s `php artisan horizon`. Azure's `laravel-horizon-cc` probes
  that listener; compose could use `horizon:status` but runs the same path.
- **Supervisors** ([config/horizon.php](../../../config/horizon.php)):

| Supervisor | Queue | Balance | Processes (default / `local` / `production`) |
|---|---|---|---|
| `supervisor-1` | `default` | `auto` | 1 / 3 / 10 |
| `supervisor-llm` | `llm` | `simple` | `HORIZON_LLM_MAX_PROCESSES`: 2 / 2 / 5 |

  Redis db 0 (`REDIS_QUEUE_DB`); `waits.redis:default = 60` seconds.
- **The three jobs** — this is the whole Horizon workload:

| Job | Queue | Limits | Dispatched by | Does |
|---|---|---|---|---|
| `StreamQueryFromFastApi` | `llm` | `timeout 300`, `tries 1` | `QueryController` | `POST /internal/queries` to FastAPI, reads the SSE stream (`status/bind/delta/citation/completed/failed`) and re-broadcasts each frame as `QueryStreamEvent` on Reverb |
| `GenerateExportJob` | `default` | `timeout 300`, `tries 1` | `ExportController::store` | Builds the export artefact through `StorageService` (the `exports` disk) and marks the export row |
| `DebounceWorkspaceMvRefresh` | `default` | `tries 3`, `backoff 30`, `ShouldBeUnique` for 300 s, 30 s delay | `IngestionProgressBroadcastController` on a completed ingestion event | Coalesces a burst of completions into one `REFRESH MATERIALIZED VIEW` per workspace (a Redis last-dispatch stamp lets a newer dispatch supersede it), then fires `WorkspaceDataUpdated`, `WorkspaceActivityBroadcast`, `AdminSurfaceUpdated` |

- **Nothing else is queued.** No listener, notification or mailable
  implements `ShouldQueue`; all eight broadcast events implement
  `ShouldBroadcastNow`, so Reverb fan-out never touches the queue. Failed
  jobs land in `failed_jobs` (migration `0001_01_01_000002_create_jobs_table`).
- **Metrics are blank by construction.** `horizon:snapshot` has to be
  scheduled for the Horizon metrics graphs to populate; with no scheduler
  it never runs. The dashboard's job lists still work.
- **Rule of thumb** (unchanged): Horizon is for work a user is waiting on
  that started a few hundred milliseconds ago. Anything that can run for
  minutes, needs retries across restarts, or recurs goes to Hatchet.

---

## 2. Hatchet

### 2.1 Engine and worker

| | Dev (compose) | Azure |
|---|---|---|
| Engine | `hatchet-lite:v0.86.12`, ports 8889 → 8888 (UI/REST) and 7077 (gRPC), state in the `hatchet` DB on the stack's Postgres ([Ch 02 §6](02-data-stores.md#6-hatchet-engine-state)) | `hatchet-cc` running `hatchet-lite:v0.89.7` (version drift), 1 replica, TCP ingress 7077, state in the `hatchet` DB on `georag-pg-cc` |
| Worker | `hatchet-worker`, `WORKER_POOL=all`, 20 slots, 6 CPU / 24 GiB + GPU, healthcheck greps `/proc/1/cmdline` | `hatchet-worker-cc`, `WORKER_POOL=all`, 20 slots, 4 vCPU / 8 GiB, **maxReplicas 1**, no ingress; liveness probe on the SDK health server (port 8001, event-loop block threshold 30 s) added by `apply-probes.sh` |
| SDK | `hatchet-sdk>=1.33` (`src/fastapi/pyproject.toml`) | same image |
| Auth | `HATCHET_CLIENT_TOKEN` from `hatchet-admin token create`; gRPC insecure, cookie insecure (`HATCHET_*_INSECURE=t`) | flags flipped to `f` per `.env.production.example`; CD deploys the worker image but not the engine |

Token bootstrap and the engine's environment are in
[Ch 01 §4](01-services.md#4-dev-data--domain-service-model-sidecars-stores-hatchet).
The worker never scales to zero on Azure: it has no ingress for the default
HTTP scaler and its every-minute crons keep it busy, so it is the one
production app the nightly shutdown does not stop (§4).

### 2.2 The registry — 51 workflows

`POOLS` in `worker.py` has an `ingestion` list (13) and an `ai` list (38);
`all` is their concatenation and the only pool anything runs. The split
exists so the every-minute crons could one day move to a small always-on
pool; today it is dormant. `python -m app.hatchet_workflows.worker --list`
prints the names without connecting. Crons are UTC.

**Ingestion list (13)**

| Workflow | Cron | Role |
|---|---|---|
| `outbox_dispatcher` | `* * * * *` | Drains `outbox.pending_propagations` (§2.5) |
| `ingest_pdf` | — | The PDF pipeline ([Ch 04 §3](04-ingestion-flow.md#3-the-ingest_pdf-hatchet-workflow)); `GROUP_ROUND_ROBIN`, `max_runs=2` per workspace; dispatches `embed_pending_passages` after persist |
| `tiff_normalize` | — | Lossless TIFF → PDF, then routes into `ingest_pdf` (ADR-0005) |
| `ingest_zip_archive` | — | Extracts and fans out by extension |
| `ingest_spatial`, `ingest_tabular`, `ingest_well_logs` | — | Vector, drill CSV/XLSX, LAS ingest ([Ch 04 §4](04-ingestion-flow.md#4-the-other-ingest-workflows)); `ingest_tabular` dispatches `promote_silver_to_gold` per project |
| `stale_run_detector` | `*/15 * * * *` | Recovers `silver.ingest_progress` rows stuck in `started` past 15 min: completes finished-but-unmarked embeds, re-dispatches dead parses in-process, times out the rest |
| `nightly_ingestion_integrity` | `0 2 * * *`, `0 4 * * *` | Four-tier orphan sweep; Tier 1 re-dispatches bronze objects with no silver row **over HTTP** to `FASTAPI_INTERNAL_URL` (§7, finding 5); sweeps `promote_silver_to_gold` |
| `reliability_metrics_publisher` | `* * * * *` | Refreshes in-process Prometheus gauges that nothing scrapes in production ([Ch 12](12-observability.md)) |
| `storage_tiering_run` | `0 3 * * *` | Phase 0 agent |
| `index_health_check` | `0 */6 * * *` | Phase 0 agent (hypopg what-ifs) |
| `store_reconciliation_run` | `0 4 * * *` | Phase 0 agent; cross-store counts, consumes outbox dead-letters |

**AI list (38)**

| Workflow | Cron | Role |
|---|---|---|
| `audit_ledger_verify` | `0 2 * * *` | Hash-chain verification of the previous 24 h |
| `repair_shadow_aggregate` | `15 2 * * *` | Repair-loop shadow telemetry → `gold.repair_shadow_daily` |
| `tenant_isolation_audit` | `0 2 * * *` | Phase 0 agent; writes outbox rows |
| `graph_tenant_audit` | `30 2 * * *` | Phase 0 agent for a graph store that no longer exists; runs nightly regardless |
| `mv_refresh_silver` | `0 3 * * *` | `REFRESH MATERIALIZED VIEW` on the silver fact-source views |
| `public_geo_sync` | `30 3 * * 0` | Weekly ArcGIS refresh of `public_geo` (the live owner since the Dagster pull went) |
| `flow_jwt_key_reaper` | `0 4 * * *` | Expires `workflow.flow_jwt_keys` rows |
| `cold_tier_archive` | `0 4 * * *` | Writes-only cold-tier archive; pruning is operator-gated |
| `idempotency_keys_cleanup`, `pg_partman_maintenance` | `15 4 * * *` | TTL purge of `workspace.idempotency_keys`; advance the monthly partitions |
| `retention_sweep` | `45 4 * * *` | `audit.query_audit_log` 180 d, terminal `silver.ingest_progress` 90 d |
| `model_upgrade_watch_run` | `0 5 * * *` | Phase 0 agent |
| `embed_pending_passages` | `45 5 * * *`, `*/10 * * * *` | Dense + sparse embed of unembedded `silver.document_passages` into Qdrant; per-workspace singleton (`max_runs=1`) |
| `verbalize_page_images` | `20 * * * *` | Inert unless `IMAGE_VERBALIZATION_ENABLED`; returns before touching Postgres |
| `qdrant_payload_audit` | `0 * * * *` | Guard 2 payload-shape audit; fail-open when Qdrant is unreachable (§7) |
| `answer_quality_watch` | `30 14 * * *` | Yesterday's refusal / guard-fire / zero-evidence / confidence signals vs the trailing week; feeds the `answer-quality-regression` alert |
| `enrich_passage_context` | `45 14 * * *` | Contextual-retrieval headers (one LLM call per passage) |
| `model_cost_summary_run` | `0 15 * * *` | Phase 0 agent |
| `what_changed_weekly` | `0 17 * * 1` | Fans `what_changed_detector` across active workspaces (the inline comment still says "06:00 UTC") |
| `cost_burn_watcher` | `*/5 * * * *` | Emits `cost.burn.alert` audit rows; suspends LLM activity at 2× the ceiling |
| `promote_silver_to_gold` | — | Silver → gold visual tables; dispatched per project and by the nightly sweep |
| `nl_summaries` | — | One retrievable passage per structured row (ADR-0012); registered, deliberately unscheduled |
| `external_notification`, `public_geoscience_pull` | — | The two rows in `workflow.flow_registry`, reachable through the integrations endpoint (§2.3); no caller since Kestra went |
| `phase2_smoke` | — | Placeholder |
| `generate_report`, `score_targets` | — | Report Builder and Target Recommendation graphs; `execution_timeout="24h"` on a 20-slot single-replica worker |
| `field_outcome_learning`, `what_changed_detector`, `train_target_model`, `train_source_trust`, `continuous_learning_loop` | — | Learning-loop workflows; two of them are only ever run inline (§2.4) |
| `support_replay`, `restore_workspace`, `workspace_export` | — | Operator-triggered diagnosis, manifest-backed restore, per-workspace JSONL.gz export |
| `lineage_walk`, `llm_incident_diagnosis_run`, `support_packet_assemble` | — | On-demand Phase 0 agents |

Totals: 27 workflows carry 29 cron expressions. The shutdown-job header
and the compose header still say "32 registered crons"; that count
predates the 2026-08-23 and 2026-08-28 deletions.

### 2.3 How work reaches Hatchet

| Path | Mechanism | Callers |
|---|---|---|
| **Upload trigger endpoints** | `POST /internal/v1/shadow/{ingest_pdf \| tiff_normalize \| ingest_zip_archive \| ingest_spatial \| ingest_tabular \| ingest_well_logs}/trigger` in `app/routers/shadow_trigger.py` → `workflow.aio_run_no_wait(payload)` | Laravel `UploadController` (the `ShadowRouter` is retired), gated per workspace by `app/Services/Ingestion/HatchetDispatchThrottle.php` after the 2026-06-01 burst that lost 529 files to queue-expiry cancellations |
| **Integrations endpoint** | `POST /internal/v1/integrations/{flow}/trigger` in `app/routers/integrations_trigger.py`; per-flow JWT only (`Authorization: Bearer`, `scope=flow:<name>`), keys in `workflow.flow_registry` decrypted with `AUDIT_ENCRYPTION_KEY` | Designed for Kestra. No caller exists; the endpoint and its key machinery (`flow_jwt.py`, `flow_jwt_key_reaper`) remain live |
| **In-process dispatch** | `aio_run_no_wait` from inside another workflow | `ingest_pdf` → `embed_pending_passages`; `stale_run_detector` → the owning `ingest_*`; `ingest_tabular` → `promote_silver_to_gold` |
| **Engine crons** | `on_crons=` on the workflow decorator; the engine sends an empty input, so cron-fired workflows must default every field and their CEL concurrency keys must use `has()` (fixed 2026-08-21) | 27 workflows |
| **Operator** | Hatchet UI on 8889 / `hatchet-cc`, or `hatchet-admin` | ad hoc |

Laravel → FastAPI calls carry `X-Service-Key` plus a short-lived HS256
bearer from `app/Services/FastApiJwtMinter.php` (signed with the same
`FASTAPI_SERVICE_KEY`, claims identify user, project and roles).

### 2.4 Retries, timeouts, concurrency

- Per-task `retries=`: 0 on 18 tasks, 1 on 21, 2 on 9. `execution_timeout`
  ranges from 2 minutes to 24 hours. `on_failure` hooks exist on
  `ingest_pdf`, `ingest_zip_archive`, `tiff_normalize` and
  `stale_run_detector` only.
- Five workflows declare `GROUP_ROUND_ROBIN` concurrency keyed on
  `workspace_id` (`ingest_pdf` at `max_runs=2`, `embed_pending_passages`
  at 1, `enrich_passage_context`, `verbalize_page_images` among them).
  The `HatchetDispatchThrottle` docstring still says `ingest_pdf` is
  `max_runs=1`; it was raised to 2 on 2026-08-07.
- **Three call sites bypass the engine.** `routers/ml_training.py`
  (`train_target_model`, `train_source_trust`) and
  `services/report_builder/whatchanged_integration.py`
  (`what_changed_detector`) call `aio_mock_run`, the SDK's test helper:
  the task body runs inline with no run record, retry or durability. Their
  registration on the worker is decorative. (`what_changed_weekly` was
  fixed after the review.)

### 2.5 The outbox

`outbox.pending_propagations` / `outbox.propagation_attempts` are drained
every minute by `outbox_dispatcher` (`FOR UPDATE SKIP LOCKED`, per-target
semaphores `qdrant=10`, `neo4j=4`, `seaweedfs=8`, `external_webhook`
default 4). After `dead_letter_after_attempts` (3) transient failures a
row is dead-lettered and a `silver.store_reconciliation_findings` row is
written for `store_reconciliation_run`.

As built, **only two writers enqueue rows**: the tenant-isolation auditor
and the support-packet agent. Ingestion does not use the outbox — passage
embedding goes through `embed_pending_passages`, and there is no object
mirror. [Ch 04 §6](04-ingestion-flow.md#6-the-outbox-pattern) describes
the atomic silver-write-plus-outbox design; that is target state, not
what the ingest workflows do. `_dispatch_neo4j` still returns
`transient_failure` for the removed store, so a stray `neo4j` row burns
its three attempts before dead-lettering.

---

## 3. Schedulers outside the application

### 3.1 GitHub Actions

| Workflow | Cron (UTC) | Purpose |
|---|---|---|
| `eval-gate.yml` | `17 5 * * *` | Nightly golden-query and hallucination gate with LLM and embeddings stubbed ([Ch 14](14-status-matrix.md)) |
| `chaos.yml` | `0 6 * * 1` | Weekly chaos run |
| `coverage.yml` | `40 6 * * 0` | Weekly coverage; runner-only |
| `codeql.yml` | `16 23 * * 1` | Weekly CodeQL (also on PRs) |
| `perf-baseline.yml` | *(disabled)* | Its schedule is commented out; it had produced months of green runs against no target |

### 3.2 Azure Container Apps Jobs

`shutdown-scheduler-cc` fires at `0 6,7 * * *` and `startup-scheduler-cc`
at `0 13,14 * * *` UTC; each has a DST guard that exits on the wrong hour,
so the effective window is 23:00–06:00 US-Pacific. The bodies live in
`deploy/azure/containerapps/scripts/` and are copied into the job YAML by
`scripts/check_scheduler_job_parity.py`, which CI verifies. They run
under the custom **GeoRAG Nightly Scheduler** role after two cron jobs
with Contributor deleted the database on 2026-08-23.

What the window does to orchestration:

- The Flexible Server is stopped, so `hatchet-cc` cannot poll its cron
  table. **Crons that fall inside the window are not backfilled**; the
  engine logs `could not poll cron schedules` and the worker retries its
  heartbeat until the database returns.
- Since the window moved (2026-08-21) the nightly block at 02:00–05:45
  sits outside it. Still inside: `index_health_check` at 06:00 and 12:00,
  seven ticks each of `qdrant_payload_audit` and `verbalize_page_images`,
  and every tick of the minute-, 5-, 10- and 15-minute crons. The
  shutdown-job header's list of "moved in" crons (enrich at 10:30, phase-0
  at 06:00) is stale; those have since moved to 14:45 and 02:00–05:00.
- `hatchet-worker-cc` keeps running through it (§2.1), so the largest
  single line item is unaffected by the cost control.

---

## 4. Consolidated UTC timetable

| Time | Workflow(s) |
|---|---|
| every minute | `outbox_dispatcher`, `reliability_metrics_publisher` |
| every 5 min | `cost_burn_watcher` |
| every 10 min | `embed_pending_passages` (safety net) |
| every 15 min | `stale_run_detector` |
| :00 hourly | `qdrant_payload_audit`; `index_health_check` at 00/06/12/18 |
| :20 hourly | `verbalize_page_images` (inert unless enabled) |
| 02:00 | `audit_ledger_verify`, `nightly_ingestion_integrity` pass 1, `tenant_isolation_audit` |
| 02:15 | `repair_shadow_aggregate` |
| 02:30 | `graph_tenant_audit` |
| 03:00 | `mv_refresh_silver`, `storage_tiering_run` |
| 03:30 Sun | `public_geo_sync` |
| 04:00 | `nightly_ingestion_integrity` pass 2, `flow_jwt_key_reaper`, `cold_tier_archive`, `store_reconciliation_run` |
| 04:15 | `idempotency_keys_cleanup`, `pg_partman_maintenance` |
| 04:45 | `retention_sweep` |
| 05:00 | `model_upgrade_watch_run` |
| 05:17 | GitHub Actions `eval-gate` |
| 05:45 | `embed_pending_passages` (daily) |
| 06:00 Mon | GitHub Actions `chaos` |
| 06:00 / 07:00 | Azure shutdown job (one fires) |
| 06:40 Sun | GitHub Actions `coverage` |
| 13:00 / 14:00 | Azure startup job (one fires) |
| 14:30 | `answer_quality_watch` |
| 14:45 | `enrich_passage_context` |
| 15:00 | `model_cost_summary_run` |
| 17:00 Mon | `what_changed_weekly` |
| 23:16 Mon | GitHub Actions `codeql` |

---

## 5. Alerting on orchestration

`deploy/azure/alerts/create-alerts.sh` defines the log-based rules:
`scheduler-sweep-failed` and `scheduler-sweep-missing` (the Azure jobs),
`answer-quality-regression` (reads `answer_quality_watch`'s log line), a
cost-ceiling rule (reads `cost_burn_watcher`), and a Qdrant-missing-points
rule. **Nothing alerts on a workflow raising, a lost worker heartbeat, or
a run that started and never finished** — the three rules the review
proposed are not in the script. Horizon has no alerting at all beyond its
own dashboard. [Ch 12](12-observability.md) covers the rest.

---

## 6. Retry and dead-letter posture, summarised

| Path | Retry | Dead letter |
|---|---|---|
| Horizon job | `tries` on the class (1 for the two long jobs, 3 for the debounce) | `failed_jobs` table; Horizon UI |
| Hatchet task | `retries=` per task (mostly 0 or 1) | Run marked failed in the engine; `on_failure` hook only on the four ingestion workflows |
| `ingest_*` runs left `started` | `stale_run_detector` re-dispatches parse-stage deaths up to `RECOVERY_MAX_ATTEMPTS`, then `timed_out` | `silver.ingest_progress` |
| Outbox row | 3 transient failures | `dead_lettered` + `silver.store_reconciliation_findings` |
| Cron missed while Postgres is stopped | none — not backfilled | nothing records it |

---

## 7. Status of the 2026-08-21 Hatchet review

[docs/hatchet_review_2026_08_21.md](../../hatchet_review_2026_08_21.md)
measured the fleet over 2026-08-03 → 08-21 (52 workflows then; 51 now).
Where each finding stands in the code on 2026-09-07:

| Finding | Status |
|---|---|
| 1 — four nightly workflows failing on grants/schema | Closed (`47d77b1`, 2026-08-20) |
| 2 — `embed_pending_passages`, `enrich_passage_context`, `verbalize_page_images` crons born failed on a CEL `no such key` | Closed 2026-08-21 (`has()` guard, cron-payload defaults, regression tests) |
| 3 — engine DB stopped while most crons fire; worker had no probe; single replica | Partly closed: window moved so the nightly block is outside it; SDK liveness probe applied. `maxReplicas 1` and 20 slots remain |
| 4 — nightly backups failing to a store that does not exist | Closed by deletion (2026-08-23) |
| 5 — Tier 1 orphan recovery calls `fastapi-cc` over HTTP at an hour it may be scaled to zero | **Open** — `nightly_ingestion_integrity.py` still POSTs to `FASTAPI_INTERNAL_URL` |
| 6 — no alert on any Hatchet failure; `reliability_metrics_publisher` feeds a scraper that does not exist | **Open** — five log rules exist, none on worker failure; the publisher still runs every minute |
| 7 — production paths using `aio_mock_run` | Partly closed: `what_changed_weekly` fixed; `ml_training.py` and `whatchanged_integration.py` still bypass the engine |
| 8.1 outbox long-poll rebuilds a pool every minute | Open |
| 8.2 `qdrant_payload_audit` fail-open | Open |
| 8.3 `_dispatch_neo4j` returns `transient_failure` forever | Open |
| 8.4 stale crons on the engine (`vllm_security_check_run`, `backup_neo4j`) | Needs an engine-side sweep; nothing in the repo does it |
| 8.5 dead pull modules carrying crons | Closed (deleted 2026-08-28) |
| 8.6 24 h timeouts on skeleton workflows | Open |
| 8.7 engine version drift 0.86.12 / 0.89.7 | Open |
| 8.8 throttle docstring says `max_runs=1` | Open |
| 8.9 `charts/georag/templates/hatchet.yaml` describes a StatefulSet nothing runs | Open (documents, does not drive) |
| 8.10 `.claude/skills/hatchet-workflow/` has only `NOTES.md` | Open |

---

## 8. Stale comments worth clearing

- `worker.py` header: "registers two workflows".
- `docker-compose.yml` hatchet headers and the shutdown-job header: "32
  registered crons", "Phase 0 only registers the synthetic workflow", the
  moved-in cron list.
- `what_changed_weekly.py`: `0 17 * * 1` annotated "Mondays at 06:00 UTC".
- `embed_pending_passages.py` docstring: "Cron schedule omitted for now".
- `external_notification.py` and `public_geoscience_pull.py` docstrings:
  describe Kestra as the caller.
- `docker-compose.yml` `HATCHET_PG_*` comment and `config/database.php`
  `pgsql_hatchet`: the `HatchetWorkersController` dashboard they served no
  longer exists in `app/`.
- `HatchetDispatchThrottle.php`: `max_runs=1`, docling and PaddleOCR.
