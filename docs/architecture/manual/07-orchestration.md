# Chapter 07 — Orchestration

Hard Rule #7: **no overlap.** Four orchestrators live in this stack, each
owns a distinct kind of work.

| Orchestrator | Owns | Trigger source |
|---|---|---|
| **Laravel Horizon** (Redis queues) | User-triggered async — uploads, broadcast events, idempotency reapers | `dispatch()` calls from controllers, listeners |
| **Hatchet** | Durable per-document + per-event work, scheduled audit verification, outbox dispatch | FastAPI client + cron schedules baked into workflows |
| **Dagster** | Scheduled bulk pipelines, bronze→silver→gold materialisation, eval batches | Dagster daemon schedules + sensors |
| **Kestra** | Integration edge — inbound webhooks, scheduled external pulls | YAML flows (`kestra/flows/georag/*.yaml`); cron / webhook triggers |

---

## 1. Laravel Horizon

Config: [config/horizon.php](../../../config/horizon.php),
[config/queue.php](../../../config/queue.php).

Process: `php artisan horizon` in the `laravel-horizon` container
([docker-compose.yml:640](../../../docker-compose.yml)).

Queues (Redis db 0):

| Queue | Purpose |
|---|---|
| `default` | One-off background work — small jobs |
| `embeddings` | Legacy — superseded by Hatchet `embed_pending_passages_wf` |
| `notifications` | User-visible notifications (in-app) |
| `exports` | NI 43-101 generation, CSV/XLSX export, raster mosaic builds |
| `broadcasts` | Outbound Reverb event dispatch |

Supervisor configs balance per queue with `auto` strategy. Stop grace 60 s
on the container — `SG-03` fix.

Jobs live in [app/Jobs/](../../../app/Jobs/), listeners in
[app/Listeners/](../../../app/Listeners/). The dispatcher rule is simple:
Laravel queue = "we’re waiting on a response that started a few hundred ms
ago"; anything that could run for minutes goes to Hatchet.

---

## 2. Hatchet (durable workflows)

Engine: `hatchet-lite`
([docker-compose.yml:1861](../../../docker-compose.yml)) with Postgres-backed
message queue (no RabbitMQ).

Two worker pools, **same image** (`georag/fastapi:latest`), distinguished by
`WORKER_POOL`:

- `WORKER_POOL=ingestion` ([docker-compose.yml:1967](../../../docker-compose.yml)) — file ingestion + propagation work
- `WORKER_POOL=ai` ([docker-compose.yml:2164](../../../docker-compose.yml)) — GPU work (embedding, reranking, scoring)

The worker entrypoint
[src/fastapi/app/hatchet_workflows/worker.py](../../../src/fastapi/app/hatchet_workflows/worker.py)
reads `WORKER_POOL` and registers a different subset.

### Workflow registry (per pool)

`ingestion` pool registers:

- `ingest_pdf` ([ingest_pdf.py](../../../src/fastapi/app/hatchet_workflows/ingest_pdf.py))
- `outbox_dispatcher` (cron `* * * * *`)
- `stale_run_detector`
- `nightly_ingestion_integrity`
- `reliability_metrics_publisher`
- `tiff_ocr_cluster` (now deprecated by `tiff_normalize`)
- `ocr_quality_check_wf`
- `re_ocr_page`
- `backup_postgres`, `backup_neo4j`, `backup_qdrant`, `backup_redis`,
  `backup_seaweedfs`
- `cold_tier_archive_workflow`
- `workspace_export`
- `mv_refresh_silver`
- `sync_silver_to_kg`
- `phase2_smoke`
- `idempotency_keys_cleanup`
- `flow_jwt_key_reaper`
- `generate_report`

`ai` pool registers:

- `embed_pending_passages_wf` ([embed_pending_passages.py](../../../src/fastapi/app/hatchet_workflows/embed_pending_passages.py))
- `audit_ledger_verify` (cron `0 2 * * *`)
- `score_targets`
- `evaluate_workspace`
- `external_notification`
- `support_replay`
- `cost_burn_watcher`
- `continuous_learning_loop`
- `field_outcome_learning`
- `eval_real_rag_nightly`
- `restore_workspace`
- `shadow_diff`
- `public_geoscience_pull`
- `phase0_agents` — the Phase 0 agent group (Index Health, Storage Tiering,
  Store Reconciliation, etc.)
- `llm_incident_diagnosis` (PoC)

`all` is the legacy back-compat pool — registers everything. Will be removed
once the split is stable.

### Cron schedules

Cron expressions are declared inside each workflow’s decorator. Notable:

| Workflow | Cron | Why |
|---|---|---|
| `outbox_dispatcher` | `* * * * *` | Outbox fan-out latency target ≤ 1 minute |
| `audit_ledger_verify` | `0 2 * * *` UTC | Daily hash-chain verification — runs against the previous 24 h window |
| `stale_run_detector` | `*/5 * * * *` | Marks runs left `running` past TTL |
| `nightly_ingestion_integrity` | `0 3 * * *` | Bronze vs silver row-count reconciliation |
| `reliability_metrics_publisher` | `*/1 * * * *` | Publishes SLIs to Prometheus |
| `cold_tier_archive_workflow` | `0 4 * * *` | Moves bronze objects between hot/warm/cold tiers |
| `flow_jwt_key_reaper` | `0 5 * * 0` | Weekly key rotation |
| `cost_burn_watcher` | `*/15 * * * *` | Cost ceiling watchdog (Tier 3 unlock) |

### Worker slots + auth

- `HATCHET_WORKER_SLOTS=20` (default; concurrent task ceiling per worker).
- `HATCHET_CLIENT_TOKEN` is the JWT created via `hatchet-admin token create`
  ([docker-compose.yml:1918-1923](../../../docker-compose.yml)).

### Engine state

Lives in the dedicated `hatchet` logical DB on the postgresql server
([docker/postgresql/init/20-hatchet-database.sql](../../../docker/postgresql/init/20-hatchet-database.sql)).
Laravel reads it via the `pgsql_hatchet` connection
([docker-compose.yml:548-556](../../../docker-compose.yml)) for the
Hatchet Worker Dashboard at `/admin/integrations/hatchet`.

---

## 3. Dagster (scheduled bulk + materialisation)

Definitions: [src/dagster/georag_dagster/definitions.py](../../../src/dagster/georag_dagster/definitions.py).

### Resources ([resources.py](../../../src/dagster/georag_dagster/resources.py))

- `PostgresResource` — direct PG connection (transactions OK).
- `Neo4jResource` — Bolt driver.
- `S3Resource` (boto3) — `S3_ENDPOINT_URL=http://minio:8333`
  ([docker-compose.yml:1710](../../../docker-compose.yml)).
- `QdrantResource`.
- vLLM HTTP client.

### Asset groups

Already enumerated in [Ch 04 §5](04-ingestion-flow.md). Grouped by tier:

- `bronze.*` — raw file → bronze table
- `bronze_to_silver/*` — bronze → silver canonical
- `silver_*` — silver derivations
- `gold_*` — gold aggregations
- `index_*` — store fan-out (Qdrant, Neo4j)
- `reranker_labels*` — synthetic-label generation for reranker fine-tune

### Schedules + sensors

Live in [src/dagster/georag_dagster/definitions.py](../../../src/dagster/georag_dagster/definitions.py)
+ [observability/](../../../src/dagster/georag_dagster/observability/). Notable:
- `silver_reports_schedule` — nightly silver layer refresh
- `gold_h3_density_schedule` — nightly H3 aggregation
- `reranker_labels_weekly` — weekly synthetic-label batch
- Sensors: file-drop sensor on the `bronze` bucket, schema-change sensor

### OTel

`OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4318` set on both daemon
and webserver ([docker-compose.yml:1723-1725](../../../docker-compose.yml)).
`install_tracer_provider()` is called at module load in `definitions.py`.

### Reverb bridge

`commit_ingestion_run` asset POSTs to
`http://laravel-octane/api/internal/v1/ingest-progress/broadcast` with
`FASTAPI_SERVICE_KEY` — this is what makes the IngestionRuns / DrillReview /
IngestQuality / Foundry cascade re-render after a Dagster run finishes.

### Webserver

Available at `http://localhost:3001` when the `dev-ingest` profile is up
([docker-compose.yml:1776](../../../docker-compose.yml)).

---

## 4. Kestra (integration edge)

Service: `kestra/kestra:v1.2.18`, standalone mode
([docker-compose.yml:2319](../../../docker-compose.yml)).

Flows live in [kestra/flows/georag/](../../../kestra/flows/georag/) and are
bind-mounted read-only into `/app/flows`. Source of truth is the YAML; the
operator loads them into Kestra via API/CLI (D7 decision).

### Active flows

| Flow | Trigger | Forwards to | Purpose |
|---|---|---|---|
| `external_notification` ([external_notification.yaml](../../../kestra/flows/georag/external_notification.yaml)) | Webhook | FastAPI `/internal/v1/integrations/external_notification/trigger` (HMAC-signed; per-flow JWT in `Authorization`) | Inbound notifications from external systems |
| `public_geoscience_pull` ([public_geoscience_pull.yaml](../../../kestra/flows/georag/public_geoscience_pull.yaml)) | Cron | FastAPI endpoint → Hatchet `public_geoscience_pull` | Scheduled provincial open-data refresh (mines, occurrences, bedrock geology) |
| `support_packet_dispatch` ([support_packet_dispatch.yaml](../../../kestra/flows/georag/support_packet_dispatch.yaml)) | Webhook (Sanctum-authed) | FastAPI Support Cockpit endpoint | Operator-triggered support packet generation |

### Auth

- **Inbound:** Caddy ([docker-compose.yml:2393](../../../docker-compose.yml))
  is the WS-capable edge. `forward_auth` to Laravel validates Sanctum or PAT;
  on success Caddy injects basic auth (`KESTRA_BASIC_AUTH_USER` /
  `KESTRA_BASIC_AUTH_PASSWORD`) before proxying to `kestra:8080`.
  Browser-cookie users can also hit `/admin/integrations/kestra/{path?}` —
  routed by the Laravel `KestraSsoController`.
- **Outbound (Kestra → FastAPI):** per-flow JWT signed with
  `KESTRA_FLOW_JWT_SECRET`. Keys live encrypted in
  `workflow.flow_registry` (column encrypted with `AUDIT_ENCRYPTION_KEY` via
  pgcrypto). Loader:
  [src/fastapi/app/services/flow_jwt.py](../../../src/fastapi/app/services/flow_jwt.py)
  `_load_per_flow_key_sync()`.

### State

Dedicated `kestra` logical DB on the postgresql server. Both repository and
queue use Postgres — no Redis. Configured via the inline
`KESTRA_CONFIGURATION` env in [docker-compose.yml:2331-2364](../../../docker-compose.yml).

### Observability

Laravel mirrors flow state through the `pgsql_kestra` connection
([docker-compose.yml:559-564](../../../docker-compose.yml)). The Laravel
admin UI surfaces flow runs at `/admin/integrations/kestra` via the same
read-only pattern as Hatchet.

---

## 5. Cross-orchestrator contracts

### FastAPI ↔ Hatchet

FastAPI uses the Hatchet Python client (`HATCHET_CLIENT_TOKEN`,
`HATCHET_CLIENT_HOST_PORT=hatchet-lite:7077`,
`HATCHET_CLIENT_TLS_STRATEGY=none`). Trigger:
`POST /internal/v1/shadow/ingest_pdf/trigger` → FastAPI router calls
`hatchet.client.workflow.trigger(name="ingest_pdf", input=...)`.

### FastAPI ↔ Laravel

`X-Service-Key: <FASTAPI_SERVICE_KEY>` on every request both directions.
JWT-minted endpoints get an additional `Authorization: Bearer <jwt>`
header (see [app/Services/FastApiJwtMinter.php](../../../app/Services/FastApiJwtMinter.php)).

### Dagster ↔ Laravel

Dagster has no incoming endpoints from Laravel. Outgoing: only the
`commit_ingestion_run` Reverb broadcast bridge.

### Kestra ↔ FastAPI

Per-flow JWT (`KESTRA_FLOW_JWT_SECRET` + per-flow private key from
`workflow.flow_registry`). Endpoints under
`/internal/v1/integrations/<flow_name>/trigger`.

---

## 6. Dead-letter + retry posture

| Path | Retry policy | Dead letter |
|---|---|---|
| Laravel queue | `tries=3`, `backoff=[60, 300, 900]` | Failed jobs table |
| Hatchet step | Per-step `retries=N` decorator (typical: 3) | Hatchet engine UI |
| `outbox.pending_propagations` | 3 transient failures → dead-letter flag | `outbox.propagation_attempts.status='dead_lettered'` |
| Dagster asset | Built-in op retry policy, default 0 (re-run from UI) | Asset materialisation history |
| Kestra flow | `retry:` block per task | Kestra flow run history |

---

## 7. Cron schedule audit (all four)

| Schedule | Engine | Workflow / job |
|---|---|---|
| `* * * * *` | Hatchet | `outbox_dispatcher` |
| `*/1 * * * *` | Hatchet | `reliability_metrics_publisher` |
| `*/5 * * * *` | Hatchet | `stale_run_detector` |
| `*/15 * * * *` | Hatchet | `cost_burn_watcher` |
| `0 2 * * *` | Hatchet | `audit_ledger_verify` |
| `0 3 * * *` | Hatchet | `nightly_ingestion_integrity` |
| `0 4 * * *` | Hatchet | `cold_tier_archive_workflow` |
| `0 5 * * 0` | Hatchet | `flow_jwt_key_reaper` |
| (per-asset) | Dagster | bronze → silver schedules + gold rollups |
| (per-flow) | Kestra | `public_geoscience_pull` scheduled pull |
| (Ofelia cron) | Ofelia | Postgres / Neo4j / Qdrant backups |
| (cron via supervisor) | Horizon | `horizon:snapshot` every minute |
