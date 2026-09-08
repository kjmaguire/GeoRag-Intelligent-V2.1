# ADR 0022: AWS replaces Azure as the production cloud

- **Date**: 2026-09-08
- **Status**: Accepted
- **Deciders**: Kyle Maguire (SME)
- **Supersedes**: ADR-0019 (Cohere Parse v5 on Azure AI Foundry replaces Azure
  Document Intelligence — the model choice stands, the host does not),
  ADR-0020 (Azure Blob replaces SeaweedFS in production),
  ADR-0021 (Foundry Embed v4 / Rerank v4 replace the self-hosted models — the
  decision to use hosted Cohere models stands, the host and one model version
  do not). ADR-0001 (SeaweedFS in compose) is **unaffected**: it covers the dev
  and on-prem half, which does not move.
- **Related**: `deploy/aws/MIGRATION-PLAN.md` (the full migration plan, with
  the service-by-service mapping and the evidence behind each claim),
  `docs/architecture/manual/` Ch 00 §3, Ch 01, Ch 02, Ch 07 §5, Ch 08, Ch 12.

## Context

Production has run on **Azure Container Apps** since 2026-07-30: nine container
apps in resource group `georag`, an Azure Database for PostgreSQL Flexible
Server, an Azure Blob storage account, and an Azure AI Foundry resource serving
the LLM, embeddings, reranking and OCR. The Azure credits funding it are gone,
so the platform has to run somewhere else.

**No data migration is required.** Nothing in the Azure Postgres, Qdrant, Redis
or Blob storage needs to come across; this is a fresh deployment of the code.
That removes the item that normally dominates a move of this kind. Switching
the embedding backend usually forces a full re-embed of every passage — the
point of no return in ADR-0021's migration mechanics — but with no corpus to
preserve there is nothing to re-embed, and the `georag_chunks` collection can
be recreated at whatever dimension the chosen model needs, for free.

Two things constrained the choice:

1. **Every LLM-family capability stays on Cohere models.** Chat is Command A+
   (`Cohere-command-a-plus-05-2026`), embeddings Embed v4, reranking Rerank v4,
   scanned-page OCR Parse 5. The question was only how they are reached from
   AWS, not whether the vendor changes.
2. **SPLADE++ has no managed equivalent anywhere**, including on Cohere. It had
   no Foundry counterpart and has no AWS counterpart. It is self-hosted or the
   sparse leg of hybrid retrieval does not exist.

A third constraint turned out not to bind. `docker/postgresql/init/*.sql`
creates fourteen extensions, and on Azure Flexible Server `h3` sat outside the
`azure.extensions` allow-list and was a known problem — so managed Postgres on
AWS looked like the largest technical risk. Auditing each extension against its
call sites in this repository dissolved it; see the Postgres decision below.

## Options considered — reaching Cohere from AWS

Three of the four adapters already speak Cohere's **native v2 API**
(`/providers/cohere/v2/{embed,rerank,parse}` with an `api-key` header): Foundry
was acting as a proxy. Only the chat path goes through Foundry's own
OpenAI-compatible surface. That shaped the comparison.

| Option | Command A+ | Embed v4 | Rerank | Parse 5 | Auth | Adapter work |
|---|---|---|---|---|---|---|
| A. **Cohere API direct** (`api.cohere.com`) | yes | yes | **v4** | yes | API key | base URL + header + model id ×4 |
| B. **Amazon Bedrock**, serverless only | **no** — catalogue is Command R/R+, legacy | yes | **3.5** | **not offered** | IAM / SigV4 | full rewrite ×2; two capabilities unserved |
| C. **Cohere on SageMaker** | yes | yes | v4 | yes | IAM / SigV4 | full rewrite ×4 + endpoint ops |
| D. **Bedrock + Bedrock Marketplace** | yes, via Marketplace endpoint | yes, serverless | **3.5** | yes, via Marketplace endpoint | IAM / SigV4 | full rewrite ×4 |

Option A was recommended: full version parity in one hop, the smallest diff,
and cloud-agnostic — worth something given that this migration exists because a
cloud's credits ran out. Option B alone is not implementable, because Bedrock's
serverless Cohere catalogue carries neither Command A+ nor any Parse model.

**Bedrock Marketplace** closes that gap. It subscribes a model and deploys it to
a SageMaker-managed endpoint that is then invoked through Bedrock's own APIs, so
chat and OCR keep the same auth model and SDK as embeddings and reranking.

## Decision

**Production moves to AWS.** Four decisions, taken 2026-09-08:

| # | Decision | Chosen | Recommended |
|---|---|---|---|
| 1 | Cohere route | **Amazon Bedrock** (option D) | option A |
| 2 | PostgreSQL | **RDS for PostgreSQL 18** | same |
| 3 | Compute | **ECS Fargate** | same |
| 4 | SPLADE++ | **Self-hosted Fargate sidecar** | same |

### 1. Cohere via Amazon Bedrock

| Capability | Bedrock surface | Model |
|---|---|---|
| Embeddings | serverless `InvokeModel` | Embed v4, 1024-dim output |
| Reranking | serverless `Rerank` | **Rerank 3.5** |
| Chat | Marketplace endpoint, `Converse` / `InvokeModel` | Command A+ |
| OCR / parse | Marketplace endpoint, `InvokeModel` | Parse 5 |

Taken against the recommendation, deliberately. What it buys: one auth model
(IAM/SigV4) and one SDK across all four capabilities, AWS-consolidated billing,
no third-party egress dependency, and — the point that is easy to miss — the two
Foundry error alerts port to `AWS/Bedrock` CloudWatch metrics with their
measured thresholds intact, instead of having to be rebuilt application-side.
Those alerts exist because Foundry blocked 1,421 of 2,524 calls on 2026-08-17
and nothing noticed.

`LLM_BACKEND` gains `bedrock`; `EMBEDDING_BACKEND`, `RERANKER_BACKEND` and
`OCR_ENGINE` likewise, and their defaults change to `bedrock` in the same
commit. `foundry` and `azure` become **hard startup errors naming the
replacement** for one release, then are deleted — extending the
`services/ingest/ocr_engine.py` retired-value pattern rather than adding a
silent new path. That pattern exists because a silent downgrade of exactly this
shape already happened once (2026-08-21).

### 2. RDS for PostgreSQL 18

Of the fourteen extensions the init scripts create, the audit against call
sites in this repository found:

| Extension | RDS PG 18 | Call sites | Verdict |
|---|---|---|---|
| `postgis`, `postgis_topology`, `postgis_raster` | yes | schema-wide | keep |
| `h3`, `h3_postgis` | yes (h3-pg added for PG 18) | `gold.h3_density_mineral.h3_index` | keep |
| `pg_partman` | yes | `audit.*` monthly partitions | keep |
| `pg_trgm`, `pg_stat_statements`, `uuid-ossp` | yes | indexes, `index_health` | keep |
| `hypopg` | yes | `agents/phase0/index_health.py` | keep |
| `pg_repack` | yes | one advisory string | keep, unused |
| `auto_explain` | parameter, not extension | **none** | parameter group |
| `pg_ivm` | **no** | **none** | **drop** |
| `pg_stat_kcache` | **no** | **none** | **drop** |

Both extensions RDS lacks have zero readers. `pg_ivm` is consistent with Ch 02:
the only materialised view in the schema is `silver.mv_collar_summary`, and
`mv_refresh_silver` refreshes it with a plain `REFRESH MATERIALIZED VIEW`. So
the migration drops two unused extensions and moves one diagnostic into a
parameter group, and the extension risk does not decide the service.

PgBouncer stays compose-only, as it already was on Azure; asyncpg already runs
`statement_cache_size=0`, so RDS Proxy drops in cleanly if connection counts
ever justify it. Not day one.

### 3. ECS Fargate

Nine services plus the sparse sidecar, per-service task definitions, ALB in
front of Octane and Reverb (stickiness on for Reverb's WebSockets), Cloud Map
for internal discovery, EFS for Qdrant and Redis. App Runner was rejected
because Horizon, the Hatchet engine and the Hatchet worker are not HTTP request
workloads; EKS because this deployment is nowhere near the scale that justifies
a control plane.

Two shapes carried across deliberately rather than by default: the Hatchet
worker stays at desired 1, because several workflows are `max_runs=1`
singletons; Octane goes to desired **2**, because at 1 every deploy and task
replacement is a user-visible outage on the only public app, and the cost
objection recorded in `deploy/azure/README.md` was already answered there on the
evidence (`max_connections` 429 against a 24 h peak of 99; Octane opens PDO
connections lazily per worker; session, cache and queue are all Redis).

### 4. SPLADE++ as a Fargate sidecar

~440 MB, CPU only, roughly 0.5 vCPU / 2 GiB. `SPARSE_SERVICE_URL` already
exists and is the supported path (`services/sparse_encoder.py`). The in-process
fallback works and needs no infrastructure, but it loads one copy per uvicorn
worker — the pattern that OOM-killed the container on 2026-06-24 with the
2.4 GiB embedder. Hybrid retrieval keeps both legs.

### Everything else

- **Object storage → S3** with IAM task roles. `STORAGE_BACKEND=s3_compatible`
  already covers it on both layers, so this is mostly configuration — but not
  entirely; see the gotchas.
- **Registry → ECR**, **secrets → Secrets Manager**, **identity → IAM task
  roles**, **scheduling → EventBridge Scheduler**, **observability →
  CloudWatch Logs, metric filters, alarms and SNS**.
- `GEORAG_ENV=production` is set on every task definition. It gates
  `main.py::_assert_production_posture`, the only thing that reports a security
  control being off.

## Gotchas hit (worth knowing for next time)

1. **The brief's Qdrant claim was wrong, and the real defect was elsewhere.**
   Qdrant did have persistent storage on Azure — an Azure Files share. Its
   actual defects were being mounted with the storage account key (rotation
   breaks the mount on next restart) and a fixed quota whose exhaustion stalled
   the optimiser and generated 10.8 M storage transactions in a day. The store
   with *no* persistence was **Redis**: AOF off, no volume, so every restart and
   every nightly scale-to-zero dropped sessions and queued Horizon jobs. EFS
   plus AOF on fixes both. Read Ch 02 before trusting a summary of it.
2. **Object storage is not pure configuration.** `StorageConfig.from_env()`
   marks `access_key` and `secret_key` `required=True`, so it raises before
   boto3 can use the ECS task-role credential chain; and `endpoint_url` defaults
   to `http://minio:8333` and is always passed to the client, so it must resolve
   to `None` for real S3. Two small load-bearing changes a config-only migration
   would have missed.
3. **Backend defaults selected a host that would not exist.** `EMBEDDING_BACKEND`
   and `RERANKER_BACKEND` default to `foundry` in code *and* in compose. On an
   AWS task an unset value fails at first call rather than at startup — the same
   class of failure ADR-0021 gotcha 1 records in the other direction.
4. **Rerank drops a major version.** Bedrock serves Rerank 3.5, not v4.
   `RERANKER_SCORE_THRESHOLD_FOUNDRY = 0.2` was measured against v4's calibrated
   scores and is the system's only retrieval-quality floor (hard rule 5, as
   built). It is re-measured on the golden set, not carried over.
5. **Marketplace endpoints do not scale to zero.** They bill for SageMaker
   compute for as long as they exist, so the nightly sweeps have to delete and
   recreate them — slower and more failure-prone than stopping a container, and
   a failed recreate means no chat at all rather than a degraded path.
6. **The DST double-fire mechanism disappears.** Container Apps Jobs have no
   timezone support, so each sweep fired at both candidate UTC hours with an
   in-script guard exiting 0 on the wrong one, and
   `scripts/check_scheduler_job_parity.py` existed to keep cron and guard
   agreeing. EventBridge Scheduler is timezone-native: one schedule each, no
   guard. The parity checker's *other* half — that the deployed inline script
   matches the reviewed file — is still needed, because an ECS RunTask override
   carries the same script twice for the same reason.
7. **Container App Jobs left `ContainerAppName_s` empty**, putting the name in
   `ContainerJobName_s`; a log rule written the obvious way parsed, ran, cost
   money and matched nothing, silently, forever. Every query in
   `deploy/azure/alerts/create-alerts.sh` was executed against the live
   workspace before being written down. The AWS equivalents get the same
   treatment: run each Logs Insights query against real log data before
   committing the alarm.
8. **stdout is block-buffered until the process exits** in these containers, so
   stdout timestamps cluster at the moment a container died. That is why the
   sweep scripts write all progress to stderr. It is a property of the runtime,
   not of Azure, and it survives the move.
9. **CD ran migrations only.** `php artisan db:apply-raw` was a manual operator
   step, so everything created solely in `database/raw/` has never existed on
   Azure while live code queries several of those objects
   (`scripts/raw-parity-baseline.txt`). The AWS deploy job runs `db:apply-raw`
   after `migrate` so the trap does not repeat.
10. **Bronze was the one irreplaceable copy**, with no backup workflow and no
    restore procedure — the `backup_*` workflows were deleted 2026-08-23 and
    Azure PITR does not cover Blob. On S3 the fix is configuration, so this
    migration takes it rather than carrying the gap across.

## Consequences

### Positive

- Runs on a cloud that is paid for.
- One auth model (IAM) for compute, storage, secrets, scheduling and every
  model call; no static keys in the model path at all.
- Bedrock publishes `InvocationClientErrors` / `InvocationServerErrors` /
  `InvocationThrottles` per model, so the Foundry error alerts port directly
  with their measured thresholds.
- **Redis gets persistence** (EFS + AOF on) for the first time in production.
- **Qdrant loses two failure classes**: EFS is elastic, so no quota wall, and
  IAM-authorised, so no account-key mount to break on rotation.
- **Bronze gets versioning, lifecycle and replication** — the first backup
  posture object storage has had.
- The nightly schedule collapses from two crons plus a DST guard plus a parity
  checker to one timezone-aware schedule per sweep.
- S3 presigned URLs are native to the task role, so Azure's
  `allowSharedKeyAccess` — enabled only because Laravel's `temporaryUrl()` signs
  with the account key and `microsoft/azure-storage-blob` ^1.1 has no
  user-delegation SAS — has no successor and the wart disappears.
- Infrastructure is written down. On Azure there is no Bicep, Terraform or ARM
  template for the container apps, so ~55 environment variables per app are set
  by hand and drift freely from `.env.production.example`.

### Negative

- **Reranking regresses from v4 to 3.5**, with a threshold that must be
  re-measured and a retrieval quality change that is not yet quantified.
- **Two always-on SageMaker endpoints** for chat and OCR, billed whenever they
  exist, with delete/recreate wired into the nightly sweeps and a new failure
  mode — a failed recreate leaves chat and OCR dead — that needs its own alarm
  on endpoint `InService` state, because there are no invocations to fail.
- Four adapter rewrites instead of a base-URL swap, each needing its wire
  contract verified empirically before it is trusted.
- **The route depends on Command A+ and Parse 5 being subscribable in Bedrock
  Marketplace in the target region**, which could not be verified from the
  session that wrote this. If they are not, the fallback is the declined hybrid:
  chat and parse on `api.cohere.com`, embeddings and reranking left on Bedrock.
- Vendor lock-in moves rather than reduces: Foundry-shaped coupling becomes
  Bedrock-shaped coupling, which is the thing option A would have avoided.

### Neutral / unchanged

- The dev stack does not move. Compose still runs SeaweedFS, the three model
  sidecars and local Postgres; ADR-0001 and ADR-0018 stand.
- The on-prem Helm chart at `charts/georag/` stands.
- `georag_chunks` stays 1024-dim dense + SPLADE++ sparse. Embed v4 is asked for
  1024-dim output, as on Foundry, so the collection schema is unchanged — and
  with no corpus, a dimension change would have been free anyway.
- Every hard rule in CLAUDE.md is unaffected by the cloud move: async-native
  drivers only in FastAPI, Octane-safe Laravel, citations mandatory, no
  Streamlit, MapLibre not Mapbox, no knowledge graph.

## Verification

- A committed wire-contract probe report for chat and parse before either
  adapter is trusted. Parse's wire shape was **never** empirically verified even
  on Foundry; `ops/validation/cohere_parse_probe.sh` was built for exactly this.
- `RERANKER_SCORE_THRESHOLD_FOUNDRY` re-measured against Rerank 3.5 on the
  golden set before traffic is flipped.
- Backend-selection tests extended to pin the new defaults and the loud
  rejection of `foundry` / `azure`, in the shape of
  `src/fastapi/tests/test_backend_selection.py`.
- Each CloudWatch Logs Insights query run against real log data before its alarm
  is committed (gotcha 7).
- The sweep behaviour tests (`deploy/azure/containerapps/scripts/tests/run.sh`)
  and the APP_KEY rotation harness ported, not deleted: they pin what happens at
  every failure point, and there is still no staging environment to rehearse on.

## Follow-ups (not part of this ADR)

- Quantify the Rerank 3.5 versus v4 retrieval difference on the golden set. As
  ADR-0021 records, there is still no recorded golden-set comparison between the
  dev and production model stacks either.
- Nothing in production measures answer quality except `answer_quality_watch`
  reading `silver.answer_runs`; the two `/metrics` endpoints remain unscraped in
  both environments; Laravel Pulse still collects data nobody can view in
  production. The cloud move neither fixes nor worsens any of these.
- `gold.h3_density_mineral` still has no writer.
- The `integration`, `golden`, `hallucination` and `live` markers — 377 tests,
  ~12% of the Python suite — run in no workflow at all. A green PR suite is not
  full coverage, during this migration least of all.
