# Chapter 02 — Data Stores

> **Reconciled 2026-09-07** against the compose file, the Postgres init
> scripts and migrations, `src/fastapi/scripts/init_qdrant.py`, the
> `georag_object_storage` package, `config/database.php` /
> `config/filesystems.php`, and the Azure manifests and runbooks under
> `deploy/azure/` and `ops/runbooks/`. Where dev and production differ, both
> are stated. Anything the repo does not record (Flexible Server SKU, the
> live Azure env values) is said to be unrecorded rather than guessed.
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


Four durable stores (PostgreSQL, Qdrant, Redis, object storage), one tile
generator (Martin) and one engine database (Hatchet). This chapter covers
what each is *for*, how it is configured, and how each service reaches it.
Table-level detail is in [Ch 03](03-schemas.md); row-level security in
[Ch 11](11-tenancy-and-rls.md); the containers themselves in
[Ch 01](01-services.md).

| Store | Dev (compose) | Production (Azure) |
|---|---|---|
| PostgreSQL 18 + PostGIS 3.6 | `georag/postgres:18-ext` behind PgBouncer 1.25.1 | Azure Database for PostgreSQL Flexible Server `georag-pg-cc`, no PgBouncer |
| Qdrant v1.17.1 | `qdrant` service, `qdrant_data` volume | `qdrant-cc` Container App on the `qdrant-storage` Azure Files share |
| Redis | `redis:8.6.4-alpine`, AOF on, `redis_data` volume | `redis-cc` running `redis:8.10.0-alpine`, AOF off, no volume |
| Object storage | SeaweedFS 4.35 as service `minio` (S3 API) | Azure Blob account `georagblobcc` (ADR-0020) |
| Tiles | `martin` 1.11.0 as `georag_app` | `martin-cc` as `martin_readonly` |
| Hatchet state | `hatchet` logical DB on the same Postgres | `hatchet-cc` against the same Flexible Server |

Gone: Neo4j (2026-07-28, hard rule 9), the Dagster `georag_dagster` state
DB, the Kestra `kestra` DB (dropped by
`database/raw/phase3/95-kestra-sunset.sql`), the backup agent and every
`backup_*` workflow (2026-08-23). ClickHouse exists only inside the optional
Langfuse overlay (§7).

---

## 1. PostgreSQL 18 + PostGIS 3.6 — the source of truth

### 1.1 Image and extensions

Dev runs `georag/postgres:18-ext`
([docker/postgresql/Dockerfile](../../../docker/postgresql/Dockerfile) on
`postgis/postgis:18-3.6-alpine`); see [Ch 01 §2](01-services.md#2-always-on-substrate)
for the container. Extensions are registered by the init scripts on first
initialisation, in lexical order — `10-phase0-extensions-and-schemas.sql`
runs **before** `init-postgis.sql`, which is why the phase-0 script creates
`postgis` and `pg_stat_statements` itself.

| Extension | Created by | Purpose |
|---|---|---|
| `postgis`, `postgis_topology` | `init-postgis.sql` (postgis also in `10-phase0`) | Geometry / geography; topology for boundary work |
| `postgis_raster`, `h3`, `h3_postgis` | `10-phase0` | Raster types; H3 hex indexing (`gold.h3_density_mineral`) |
| `pg_trgm` | `init-postgis.sql` | Trigram fuzzy match (hole IDs, formation names) |
| `uuid-ossp` | `init-postgis.sql` | `uuid_generate_v4()` for legacy defaults |
| `pg_stat_statements`, `auto_explain`, `pg_stat_kcache` | `10-phase0` (+ `shared_preload_libraries` in compose) | Query stats, slow-plan logging (> 2 s, JSON), kernel CPU/IO per query |
| `hypopg` | `10-phase0` | Hypothetical indexes for the Index Health agent |
| `pg_partman` (schema `partman`) | `10-phase0` | Monthly partitions on `audit.audit_ledger`, `workflow.workflow_runs`; advanced by the `pg_partman_maintenance` workflow |
| `pg_repack` | `10-phase0` | On-demand online reorg |
| `pg_ivm` | `10-phase0` | Incremental view maintenance — installed, unused |
| `pgcrypto` | Laravel migrations (`2026_08_17_*`) and `database/raw/phase5/20-per-flow-jwt-keys.sql` | HMAC + symmetric encryption under `AUDIT_ENCRYPTION_KEY` |
| ~~`vector`~~ | — | **Deliberately absent.** [ADR-0013](../../adr/0013-no-pgvector-postgres-extension.md): Qdrant is the sole vector store. |

Two things to know about this list:

- The init scripts only run on a fresh volume. For an existing dev volume
  use `scripts/phase0_apply_extensions.sh`. **They never run on Azure**:
  Flexible Server is provisioned by hand and its `azure.extensions`
  allow-list does **not** include `h3`, so `gold.h3_density_mineral` and
  `silver.density_choropleth_h3` stay raw-SQL-only and the H3 heatmap is
  capability-gated (architecture doc §06b).
- The verification notice at the end of `10-phase0` counts the namespace
  `public_geoscience`, which does not exist (the schema is `public_geo`,
  §1.5), so a clean first init always logs `7 / 8 expected namespaces`.

### 1.2 Connection paths

| Path | Role | Used by | Why |
|---|---|---|---|
| `pgbouncer:6432`, transaction pooling | `georag_app` | `laravel-octane`, `laravel-horizon`, `laravel-reverb`, `fastapi` runtime | Short-lived queries; the only path safe for asyncpg under pooling |
| `postgresql:5432` direct | `georag` (owner) | Laravel `pgsql_migrations` (`MIGRATE_DB_*`), `hatchet-worker` (`POSTGRES_USER`) | DDL needs session state; the worker needs transactions and RLS GUCs |
| `postgresql:5432` direct | `georag_app` | `fastapi` via `POSTGRES_DIRECT_HOST` (per-flow key loader's `set_config`), `martin` in dev | Transaction-local settings; Martin's persistent connections |
| `postgresql:5432/hatchet` | `hatchet` | `hatchet-lite`; Laravel still defines `pgsql_hatchet`, but the Worker Dashboard that used it is gone | Engine state |

`max_connections=200` in dev; PgBouncer multiplexes up to 1000 client
connections onto a pool of 50 ([Ch 01 §2](01-services.md#2-always-on-substrate)).

**Production has no PgBouncer.** Every task connects to RDS directly. The
instance class is `deploy/aws/terraform/`'s to state — unlike the Azure
Flexible Server, whose SKU and connection limit were not recorded in the
repository at all. None of the compose-side tuning (`shared_buffers`,
`work_mem`, `io_method`…) reaches the managed server; RDS applies its own
parameter group. Note also that `.env.production.example` sets `DB_USERNAME=georag`
(line 143) directly under a comment saying Laravel connects as
`georag_app`; compose ignores that key because the services set
`DB_USERNAME` from `GEORAG_APP_USER`, but the example is wrong as a
production template and the live Azure env is not in the repo.

### 1.3 Roles

| Role | Login | Defined in | What it is |
|---|---|---|---|
| `georag` | yes | `POSTGRES_USER` — the initdb bootstrap user | **Superuser in dev** by construction. Owns the `georag` DB and every schema. Migrations and the Hatchet worker connect as it. On Flexible Server no login is a Postgres superuser; what `georag` is there is not recorded. |
| `georag_app` | yes | [database/raw/phase1/10-georag-app-role.sql](../../../database/raw/phase1/10-georag-app-role.sql) | `NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE`. Runtime role for Laravel and FastAPI. USAGE on 13 schemas; SELECT/INSERT/UPDATE on their tables (DELETE only on `workspace.idempotency_keys` and `workspace.dry_run_outputs`); EXECUTE on the four `audit.*` hash-chain functions. The script's final block **raises** if the role is ever SUPERUSER or BYPASSRLS. |
| `georag_read`, `georag_write`, `georag_audit` | no (groups) | [docker/postgresql/init/init-roles.sql](../../../docker/postgresql/init/init-roles.sql) | Least-privilege groups: read = SELECT on silver/bronze/gold/public_geo; write inherits read + INSERT/UPDATE on silver/public; audit = INSERT on `audit.*`. **Nothing outside `database/` references them** — no connection string uses them. The header also names a `georag_admin` that is never created. |
| `hatchet` | yes | [docker/postgresql/init/20-hatchet-database.sql](../../../docker/postgresql/init/20-hatchet-database.sql) | Owner of the `hatchet` DB; password `hatchet` unless `HATCHET_DB_PASSWORD` is set. |
| `martin_readonly` | dev: no; Azure: yes | migration `2026_04_22_130000_create_silver_mvt_functions.php` (+ later MVT migrations) | `NOLOGIN NOINHERIT NOSUPERUSER`, EXECUTE on the `silver.pg_*` tile functions. On Azure `deploy/azure/containerapps/rotate-martin-credential.sh` gives it a password and `martin-cc` connects as it. In compose Martin still connects as `georag_app` (§5). |
| ~~`kestra`~~ | — | dropped by `database/raw/phase3/95-kestra-sunset.sql` | history |

`init-roles.sql` **is** inside the auto-init directory; an older note
saying it had to be applied by hand is obsolete. It guards the grants on
`public_geo` and `public.query_audit_log` because those objects come from
Laravel migrations and do not exist at Docker init time — re-run the file
after migrations on a fresh cluster to pick them up (the header gives the
command).

**Posture.** Runtime traffic is `georag_app` with RLS in force
([Ch 11 §3](11-tenancy-and-rls.md#3-the-role-split) and
[§10](11-tenancy-and-rls.md#10-bypassrls-is-forbidden)); 34 migrations
carry `FORCE ROW LEVEL SECURITY`, and `tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php`
fails when a tenant-scoped table lacks a policy. The owner role being a
dev superuser is mitigated, not closed: it is used only by migrations and
the worker, port 5432 is not published, and the role split proposed in the
older text (`georag_owner` / `georag_migrator`) has not been implemented.

### 1.4 Logical databases

| DB | Owner | Status |
|---|---|---|
| `georag` | `georag` | The application — every namespace in §1.5 |
| `hatchet` | `hatchet` | Hatchet engine state and its Postgres-backed queue (`SERVER_MSGQUEUE_KIND=postgres`) |
| `georag_dagster` | `georag` | **Still created** by `init-postgis.sql` on a fresh dev volume although Dagster was removed 2026-07-28. Empty and unused; a stale init leftover. |
| ~~`kestra`~~ | — | Dropped with Kestra |

### 1.5 Schema namespaces

Seventeen application schemas plus `public`. Ch 03 has the tables.

| Schema | Created by | Holds |
|---|---|---|
| `bronze`, `silver`, `gold`, `index` | `init-postgis.sql` | Medallion layers; `index` is the search-support layer and is lightly used |
| `audit` | `init-postgis.sql` and `10-phase0` | Hash-chained `audit_ledger`, `query_audit_log`, verification runs |
| `usage`, `outbox`, `workflow`, `workspace` | `10-phase0` | Usage events and cost ceilings; the outbox; Hatchet run mirrors; tenancy spine |
| `partman` | `10-phase0` | pg_partman's own catalog |
| `topology` | `postgis_topology` extension | PostGIS topology objects |
| `public_geo` | migration `2026_04_14_000000_create_public_geoscience_schema` | Government reference layers. The rename to `public_geoscience` was locked in design and never applied — the migration, Martin config and `georag_app` grants all say `public_geo`. |
| `targeting` | `2026_05_13_100000` | Target scoring |
| `eval` | `2026_05_13_140000` | Golden-query and eval artefacts |
| `ops` | `2026_05_13_140100` | Support / ops tables |
| `interpretation` | `2026_08_28_100400` (+ `database/raw/phase0/107-*`) | Geological interpretation |
| `backups` | `2026_08_28_100700` (+ `database/raw/phase0/103-*`) | `backups.snapshot_runs`; read by the admin router, no writer runs (§8) |

Default `search_path` for the `georag` DB is
`silver, bronze, gold, index, audit, public` (set in `init-postgis.sql`).

### 1.6 Durability

The base compose has **no WAL archiving**. The `docker/compose.wal-archiving.yml`
overlay adds a `pg_wal_archive` volume and the `Z_activate_wal_archiving.sql`
init hook, but its drain side depended on the backup agent deleted
2026-08-23, and nothing invokes `docker/postgresql/backup.sh` or
`wal-upload.sh` any more. Production durability is Azure Flexible Server's
automated backups with 35-day point-in-time restore
([Ch 14](14-status-matrix.md)). See §8.

---

## 2. Qdrant v1.17.1 — the vector index

### 2.1 Where it runs

- **Dev**: the `qdrant` service ([Ch 01 §4](01-services.md#4-dev-data--domain-service-model-sidecars-stores-hatchet)),
  data on the `qdrant_data` volume, cluster-level HNSW `m=32`,
  `ef_construct=256`, `ef=200`, `max_indexing_threads=4`, WAL 256 MiB per
  collection, no API key. (`.env.production.example` still comments
  "HNSW m=16, ef_construct=200 at service level" and sets
  `QDRANT_HNSW_M=16`; the compose values above are what runs.)
- **Azure**: `qdrant-cc`, data directory on the `qdrant-storage` Azure
  Files share (SMB, TransactionOptimized) in the `georagblobcc` account,
  mounted with the storage account key — rotating that key breaks the
  mount on the next restart (`ops/runbooks/secret-rotation.md`). Share
  quota exhaustion surfaces as Qdrant's *"Not enough space available for
  optimization"* and there is a `qdrant-cc-optimizer-stuck` alert plus a
  transactions-volume alert (`deploy/azure/alerts/create-alerts.sh`).
  Clients reach it through the app's internal ingress, so they set
  `QDRANT_HTTPS=true` and `QDRANT_PORT=443` (`app/config.py`). Auth is
  on: `QDRANT__SERVICE__API_KEY` on `qdrant-cc`, `QDRANT_API_KEY` on
  `fastapi-cc` and `hatchet-worker-cc` (one read-write key; rotation
  procedure in secret-rotation §6).

### 2.2 Collections

Collections are **not** created by FastAPI at startup. They are
bootstrapped by [src/fastapi/scripts/init_qdrant.py](../../../src/fastapi/scripts/init_qdrant.py)
(idempotent, plain HTTP, run by an operator), and FastAPI's lifespan step
5b only checks that the live dense dimension matches `EMBEDDING_DIMENSION`
— on a confirmed mismatch it disables the embedding model so search
refuses rather than querying the wrong vector space.

| Collection | Dense | Sparse | Payload on disk | Payload indexes | Status |
|---|---|---|---|---|---|
| `georag_chunks` | unnamed `""` slot, 1024-dim (`EMBEDDING_DIMENSION`, or `AZURE_FOUNDRY_EMBED_DIMENSION` when `EMBEDDING_BACKEND=foundry`), Cosine | named `text` slot, SPLADE++ | yes | `workspace_id`, `project_id`, `report_id`, `section_number` — all `keyword` | **Live.** The only collection retrieval reads. |
| `georag_reports` | 384-dim (bge era), Cosine | `text` | no | `workspace_id`, `report_id`, `section_number` (integer), `commodity` | **Legacy.** Still in the init script; `RETRIEVAL_USE_DOCUMENT_PASSAGES=true` sends `search_documents` to `georag_chunks`. The drop was deferred (`app/config.py` note). |

Point payload written by `app/services/ingest/passage_embedder.py`:
`report_id`, `project_id`, `workspace_id`, `section_number`,
`section_title`, `text`. `text` and `workspace_id` are asserted on every
batch and re-read from a freshly written point, because the 2026-06-01
outage was writers silently producing minimal payloads. The hourly
`qdrant_payload_audit` workflow re-checks live points.

**No quantization is configured anywhere live.** The compose comment and
the previous version of this chapter said scalar quantization was set per
collection in the Dagster indexers; those modules were deleted with the
Dagster tree on 2026-08-28 and `init_qdrant.py` sets none.

**No full-text payload index on `text`.** `app/services/identifier_boost.py`
documents that its exact-identifier `MatchText` branch needs one and that
nothing in the live tree creates it; the boost is therefore held inert and
`tests/test_identifier_boost_is_inert.py` pins that.

### 2.3 Who reads and writes

| Direction | Code | Notes |
|---|---|---|
| Write | `passage_embedder` via the `embed_pending_passages` workflow — crons `45 5 * * *` and `*/10 * * * *`, plus a dispatch from `ingest_pdf` after persist (the module docstring still says the cron was omitted; the decorator is the truth) | Dense from the configured backend, sparse from the `sparse` sidecar or in-process SPLADE, then `silver.document_passages.embedding_id` is back-filled |
| Write | `nl_summaries` workflow | One synthetic passage per structured row (ADR-0012); registered, not scheduled |
| Read | `tools.search_documents` → `app/services/qdrant_service.hybrid_query` | `workspace_id` filter is mandatory on every query — the tenancy contract for this store ([Ch 11](11-tenancy-and-rls.md)) |
| Audit | `qdrant_payload_audit` (hourly), `store_reconciliation_run` agent | Payload shape and cross-store counts |

Switching embedding models (bge → Qwen3 in June, Qwen3 → Cohere Embed v4
on Foundry) keeps the 1024-dim schema but still requires a full re-embed:
`scripts/reset_embeddings_for_reencode.py` clears `embedding_id` so the
embed workflow re-processes everything. Per-store wait budgets on the
query path are `TIMEOUT_QDRANT_S` (2 s in compose, 6 s code default) and
the separate reranker timeout.

---

## 3. Redis — cache, sessions, queues, and a few FastAPI buffers

### 3.1 Instances

| | Dev | Azure `redis-cc` |
|---|---|---|
| Image | `redis:8.6.4-alpine` | `redis:8.10.0-alpine` |
| Memory | `maxmemory 512mb`, `volatile-lru` | `maxmemory 384mb`, `volatile-lru` |
| Persistence | AOF on (`everysec`), `redis_data` volume | **AOF off, no volume** — contents are lost on every restart and on the nightly scale-to-zero |
| Sizing | 1 CPU / 1 GiB limit | 0.25 vCPU / 0.5 GiB |
| Apply | compose | `apply-redis.sh` only — the YAML carries a `REPLACE_AT_DEPLOY_TIME` secret placeholder |

Self-hosted rather than Azure Cache for Redis so the shutdown scheduler can
stop it overnight. `volatile-lru` is non-negotiable because one instance
holds queue jobs (no TTL) beside TTL'd cache and sessions;
`scripts/check_redis_manifests.py` enforces the invariants on every Redis
manifest in the repo.

### 3.2 Database map (`config/database.php`)

| DB | Laravel connection | Holds |
|---|---|---|
| 0 | `default`, `queue`, `session` (`REDIS_DB`, `REDIS_QUEUE_DB`, `REDIS_SESSION_DB`) | Horizon supervisor state, queue jobs on `default` and `llm`, sessions |
| 1 | `cache` (`REDIS_CACHE_DB`) | Laravel cache |
| 2, 3 | — | spare (`--databases 4`) |

`CACHE_STORE`, `SESSION_DRIVER` and `QUEUE_CONNECTION` are all `redis` in
both env examples; the framework defaults (`database`) never apply. Client
is phpredis; keys are prefixed `<app-name>-database-`.

Because the Azure instance is ephemeral, a restart logs every user out
and drops any queued Horizon job. The three Horizon jobs are short and
user-triggered ([Ch 07](07-orchestration.md)), so this was accepted.

### 3.3 FastAPI and the worker (`redis.asyncio`, hard rule 2)

- `services/workspace_resolution.py` — project → workspace read-through
  cache, 5-minute TTL.
- `agent/event_stamper.py` — per-answer-run ring buffer of SSE events so
  a reconnecting client can replay with idempotency keys.
- `agents/runtime.py` and the Phase 0 agents — run coordination.
- `hatchet_workflows/external_notification.py` — per-sender rate-limit
  token bucket; `cost_burn_watcher`, `restore_workspace`, export extras.
- `TIMEOUT_REDIS_S=0.5` bounds every call on the query path.

---

## 4. Object storage — SeaweedFS in dev, Azure Blob in production

One `STORAGE_BACKEND` switch, read in two places with the same two values:
`src/georag_object_storage/georag_object_storage/factory.py` for Python
(`s3_compatible` default, `azure_blob`) and `config/filesystems.php` for
Laravel (driver `s3` or `azure`). `.env.example` sets `s3_compatible`;
`.env.production.example` sets `azure_blob`. [ADR-0001](../../adr/0001-seaweedfs-replaces-minio.md)
now covers only the compose / on-prem half; [ADR-0020](../../adr/0020-azure-blob-replaces-seaweedfs-in-production.md)
records the production half.

### 4.1 Buckets and containers

The package's `Bucket` enum has four members; the container/bucket name
for each comes from env with these defaults:

| `Bucket` | Env | Default name | Purpose |
|---|---|---|---|
| `BRONZE` | `AZURE_STORAGE_CONTAINER_BRONZE` / `S3_BUCKET_BRONZE` | `bronze` | Raw uploads and bronze artefacts |
| `BRONZE_RASTER` | `…_BRONZE_RASTER` | `bronze-raster` | Rendered page images and rasters |
| `EXPORTS` | `…_EXPORTS` | `exports` | Generated exports and reports |
| `BACKUPS` | `…_BACKUPS` | `georag-backups` | No writer since 2026-08-23 |

`minio-init` also creates `tier-hot`, `tier-warm`, `tier-cold` in dev.
They are outside the enum; the only code naming one is the support-packet
agent, which writes to a literal `tier-warm` that no Azure container
matches (its own comment says so). Treat the tier buckets as dev-only.

Laravel disks: `s3` (default bucket from `AWS_BUCKET` / container from
`AZURE_STORAGE_CONTAINER_BRONZE`), `s3-bronze`, `s3-exports`, plus the
framework `local` and `public`. `FILESYSTEM_DISK` is `local` in dev and
`s3` in production.

### 4.2 Credentials and endpoints

- **Dev**: `S3_ENDPOINT=http://minio:8333`, `S3_ACCESS_KEY` /
  `S3_SECRET_KEY`; the `MINIO_*` and `AWS_*` names carry the same values
  for older code paths. SeaweedFS serves S3 on 8333, the filer UI on 8888,
  the master on 9333 (healthcheck only). Container and DNS name stay
  `minio` on purpose.
- **Azure**: `AZURE_STORAGE_CONNECTION_STRING` *or*
  `AZURE_STORAGE_ACCOUNT_URL` for managed identity
  (`AZURE_STORAGE_AUTH_MODE`, `AZURE_STORAGE_ACCOUNT_NAME=georagblobcc`).
  Per `deploy/azure/README.md`, blob read/write traffic is managed
  identity on every tier; the account key survives only because
  Laravel's `temporaryUrl()` signs download SAS links with it, so
  `allowSharedKeyAccess` is still enabled on the account.

### 4.3 Bronze store

`app/services/bronze_store.py` gives the §04p PDF pipeline a key/value
interface with two implementations: a local directory (`BRONZE_LOCAL_DIR`,
`/tmp/georag/bronze` in compose) and the object-storage backend (URIs of
the form `s3://bronze/<key>`). [Ch 04](04-ingestion-flow.md) shows where
the `parse` step uses it. The older key-pattern table in this chapter was
not backed by code and has been removed; the key layout is whatever the
callers in `app/services/ingest/` pass.

### 4.4 Durability

Blob is LRS: three replicas in one datacentre. That covers hardware
failure, not deletion, and there is no second copy. It is the one store
that cannot be rebuilt from another (§8).

---

## 5. Martin — the tile path into Postgres

Not a store, but the one other process with a direct database connection.
[Ch 09](09-martin-and-maplibre.md) covers the sources and the Laravel
proxy; the facts that belong here:

- [docker/martin/martin.yaml](../../../docker/martin/martin.yaml): 20
  function sources in `silver` (`pg_*_by_project`, each `(z, x, y, query)`
  → MVT) and 9 table sources — `public.smdi_deposits` plus eight
  `public_geo.v_pg_*_mvt` views. `pool_size: 20`, `worker_processes: 2`,
  `cache_size_mb: 512`, `auto_publish: false`.
- Connects direct to `postgresql:5432` (persistent connections; PgBouncer
  transaction pooling would break it). Dev uses `georag_app`, which is
  wider than needed; Azure uses `martin_readonly` (§1.3), whose EXECUTE
  grants cover the tile functions only.
- The Azure migration comment counts the functions it found live at 18
  and the manifest header says 24; the config file lists 20. Reconcile
  against the live catalog before trusting any of the three.

---

## 6. Hatchet engine state

`hatchet-lite` keeps workflow runs, steps and its message queue in the
`hatchet` logical DB (§1.4). App code never queries it. Laravel still
defines a `pgsql_hatchet` connection for the Hatchet Worker Dashboard, but
that controller and route no longer exist, so the connection has no
consumer. Application-side mirrors of runs live
in the `workflow` schema of the `georag` DB and are written by the
workflows themselves ([Ch 07](07-orchestration.md)).

---

## 7. Langfuse overlay (ClickHouse) — optional, dev only

`docker/compose.langfuse.yml` adds `langfuse-web` and `langfuse-worker`
(Langfuse 3), a `clickhouse-server:26.3-alpine` trace store, and an init
container. Every app service already carries `LANGFUSE_HOST` /
`LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` pointing at
`langfuse-web:3000`; empty keys disable the SDK. Nothing under
`deploy/azure/` provisions Langfuse, so production tracing is Azure
Monitor ([Ch 12](12-observability.md)).

---

## 8. Backup and recovery, as built

The nightly `backup_postgres` / `backup_qdrant` / `backup_redis` /
`backup_seaweedfs` workflows were deleted on 2026-08-23 (they wrote to a
SeaweedFS that does not exist on Azure and had failed every night since
the migration; `backup_neo4j` went 2026-08-19). Ofelia and the backup
agent went with them. What remains:

| Store | Recovery story | Evidence |
|---|---|---|
| PostgreSQL | RDS automated backups, 35-day PITR. No repo-side dump or WAL upload runs. | [Ch 14](14-status-matrix.md); `docker/postgresql/backup.sh` and `wal-upload.sh` have no caller |
| Qdrant | Derived data: reset `embedding_id` and let `embed_pending_passages` rebuild from `silver.document_passages` | `scripts/reset_embeddings_for_reencode.py` |
| Redis | AOF on an EFS volume since 2026-09-08. The Azure app had `--appendonly yes` with **no volume**, so every nightly restart dropped sessions and any queued Horizon job | `deploy/aws/terraform/services.tf`; `scripts/check_redis_manifests.py` |
| Object storage | S3 versioning with 90-day non-current retention since 2026-09-08. On Azure it was the one irreplaceable copy: LRS only, no backup workflow, no restore procedure | `deploy/aws/terraform/`; ADR-0022 |
| `backups.snapshot_runs` | Table exists and the admin router lists it; no workflow writes to it | `app/routers/admin_tier234.py` |

`ops/runbooks/aws-oncall.md` states plainly that **nothing here has been
restore-tested**. The two rows above are more mechanism than Azure ever
had, and a mechanism that should work is not a restore procedure. That is
the current posture, accepted deliberately; treat a Postgres PITR drill as
the highest-value gap in this chapter.
