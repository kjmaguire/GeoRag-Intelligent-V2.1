# Azure → AWS migration plan

**Status:** proposed, 2026-09-07. Four decisions at the end are Kyle's, not
this document's. Nothing under `deploy/aws/` is built until they are answered.

**Constraint that shapes everything:** no data migration. Postgres, Qdrant,
Redis and Blob start empty on AWS. That means the embedding vector space can
change for free, the `georag_chunks` collection can be recreated at any
dimension, and a full re-embed — normally the blocking item when the embedding
backend moves — costs nothing because there is no corpus.

Verified against the code and `docs/architecture/manual/` (reconciled
2026-09-07), not against `georag-architecture.html`. Every claim below that
came from a web source rather than this repository is marked **[unverified
in-region]** and must be confirmed with a live API call before it is built on.

---

## 0. Two corrections to the brief

**Qdrant did have persistent storage on Azure.** Ch 02 §2.1: `qdrant-cc` runs
with its data directory on the `qdrant-storage` Azure Files share (SMB,
TransactionOptimized) in the `georagblobcc` account. It is not a missing-volume
bug. What it *is* is two real defects worth not replicating: the share is
mounted with the **storage account key**, so rotating that key breaks the mount
on the next restart (`ops/runbooks/secret-rotation.md`), and the share has a
fixed **quota** whose exhaustion surfaced as Qdrant's "Not enough space
available for optimization" and generated 10.8 M storage transactions in a day
(2026-08-17..20). EFS is elastic and IAM-authorised, which removes both classes.

**The store that actually has no persistence is Redis.** Ch 02 §3.1:
`redis-cc` runs with **AOF off and no volume**. Every restart, and every
nightly scale-to-zero, drops all sessions and any queued Horizon job. This is
the persistence bug to fix in the move.

---

## 1. Target service per tier

| Tier | Today (Azure) | Target (AWS) | Why |
|---|---|---|---|
| Ingress | Container Apps ingress on `laravel-octane-cc` | **ALB** → ECS service | WebSocket support is native; Reverb needs it |
| Laravel Octane | `laravel-octane-cc` (1/1) | **ECS Fargate** service, ALB target group | 1:1 with the current shape; see §5 on the replica floor |
| Laravel Horizon | `laravel-horizon-cc` | ECS Fargate service, no ingress | |
| Laravel Reverb | `laravel-reverb-cc` | ECS Fargate service, ALB target group, **stickiness on** | WS connections must pin to a replica |
| FastAPI | `fastapi-cc` | ECS Fargate service, internal only | Reached by Laravel via Cloud Map service discovery |
| Hatchet engine | `hatchet-cc` | ECS Fargate service, internal only | |
| Hatchet worker | `hatchet-worker-cc` (4 vCPU / 8 GiB, max 1) | ECS Fargate task, 4 vCPU / 8 GiB, desired 1 | `max_runs=1` singletons assume one worker |
| Qdrant | `qdrant-cc` + Azure Files | ECS Fargate + **EFS** access point | Elastic, IAM-auth, no quota wall |
| Redis | `redis-cc`, AOF off, no volume | ECS Fargate + **EFS**, **AOF on** | Keeps nightly stop/start; fixes the persistence defect |
| Sparse (SPLADE++) | none in production | ECS Fargate sidecar service, 0.5 vCPU / 2 GiB | **Decision 4** |
| Martin | `martin-cc` | ECS Fargate service, internal only | |
| PostgreSQL | Flexible Server `georag-pg-cc` | **RDS for PostgreSQL 18** | **Decision 2**; see §3 |
| Object storage | Blob `georagblobcc` | **S3** + IAM task roles | §4 |
| LLM / embed / rerank / OCR | Azure AI Foundry | **Cohere API direct** | **Decision 1**; see §2 |
| Registry | ACR `georagacrcc` | **ECR** | |
| Secrets | Container Apps secrets | **Secrets Manager**, ECS `secrets.valueFrom` | |
| Identity | managed identity | **IAM task roles** | |
| Scheduling | 2 Container Apps Jobs, DST-guarded | **EventBridge Scheduler** (timezone-native) | §6 |
| Logs / metrics / alerts | Log Analytics + Azure Monitor | **CloudWatch Logs + metric filters + alarms → SNS** | §7 |

---

## 2. Cohere on AWS

### 2.1 What the code actually does today

Confirmed by reading the adapters, not the docs:

| Capability | URL built in code | Auth | File |
|---|---|---|---|
| Embeddings | `{endpoint}/providers/cohere/v2/embed` | `api-key:` header | `app/services/embedding.py:103` |
| Reranking | `{endpoint}/providers/cohere/v2/rerank` | `api-key:` header | `app/services/reranker.py:348` |
| OCR / parse | `{endpoint}/providers/cohere/v2/parse` | `api-key:` header | `app/services/ingest/cohere_parse_client.py:83` |
| Chat | `{endpoint}/openai/v1/chat/completions` | `api-key:` header | `app/config.py::effective_llm_url` |

The brief's structural insight holds. Three of four adapters already speak
Cohere's native v2 API; Foundry was a proxy in front of it. For those three the
move is a base URL, an auth header, and a model-name field — the request and
response handling is already Cohere-shaped.

### 2.2 The three routes, resolved

**[unverified in-region]** — everything in this subsection comes from public
sources dated September 2026 and must be confirmed with `aws bedrock
list-foundation-models --region <target>` and a live Cohere API call before it
is built on.

| | (a) Cohere API direct | (b) Amazon Bedrock | (c) Cohere on SageMaker |
|---|---|---|---|
| Command A+ (`command-a-plus-05-2026`) | yes — `api.cohere.com/v2/chat`, and OpenAI-shaped via the Compatibility API | **no** — Bedrock's Cohere generative catalogue is Command R / R+, now legacy | yes, self-managed endpoint |
| Embed v4 | yes — `/v2/embed` | yes | yes |
| Rerank v4 | yes — `/v2/rerank` | **Rerank 3.5**, not v4 | yes |
| Parse 5 | yes — `/v2/parse` | **not offered** | yes |
| Auth | API key | IAM / SigV4 | IAM / SigV4 |
| Adapter work | base URL + header + model id ×4 | full rewrite ×3, and two capabilities have no model | full rewrite ×4 + endpoint ops |

Bedrock cannot serve two of the four capabilities at the versions this
deployment uses. It is not a candidate for a complete migration; at best it
serves embed and rerank while chat and parse go elsewhere, which means running
two vendors' wire protocols for one vendor's models.

**Recommendation: (a) Cohere API direct.** It is the only route with full
version parity on all four capabilities, it is the smallest diff, and it is
cloud-agnostic — which is worth something given that this migration exists
because a cloud's credits ran out.

### 2.3 The chat path specifically

The chat adapter is the exception the brief flags, and it stays the exception.
Two sub-options under route (a):

- **Compatibility API** (`/compatibility/v1/chat/completions`) — OpenAI-shaped,
  documented to support function calling and structured outputs. Keeps
  `_call_openai_compatible_llm` and the whole streaming path intact.
- **Native `/v2/chat`** — a new adapter, a new streaming event shape, and a new
  branch in `effective_llm_url`.

Recommend the Compatibility API, falling back to a native adapter only if the
probe below fails.

**Three behaviours must be re-verified, not assumed.** They were confirmed
empirically against Foundry on 2026-07-30 and are documented in `app/config.py`'s
`AZURE_FOUNDRY_*` block:

1. JSON `response_format` is accepted.
2. Reasoning arrives in a separate `reasoning_content` field.
3. Cohere wraps JSON output in `<|START_TEXT|>` / `<|END_TEXT|>` sentinels that
   the client strips.

Whichever sub-option is chosen, a probe like `ops/validation/cohere_parse_probe.sh`
runs first and its report is committed. That is also the moment to close the
Parse v5 gap: its wire shape was **never** empirically verified even on Foundry,
and the probe exists for exactly that.

### 2.4 Backend naming — deliberate and loud

`LLM_BACKEND` accepts `azure | vllm | anthropic`; `EMBEDDING_BACKEND` and
`RERANKER_BACKEND` both **default to `foundry`** in code (`embedding.py:58`,
`reranker.py:148`) *and* in `docker-compose.yml`. An unset value on an AWS task
therefore selects a host that does not exist there and fails at first call, not
at startup.

Plan: add `cohere` as an explicit value to all three, keep `foundry`/`azure`
recognised for exactly one release as a **hard startup error** naming the
replacement (the `ocr_engine.py` retired-value pattern, extended — not a silent
new path), then delete them. `scripts/check_settings_have_readers.py` keeps this
honest.

### 2.5 SPLADE++ — the one capability with no managed equivalent anywhere

SPLADE++ (`naver/splade-cocondenser-ensembledistil`) had no Foundry equivalent
and has no Bedrock or Cohere equivalent. It encodes the named `text` sparse
vector on `georag_chunks` and is one of the legs fused in `services/fusion.py`.
Dropping it removes the sparse leg of hybrid retrieval outright.

Three options, **Decision 4**:

1. **Self-hosted sidecar on Fargate** (recommended). ~440 MB model, CPU only,
   0.5 vCPU / 2 GiB. `SPARSE_SERVICE_URL` already exists and is the supported
   path (`sparse_encoder.py:112`).
2. **In-process** in FastAPI and the worker. `_get_sparse_model()` already
   falls back to this when `SPARSE_SERVICE_URL` is unset. Cheaper, but loads
   one copy per uvicorn worker — and the sidecar exists because exactly that
   pattern OOM-killed the container on 2026-06-24 with the 2.4 GiB embedder.
   440 MB × 3 workers is survivable; it is still the pattern that failed once.
3. **Drop the sparse leg.** Cheapest, and a real capability loss. If chosen it
   is recorded in the ADR and in Ch 08, not left to be rediscovered.

---

## 3. PostgreSQL — the risk is smaller than it looks

`docker/postgresql/init/10-phase0-extensions-and-schemas.sql` and siblings
create fourteen extensions. On Azure Flexible Server `h3` sat outside the
`azure.extensions` allow-list and was a known problem. The question is whether
RDS clears the bar.

I audited every one against its actual call sites in this repository:

| Extension | On RDS PG18 **[unverified in-region]** | Call sites in this repo | Verdict |
|---|---|---|---|
| `postgis` | yes | everywhere | load-bearing |
| `postgis_topology` | yes | migrations | load-bearing |
| `postgis_raster` | yes | required by `h3_postgis` | load-bearing |
| `h3`, `h3_postgis` | yes — h3-pg added for PG 18 | `gold.h3_density_mineral.h3_index h3index` | load-bearing (DDL) |
| `pg_partman` | yes | `audit.*` monthly partitions | load-bearing |
| `pg_trgm` | yes | fuzzy lookup indexes | load-bearing |
| `pg_stat_statements` | yes | `index_health` agent | load-bearing |
| `uuid-ossp` | yes | migrations | load-bearing |
| `hypopg` | yes (RDS and Aurora) | `agents/phase0/index_health.py` | load-bearing |
| `pg_repack` | yes | one advisory string only | not invoked |
| `auto_explain` | **not `CREATE EXTENSION`** — a `shared_preload_libraries` parameter | **zero** | parameter-group setting |
| `pg_ivm` | **no** | **zero** | drop |
| `pg_stat_kcache` | **no** | **zero** | drop |

**Both extensions RDS lacks have zero call sites.** `pg_ivm` is installed and
never used — consistent with Ch 02, which records that the only materialized
view in the schema is `silver.mv_collar_summary`, refreshed by the
`mv_refresh_silver` cron with a plain `REFRESH MATERIALIZED VIEW`.
`pg_stat_kcache` and `auto_explain` are diagnostics with no reader.

So the brief's "biggest technical risk" resolves to: **managed Postgres is
viable**, RDS for PostgreSQL 18, and the migration drops two unused extensions
and moves one diagnostic into a parameter group. That is **Decision 2** — but
it is now a cost and ops preference, not a capability question. Self-managed
Postgres on EC2 remains the fallback if the in-region extension list disagrees
with the table above.

Two related notes:

- **PgBouncer is compose-only** and was already absent on Azure. asyncpg
  already runs `statement_cache_size=0`, so **RDS Proxy** would drop in
  cleanly if connection counts ever justify it. Not day one.
- The nightly stop/start needs `rds:StopDBInstance` / `StartDBInstance` in the
  scheduler role. RDS auto-restarts a stopped instance after 7 days; the
  Azure equivalent had no such limit. The nightly cadence makes this a non-issue
  in practice but it belongs in the runbook.

---

## 4. Object storage — configuration, plus two real code changes

`STORAGE_BACKEND` is honoured by both layers already:
`src/georag_object_storage/georag_object_storage/factory.py` and
`config/filesystems.php:54`. The `s3_compatible` path is what dev runs against
SeaweedFS, so pointing it at S3 is mostly configuration — but not entirely:

1. **`StorageConfig.from_env()` requires static keys.** `access_key` and
   `secret_key` are both `required=True` (`config.py:96,105`), so it raises
   before boto3 can use the ECS task-role credential chain. Must become
   optional, with the chain as the fallback.
2. **`endpoint_url` defaults to `http://minio:8333`** and is always passed to
   `boto3.client` / `aioboto3`. For real S3 it must resolve to `None` so the SDK
   picks the regional endpoint. `use_path_style_endpoint` likewise.

Both are small, both are load-bearing, and neither is caught by a config change.

**Bonus the brief called correctly.** Azure's `allowSharedKeyAccess` is still
enabled *only* because Laravel's `temporaryUrl()` signs with the account key and
`microsoft/azure-storage-blob` ^1.1 has no user-delegation SAS. S3 presigned
URLs are native to the IAM role, so the wart disappears and the flag has no
successor. `league/flysystem-azure-blob-storage`, `app/Services/Azure/*` (3
files, 303 lines) and the `azure_*` half of `georag_object_storage` become
deletable — cleanup, not blocking.

**Bronze has no backup and no restore procedure** (Ch 02 §8; the `backup_*`
workflows were deleted 2026-08-23 and production relies on Azure PITR, which
does not cover Blob). It was the one irreplaceable copy. On S3 the fix is
configuration, so this plan takes it: **versioning on, a lifecycle policy, and
Cross-Region or same-region replication on the `bronze` bucket**, decided
explicitly rather than inherited.

---

## 5. Compute notes

- **ECS Fargate over App Runner or EKS.** App Runner only serves HTTP request
  workloads — Horizon, the Hatchet worker and Hatchet itself are not that. EKS
  is the right answer at a scale this deployment is nowhere near.
- **Reverb** needs ALB target-group stickiness and an idle timeout above the
  WebSocket heartbeat.
- **`laravel-octane-cc` is min 1 / max 1 today**, and `deploy/azure/README.md`
  records both the cost objection (~$60-80/mo) and the rebuttal (`max_connections`
  429 vs a 24 h peak of 99; Octane opens PDO lazily per worker; no local state).
  On Fargate the same trade is desired count 1 vs 2 behind the ALB. Carrying the
  reasoning forward, not the setting: at desired 1, every deploy and every task
  replacement is a user-visible outage on the only public app. Recommend
  **desired 2** on Octane only, and note it in the ADR so the decision is
  re-litigated on evidence rather than rediscovered.
- **Hatchet worker stays at desired 1.** Several workflows are `max_runs=1`
  singletons and Ch 07 records `maxReplicas 1` as a still-open finding, not a
  free knob.
- **Model sidecars:** only `sparse` survives to production, and only under
  Decision 4 option 1. `embedding` and `reranker` are dev-only.

---

## 6. Scheduled shutdown / startup — the mechanism collapses

Today: `deploy/azure/containerapps/{shutdown,startup}-job.yaml` fire at
`0 6,7 * * *` and `0 13,14 * * *` UTC, each hitting **both** candidate hours
with an in-script DST guard that exits 0 on the wrong one, because Container
Apps Jobs have no timezone support. `scripts/check_scheduler_job_parity.py`
exists to keep the cron and the guard agreeing, and the alert suppression window
is derived from the crons so the schedule is not spelled out a fourth time.

**EventBridge Scheduler supports timezones natively.** One schedule each, at
`23:00` and `06:00` `America/Los_Angeles`, no double fire, no guard.

Consequences to handle rather than leave:

- `check_scheduler_job_parity.py` and the CI `scheduler-jobs` job enforce the
  Azure shape. The cron/guard half retires; the **inline-args parity** half is
  worth keeping in a new form, because an ECS RunTask override carries the same
  script twice for the same reason.
- `deploy/azure/containerapps/scripts/tests/run.sh` pins sweep behaviour at
  every failure point. That test is the reasoning; it gets ported, not deleted.
- The alert suppression window must stay **derived** from the schedule.
- `rotate-app-key.sh` + `tests/rotate-app-key.test.sh` pin what happens at each
  failure point of an APP_KEY rotation (a dump failure lifts maintenance, a
  restore failure does not, no secret changes before the in-replica half
  succeeds, the key never reaches the terminal). Same contract, Secrets Manager
  and ECS instead of `az`.

---

## 7. Observability — a rewrite, and one gap that gets worse

Azure today: ~15 baseline metric alerts plus the 12 rules in
`deploy/azure/alerts/create-alerts.sh`, several of which match **marker log
lines** (`ANSWER_QUALITY_REGRESSION`, `COST_BURN_THRESHOLD_EXCEEDED`,
`QDRANT_PARTIAL_LOSS`, `sweep complete`) because nothing scrapes the two
`/metrics` endpoints that exist. Ch 12 is explicit that this is the design as
built, not an accident.

AWS mapping:

| Azure | AWS |
|---|---|
| `ContainerAppConsoleLogs_CL` + KQL | CloudWatch Logs + Logs Insights |
| log-marker scheduled-query rules | **CloudWatch metric filters** → alarm |
| platform metrics (Container Apps `Restarts`, `Requests`) | Container Insights, ALB `HTTPCode_Target_5XX_Count`, `RequestCount` |
| `georag-pg-cc` CPU / storage / connections | RDS CloudWatch metrics |
| `georagblobcc` `Transactions` | S3 request metrics |
| `georag-alerts-ag` (one email) | SNS topic with an email subscription |

Two improvements come free: a metric filter turns a marker line into a metric
with no per-query cost (the Azure rules were scheduled queries), and the
"dead air" rule — which the restart counter could not express — becomes a
`TreatMissingData: breaching` alarm on the metric filter's own datapoint.

**Two Azure traps to carry across, not rediscover:**

- Container App **Jobs** left `ContainerAppName_s` empty and put the name in
  `ContainerJobName_s`; a rule written the obvious way parsed, ran, cost money
  and matched nothing, silently. Every query in `create-alerts.sh` was executed
  against the live workspace before being written down. The AWS equivalents get
  the same treatment: run each Logs Insights query against real log data before
  committing the alarm.
- stdout in these containers is block-buffered until the process exits, so
  stdout timestamps cluster at the moment a container died — which is why the
  sweep scripts write all progress to **stderr**. That property is the runtime's,
  not Azure's, and survives the move.

**One capability that does not survive, stated plainly.** The
`georag-foundry-cc-client-errors` and `-server-errors` alerts exist because
Foundry blocked 1,421 of 2,524 calls on 2026-08-17 and nothing noticed. Those
rules read **Azure platform metrics on the Foundry resource**. Calling
`api.cohere.com` directly, there is no cloud-side metric for that — AWS cannot
see a third-party API. The signal has to be re-created application-side: a
counter and a marker log line on non-2xx responses from every Cohere adapter,
then a metric filter and alarm. This is new code, and without it the migration
silently drops the one alert that caught a real, expensive, invisible outage.

Also carried forward, unchanged and still true: nothing in production measures
answer quality except `answer_quality_watch` reading `silver.answer_runs`; the
two `/metrics` endpoints remain unscraped; Pulse collects data nobody can view.
The move does not fix these and does not pretend to.

---

## 8. CD, registry, secrets

`.github/workflows/cd.yml` builds the fastapi, laravel and martin images with
`az acr build`, runs `laravel-migrate-job`, then rolls each app with
`az containerapp update --image`, with a best-effort rollback to the previous
digest. AWS shape:

- **ECR** + `docker buildx` with GitHub Actions layer cache. The current file
  notes `az acr build` exposes *no* cache options at all, so every deploy is a
  cold build; this is a straight improvement, not just a port.
- **OIDC** federated role in place of the Azure AD app registration — same
  no-stored-secret model.
- Migration job → **ECS RunTask** with a `php artisan migrate` override, polled
  to completion before any service rolls.
- Rollout → `aws ecs update-service --force-new-deployment` with the new task
  definition; ECS circuit breaker with rollback replaces the hand-rolled
  previous-digest rollback.
- Secrets → **Secrets Manager**, referenced by ARN in the task definition's
  `secrets` block, so no value passes through the workflow.

**The raw-SQL trap does not get to repeat.** CD runs migrations only.
`php artisan db:apply-raw` is a manual operator step, so everything created
solely in `database/raw/` has never existed on Azure — `scripts/raw-parity-baseline.txt`
lists what that still covers and names live code that queries it
(`routers/interpretation.py`, the phase0 ops agents, `app/agent/egress_gate.py`).
The AWS deploy job runs `db:apply-raw` after `migrate`, or the baseline file is
driven to empty first. Either is fine; leaving it as-is is not.

**`GEORAG_ENV=production` must be set** on every task definition. It gates
`main.py::_assert_production_posture`, the only thing that reports a security
control being off.

---

## 9. Everything else that names Azure

191 files match `azure|Azure|AZURE` outside `vendor/` and `node_modules/`. The
load-bearing ones:

- `.env.production.example` — rewritten wholesale.
- `charts/georag/` (Chart.yaml, values.yaml, templates/redis.yaml) — the on-prem
  chart; the Azure references in it are storage defaults.
- `ops/runbooks/azure-oncall.md` → `aws-oncall.md`; `ops/runbooks/secret-rotation.md`
  loses the Azure Files account-key hazard and gains IAM/Secrets Manager.
- `docs/adr/0019` (Parse on Foundry), `0020` (Blob replaces SeaweedFS in prod),
  `0021` (Foundry embed/rerank) — all superseded by the new ADR.
- CI `deployment-manifests` job (`scripts/check_redis_manifests.py`) — the Redis
  invariants it enforces (`volatile-lru` non-negotiable because one instance
  holds no-TTL queue jobs beside TTL'd cache) apply unchanged to an ECS task
  definition; the manifest list it scans changes.
- `app/Services/Azure/*`, the `Storage::extend('azure', …)` closure in
  `AppServiceProvider`, `league/flysystem-azure-blob-storage` — deleted last.

---

## 10. Sequencing

Build `deploy/aws/` alongside `deploy/azure/`; delete the Azure tree in a final
commit that carries its embedded reasoning — the probe rationale, the DST
double-fire trap, the measured alert thresholds, the `ContainerJobName_s` trap,
the rotation harness's failure-point contract — into the AWS equivalents or the
ADR rather than discarding it.

1. **ADR-0022** — the cloud move, superseding 0019/0020/0021.
2. **Cohere adapters.** Probe first (chat + parse), commit the report, then
   `cohere` backend values with loud rejection of `foundry`/`azure`, then tests.
3. **Storage.** IAM-chain credentials + endpoint resolution in
   `georag_object_storage`, Laravel `s3` disk, S3 versioning and replication.
4. **Infrastructure as code** for VPC / ALB / ECS / RDS / EFS / ECR / Secrets
   Manager / EventBridge / CloudWatch. Unlike Azure — where there is no Bicep,
   Terraform or ARM for the container apps and ~55 env vars per app drift freely
   from `.env.production.example` — this is written down from day one.
5. **CD workflow** rewrite, including `db:apply-raw`.
6. **Scheduler** on EventBridge; port the sweep tests; retire the parity checker
   in its Azure form.
7. **Observability**: log groups, metric filters, alarms, SNS — plus the new
   application-side Cohere error signal from §7.
8. **Docs**: `.env.production.example`, Ch 00/01/02/07/08/12, runbooks, CLAUDE.md
   technology snapshot.
9. **Delete `deploy/azure/`** and the Azure code paths.

Hard rules are unchanged by the cloud move: async-native drivers only in
FastAPI, Octane-safe Laravel, citations mandatory, no Streamlit, MapLibre not
Mapbox, no knowledge graph.

---

## 11. Open decisions

| # | Decision | Recommendation |
|---|---|---|
| 1 | Cohere route: (a) direct API, (b) Bedrock, (c) SageMaker | **(a)** — the only route with parity on all four capabilities; Bedrock carries neither Command A+ nor Parse |
| 2 | Postgres: RDS managed vs self-managed on EC2 | **RDS PG 18** — both extensions RDS lacks have zero call sites |
| 3 | Compute: ECS Fargate vs App Runner vs EKS | **ECS Fargate** — App Runner cannot host the non-HTTP workers |
| 4 | SPLADE++: sidecar, in-process, or drop the sparse leg | **Sidecar on Fargate** — `SPARSE_SERVICE_URL` is the supported path and in-process is the pattern that OOMed once |

---

## Sources for the [unverified in-region] claims

- [Cohere on AWS](https://docs.cohere.com/docs/cohere-on-aws) · [Cohere models on Amazon Bedrock](https://docs.cohere.com/docs/amazon-bedrock)
- [Introducing Parse](https://cohere.com/blog/parse) · [Cohere Parse 5 — InfoQ](https://www.infoq.com/news/2026/09/cohere-multimodal-parse/)
- [Announcing Command A+](https://docs.cohere.com/changelog/command-a-plus-05-2026) · [Compatibility API](https://docs.cohere.com/docs/compatibility-api)
- [RDS for PostgreSQL supported extensions](https://docs.aws.amazon.com/AmazonRDS/latest/PostgreSQLReleaseNotes/postgresql-extensions.html) · [RDS h3-pg support](https://aws.amazon.com/about-aws/whats-new/2023/09/amazon-rds-postgresql-h3-pg-geospatial-indexing/) · [Aurora HypoPG support](https://aws.amazon.com/about-aws/whats-new/2023/12/amazon-aurora-postgresql-hypopg-extension) · [RDS PostgreSQL 18](https://aws.amazon.com/about-aws/whats-new/2025/11/amazon-rds-postgresql-major-version-18/)
