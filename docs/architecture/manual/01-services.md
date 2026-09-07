# Chapter 01 — Services Catalog

> **Reconciled 2026-09-07** against `docker-compose.yml` on `main`. The
> compose file defines 16 services and nothing else runs in dev. Line
> references are the `<service>:` key's line on that date; the profile,
> image and port facts were read from the service blocks, not from the
> header comment (which is only partly corrected — see §8). Verify the
> service list any time with `docker compose config --services`.

Companion chapters: [Ch 00](00-overview.md) for the profile map and the
production topology, [Ch 02](02-data-stores.md) for what lives inside
each store, [Ch 07](07-orchestration.md) for the two orchestrators,
[Ch 14](14-status-matrix.md) for per-component status.

---

## 1. Tier overview

| Tier | Service | Profile | Image | Host port | Azure Container App |
|---|---|---|---|---|---|
| Data (always on) | `postgresql` | *(none)* | `georag/postgres:18-ext` (local build) | none | `georag-pg-cc` (Azure Database for PostgreSQL Flexible Server, not a container) |
| Data (always on) | `pgbouncer` | *(none)* | `edoburu/pgbouncer:v1.25.1-p0` | `6432` | none — apps connect to Flexible Server directly |
| Data (always on) | `redis` | *(none)* | `redis:8.6.4-alpine` | `6379` | `redis-cc` |
| Tiles (always on) | `martin` | *(none)* | `ghcr.io/maplibre/martin:1.11.0` | `3002` → 3000 | `martin-cc` |
| Application | `laravel-octane` | `dev-light`, `dev-full` | `georag/laravel:latest` (local build) | `${APP_PORT:-80}` → 80 | `laravel-octane-cc` |
| Application | `laravel-horizon` | `dev-light`, `dev-full` | `georag/laravel:latest` | none | `laravel-horizon-cc` |
| Application | `laravel-reverb` | `dev-light`, `dev-full` | `georag/laravel:latest` | `8085` → 8080 | `laravel-reverb-cc` |
| Domain service | `fastapi` | `dev-data`, `dev-full` | `georag/fastapi:latest` (local build) | `8000` | `fastapi-cc` |
| Model sidecars | `reranker` | `dev-data`, `dev-full` | `georag/fastapi:latest` | none (internal 8000) | none — Cohere Rerank v4 on `georag-foundry-cc` |
| Model sidecars | `embedding` | `dev-data`, `dev-full` | `georag/fastapi:latest` | none (internal 8000) | none — Cohere Embed v4 on `georag-foundry-cc` |
| Model sidecars | `sparse` | `dev-data`, `dev-full` | `georag/fastapi:latest` | none (internal 8000) | none — no sidecar app exists; SPLADE++ stays self-hosted in-process |
| Data (profile) | `qdrant` | `dev-data`, `dev-full` | `qdrant/qdrant:v1.17.1` | `6333`, `6334` | `qdrant-cc` |
| Data (profile) | `minio` (SeaweedFS) | `dev-data`, `dev-full` | `chrislusf/seaweedfs:4.35` | `8333` (S3), `8888` (filer) | none — Azure Blob `georagblobcc` (ADR-0020) |
| Data (profile) | `minio-init` | `dev-data`, `dev-full` | `minio/mc:RELEASE.2025-08-13T08-35-41Z` | none | none |
| Orchestration | `hatchet-lite` | `dev-data`, `dev-full` | `ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.86.12` | `8889` → 8888, `7077` | `hatchet-cc` |
| Orchestration | `hatchet-worker` | `dev-data`, `dev-full` | `georag/fastapi:latest` | none | `hatchet-worker-cc` |

Every third-party image except `hatchet-lite` carries an `@sha256` digest
pin (captured 2026-04-19, refreshed in the 2026-06-23 sweep). The three
`georag/*` images are local builds tagged `:latest`, meaning "last local
build", never a registry pull.

All services share one bridge network named `georag` and address each
other by service name. Named volumes: `postgres_data`, `qdrant_data`,
`redis_data`, `minio_data`, `fastapi_hf_cache`, `hatchet_config`, plus the
external `georag-phase-b-extract` (uranium ingest staging, created outside
compose).

There is no edge proxy. Octane on `APP_PORT` is the front door; Laravel
proxies `/tiles/…` to Martin and calls FastAPI over the internal network. Caddy was
removed with Kestra on 2026-07-28 (§7).

---

## 2. Always-on substrate

These four services have no `profiles:` key and start on a bare
`docker compose up`.

### postgresql ([docker-compose.yml:121](../../../docker-compose.yml))

- **Image** `georag/postgres:18-ext`, built by
  [docker/postgresql/Dockerfile](../../../docker/postgresql/Dockerfile) on
  `postgis/postgis:18-3.6-alpine` (base digest pinned via `BASE_DIGEST`).
  Adds **h3, hypopg, pg_stat_kcache, pg_partman, pg_repack, pg_ivm** from
  pinned release tags; `auto_explain` ships with the base image.
- **Exposure** `expose: 5432` only. No host port; PgBouncer is the only
  intended client, and the direct-connection exceptions are listed under
  pgbouncer below.
- **Command-line tuning** (64 GiB workstation, NVMe): `shared_buffers=8GB`,
  `effective_cache_size=24GB`, `work_mem=128MB`, `maintenance_work_mem=1GB`,
  `io_method=worker` (io_uring was tested and rejected under WSL2 seccomp),
  `random_page_cost=1.1`, `max_connections=200`, `jit=off`,
  `wal_compression=lz4`, `max_wal_size=4GB`,
  `idle_in_transaction_session_timeout=60000`, `lock_timeout=3000`.
  `shared_preload_libraries` = `pg_stat_statements,auto_explain,pg_stat_kcache`
  with auto_explain logging plans over 2 s as JSON. Every memory knob has a
  `POSTGRES_*` env override.
- **pg_hba** bind-mounted read-only from
  [docker/postgresql/pg_hba.conf](../../../docker/postgresql/pg_hba.conf)
  and activated with `-c hba_file=…`, so it survives volume wipes.
- **Init scripts** [docker/postgresql/init/](../../../docker/postgresql/init/)
  is mounted at `/docker-entrypoint-initdb.d` and runs on first
  initialisation only: `10-phase0-extensions-and-schemas.sql`,
  `20-hatchet-database.sql` (the `hatchet` role + logical DB),
  `init-postgis.sql`, `init-roles.sql`, `init-test-db.sh`, and the two
  `Z_activate_*.sql` opt-ins. `init-roles.sql` is inside this directory; an
  older note claiming it had to be applied by hand is obsolete.
- **No WAL archive volume.** The base compose has no `archive_mode` and no
  `pg_wal_archive` volume; those live only in the
  `docker/compose.wal-archiving.yml` overlay (§6). Production relies on
  Azure PITR (35 days).
- **Healthcheck** `pg_isready -U georag -d georag`, 10 s interval, 30 s
  start period. **Stop grace** 30 s. **Limits** 6 CPU / 16 GiB
  (reservation 10 GiB), `shm_size: 1gb`.

### pgbouncer ([docker-compose.yml:254](../../../docker-compose.yml))

- **Image** `edoburu/pgbouncer:v1.25.1-p0` (digest-pinned). Chosen after
  Bitnami moved its image behind a subscription.
- **Pool mode** `transaction` — required for asyncpg.
  `DEFAULT_POOL_SIZE=50`, `MIN_POOL_SIZE=5`, `RESERVE_POOL_SIZE=5`,
  `MAX_CLIENT_CONN=1000`, `SERVER_IDLE_TIMEOUT=600`, `SERVER_LIFETIME=3600`,
  `QUERY_WAIT_TIMEOUT=120`, `SERVER_RESET_QUERY=DISCARD ALL`,
  `AUTH_TYPE=scram-sha-256`.
  `IGNORE_STARTUP_PARAMETERS=extra_float_digits,jit,application_name` so
  asyncpg's `server_settings` are accepted.
- **Port** `${PGBOUNCER_PORT:-6432}:6432`.
- **Who bypasses it** (direct to `postgresql:5432`): Laravel's
  `pgsql_migrations` connection and its read-only `HATCHET_PG_*` connection
  into the Hatchet DB; FastAPI's `POSTGRES_DIRECT_HOST` path (transaction-
  local `set_config` for the per-flow key loader); `hatchet-worker`
  entirely (transactions + RLS GUCs); `martin` (persistent connections);
  `hatchet-lite` (its own DB).
- **Healthcheck** `psql … -d pgbouncer -c 'SHOW POOLS'` as the
  `ADMIN_USERS` role (`georag`). **Stop grace** 30 s. **Limits**
  0.5 CPU / 256 MiB.

### redis ([docker-compose.yml:351](../../../docker-compose.yml))

- **Image** `redis:8.6.4-alpine` (digest-pinned; the alpine line caps at
  8.6.x). Production runs Redis 8.10 on `redis-cc`.
- **Command** `--maxmemory 512mb --maxmemory-policy volatile-lru
  --appendonly yes --appendfsync everysec --save "" --databases 4
  --timeout 600 --tcp-keepalive 300 --slowlog-log-slower-than 1000` plus
  the five `lazyfree-*` flags. `volatile-lru`, not `allkeys-lru`, because
  one instance holds Horizon queue jobs (no TTL) beside cache and sessions
  (TTL'd); `scripts/check_redis_manifests.py` enforces this on every Redis
  manifest in the repo.
- **DB layout** `db0` queue / Horizon / sessions, `db1` cache, `db2`–`db3`
  spare.
- **Auth** `--requirepass ${REDIS_PASSWORD:-}` (empty in dev).
- **Port** `${REDIS_PORT:-6379}:6379`. **Volume** `redis_data:/data`.
- **Healthcheck** `redis-cli ping | grep PONG`. **Stop grace** 15 s (AOF
  fsync). **Limits** 1 CPU / 1 GiB.

### martin ([docker-compose.yml:1934](../../../docker-compose.yml))

- **Image** `ghcr.io/maplibre/martin:1.11.0` (digest-pinned).
- **Role** MVT tile server over the PostGIS tile functions. Removed with
  the demo-external services, restored 2026-08-25 because the database
  side (18 tile functions, `martin_readonly` role) never went away and
  the Workspace map needs it. See [Ch 09](09-martin-and-maplibre.md).
- **Command** `--config /config/martin.yaml`, config bind-mounted
  read-only from [docker/martin/martin.yaml](../../../docker/martin/martin.yaml).
- **Database** `DATABASE_URL` points at `postgresql:5432` directly as
  `georag_app` (Martin holds persistent connections; transaction pooling
  would break it). On Azure the app uses the `martin_readonly` credential
  rotated by `deploy/azure/containerapps/rotate-martin-credential.sh`.
- **Port** `${MARTIN_PORT:-3002}:3000`. Laravel proxies `/tiles/…`, so
  production needs no ingress on `martin-cc`.
- **Depends on** `postgresql` healthy. **Healthcheck**
  `wget --spider http://127.0.0.1:3000/health`, 60 s start period (Martin
  validates every source on boot). **Stop grace** 10 s. **Limits**
  0.5 CPU / 512 MiB.

---

## 3. `dev-light` — the Laravel tier

One image, three commands. Built by
[docker/laravel.Dockerfile](../../../docker/laravel.Dockerfile) on
`php:8.5-cli` (multi-stage, digest-pinned). All three bind-mount the repo
root at `/app:cached`, talk to Postgres through PgBouncer as `georag_app`,
and carry the same `REVERB_*`, `LANGFUSE_*` and `AWS_*` blocks.

### laravel-octane ([docker-compose.yml:432](../../../docker-compose.yml))

- **Command** a `sh -c` wrapper that drops the dev opcache ini, writes
  `opcache.validate_timestamps=1` and the upload-size overrides
  (`PHP_UPLOAD_MAX_FILESIZE` / `PHP_POST_MAX_SIZE`, default 2G), then
  `exec php artisan octane:start --server=swoole
  --workers=${OCTANE_WORKERS:-4} --task-workers=${OCTANE_TASK_WORKERS:-6}
  --max-requests=${OCTANE_MAX_REQUESTS:-500}`. The `exec` makes Swoole
  PID 1 so SIGTERM drains workers inside the grace period.
- **Port** `${APP_PORT:-80}:80`. `SANCTUM_STATEFUL_DOMAINS` defaults to
  the localhost / `host.docker.internal` set; without it SPA login 500s.
- **Database connections** runtime `DB_*` → `pgbouncer:6432` as
  `georag_app`; `MIGRATE_DB_*` → `postgresql:5432` as the `georag` owner
  role for the `pgsql_migrations` connection (DDL on phase-0-owned tables
  without granting `georag` to the runtime role); `HATCHET_PG_*` →
  `postgresql:5432/hatchet` as `hatchet`, wired for a Hatchet Worker
  Dashboard (`HatchetWorkersController`) that no longer exists in `app/`;
  the `pgsql_hatchet` connection in `config/database.php` has no consumer.
- **Other targets** `FASTAPI_HOST=fastapi:8000` with
  `FASTAPI_SERVICE_KEY`; `REVERB_HOST=laravel-reverb`, `REVERB_PORT=8080`
  (the *publisher* target inside the network — the browser uses
  `VITE_REVERB_*` → `localhost:8085`); `AWS_ENDPOINT=http://minio:8333`,
  bucket `bronze`, path-style; `CACHE_DRIVER` / `SESSION_DRIVER` /
  `QUEUE_CONNECTION` all `redis`.
- **Depends on** `pgbouncer`, `redis` healthy. **Healthcheck**
  `curl -f http://localhost:80/up`, 60 s start period. **Stop grace**
  30 s. **Limits** 2 CPU / 2 GiB.

### laravel-horizon ([docker-compose.yml:562](../../../docker-compose.yml))

- **Command** [docker/horizon-entrypoint.sh](../../../docker/horizon-entrypoint.sh):
  starts the `docker/horizon-health.php` listener on
  `HORIZON_HEALTH_PORT` (8080) and then `exec`s `php artisan horizon`, so
  Horizon is PID 1 and drains on SIGTERM. Compose could probe with
  `horizon:status`, but Azure Container Apps only speaks HTTP/TCP, and dev
  runs the same code path production does.
- **Supervisors** from `config/horizon.php`: `supervisor-1` on queue
  `default` (3 processes in `local`, 10 in `production`) and
  `supervisor-llm` on queue `llm` (`HORIZON_LLM_MAX_PROCESSES`, 2 local /
  5 production). Hard rule 7: Horizon is for short user-triggered work
  only; three jobs exist in total (see [Ch 07](07-orchestration.md)).
- **Depends on** `pgbouncer`, `redis` healthy; `laravel-octane` started.
  **Healthcheck** `php artisan horizon:status | grep -E 'running|paused'`.
  **Stop grace** 60 s. **Limits** 2 CPU / 1 GiB. No host port.

### laravel-reverb ([docker-compose.yml:651](../../../docker-compose.yml))

- **Command** `php artisan reverb:start --host=0.0.0.0 --port=8080`.
- **Port** `${REVERB_HOST_PORT:-8085}:8080`. Browser → `:8085`; in-network
  publishers → `laravel-reverb:8080`. Mixing those up produces 60 s
  channel-drop timeouts.
- **Role** WebSocket fan-out: `QueryStreamEvent` frames re-broadcast from
  the FastAPI SSE stream, ingestion progress, workspace-data-updated
  cascades.
- **Depends on** `redis` healthy; `laravel-octane` started.
  **Healthcheck** `curl -f http://localhost:8080/up`. **Stop grace** 30 s.
  **Limits** 0.5 CPU / 512 MiB.

---

## 4. `dev-data` — domain service, model sidecars, stores, Hatchet

### fastapi ([docker-compose.yml:721](../../../docker-compose.yml))

- **Image** `georag/fastapi:latest`, built from
  [docker/fastapi.Dockerfile](../../../docker/fastapi.Dockerfile) with
  context `./src` (it installs the sibling `georag_object_storage` and
  `georag_geoparsers` packages). Three stages on `python:3.13-slim`; the
  first builds Tesseract 5.5.2 from source (ADR-0017).
- **Command** `uvicorn app.main:app --host 0.0.0.0 --port 8000
  --workers ${UVICORN_WORKERS:-3} --no-access-log --proxy-headers
  --forwarded-allow-ips * --timeout-graceful-shutdown 30
  --header server:GeoRAG`. Pinned in compose because a stray
  `docker commit` once baked the worker CMD into the image.
  `.env.example` sets `UVICORN_WORKERS=6`; the compose default is 3
  because every worker used to load its own model copies.
- **Runs as** `33:33` (www-data) with `HOME`, `HF_HOME`,
  `SENTENCE_TRANSFORMERS_HOME`, `NUMBA_CACHE_DIR`, `MPLCONFIGDIR`,
  `XDG_CACHE_HOME` all redirected under `/tmp`. `PYTHONPATH=/app` so the
  bind mount wins over the baked site-packages copy. `shm_size: 1gb` for
  the §04p process pools.
- **Port** `${FASTAPI_PORT:-8000}:8000`. Service-to-service auth is the
  `X-Service-Key` header checked against `FASTAPI_SERVICE_KEY`.
- **Stores** Postgres via `pgbouncer:6432` as `georag_app`, plus
  `POSTGRES_DIRECT_HOST=postgresql` for the transaction-bound per-flow key
  loader; `redis:6379`; `qdrant:6333`; object storage at
  `S3_ENDPOINT=http://minio:8333` with `S3_*`, `MINIO_*` and `AWS_*`
  aliases all set to the same values (`STORAGE_BACKEND` selects
  `s3_compatible` here, `azure_blob` in production).
- **Models and LLM**
  - `LLM_BACKEND=${LLM_BACKEND:-azure}` with `AZURE_FOUNDRY_ENDPOINT` /
    `API_KEY` / `DEPLOYMENT` (no defaults) and
    `AZURE_FOUNDRY_MAX_MODEL_LEN=128000`. `VLLM_URL` has no default: the
    old `http://vllm:8000/v1` named a deleted service. `ANTHROPIC_*` is
    wired as the optional fallback (`claude-opus-4-8`, prompt caching on).
  - `EMBEDDING_BACKEND=${EMBEDDING_BACKEND:-foundry}` and
    `RERANKER_BACKEND=${RERANKER_BACKEND:-foundry}`. `.env.example` sets
    `local` / `cross_encoder`, which route to the sidecars through
    `EMBEDDING_SERVICE_URL=http://embedding:8000` and
    `RERANKER_SERVICE_URL=http://reranker:8000`. `SPARSE_SERVICE_URL=
    http://sparse:8000` always applies — SPLADE++ has no Foundry
    equivalent.
  - `EMBEDDING_MODEL_NAME=Qwen/Qwen3-Embedding-0.6B` at a pinned revision,
    `EMBEDDING_DIMENSION=1024`; must match `embedding` and
    `hatchet-worker` exactly. Cohere Embed v4 is asked for 1024 dims so the
    `georag_chunks` collection is unchanged, but switching still requires
    a full re-embed.
- **Retrieval budgets and flags** `TIMEOUT_POSTGIS_S=5`,
  `TIMEOUT_QDRANT_S=2`, `TIMEOUT_REDIS_MS=500`, `TIMEOUT_GATHER_S=180`,
  `RETRIEVAL_QUALITY_THRESHOLD=0.5`, `RETRIEVAL_USE_DOCUMENT_PASSAGES=true`.
  Flags, all default off unless noted: `AGENTIC_RETRIEVAL_V2_ENABLED`,
  `CITATION_SPAN_RESOLVER_ENABLED`, `REPAIR_LOOP_SHADOW_ENABLED`,
  `CONTEXT_PREP_ENABLED`, `PARENT_CHUNKING_ENABLED` (+ `_GROUP_SIZE=3`),
  `MULTI_TURN_RESOLUTION_ENABLED` (**on** since 2026-08-14).
  `TIMEOUT_NEO4J_S=3` is still passed and unread.
- **Hatchet client** `HATCHET_CLIENT_TOKEN` (required),
  `HATCHET_CLIENT_HOST_PORT=hatchet-lite:7077`, TLS strategy `none` — for
  `POST /internal/v1/shadow/{workflow}/trigger`.
- **Required secrets** `FASTAPI_SERVICE_KEY`, `AUDIT_ENCRYPTION_KEY`,
  `HATCHET_CLIENT_TOKEN`, `GEORAG_APP_PASSWORD`, an S3 secret — and
  `KESTRA_FLOW_JWT_SECRET`, which compose still marks `:?` required
  although Kestra is gone; `app/config.py` declares it with an empty
  default, so the compose requirement is the only thing keeping it alive.
- **Volumes** `./src/fastapi:/app:cached`, `fastapi_hf_cache:/tmp/hf_cache`
  (shared with the sidecars), `georag-phase-b-extract:/data`.
- **Depends on** `pgbouncer`, `redis`, `qdrant`, `minio`, `embedding`,
  `sparse` — all `service_healthy`. Not `reranker`: the orchestrator
  degrades to RRF order when it is absent. Note the sidecar dependencies
  apply even when both backends are `foundry`, so a `dev-data` boot always
  waits for the Qwen3 embedding and SPLADE models to load.
- **Healthcheck** `curl -f http://localhost:8000/health`. **Stop grace**
  30 s. **Limits** 5 CPU / 16 GiB (reservation 5 GiB) plus a reserved
  NVIDIA GPU, used only for in-process reranker LoRA training runs.

### reranker / embedding / sparse ([1111](../../../docker-compose.yml), [1183](../../../docker-compose.yml), [1246](../../../docker-compose.yml))

Three single-model HTTP hosts introduced 2026-06-24 so the uvicorn
workers stop loading their own copies (the OOM driver at the time). They
share the pattern:

- `georag/fastapi:latest`, user `33:33`, `./src/fastapi:/app:cached` +
  `fastapi_hf_cache:/tmp/hf_cache`, the same `/tmp` cache redirects,
  `OMP_NUM_THREADS=10`, `TOKENIZERS_PARALLELISM=false`.
- `uvicorn app.<name>_service:app --port 8000 --workers 1` (the reranker
  honours `RERANKER_WORKERS`). Internal port only; nothing is published.
- `X-Service-Key` auth via `app/sidecar_auth.py` against
  `FASTAPI_SERVICE_KEY`.
- `/health` returns 200 only once the model is loaded, so `depends_on:
  service_healthy` is a real readiness gate.
- Must **not** set their own `*_SERVICE_URL`, or they proxy to themselves.
- Stop grace 15 s.

| Sidecar | App | Model | Device | Start period | Limits |
|---|---|---|---|---|---|
| `reranker` | `app.reranker_service` | `Qwen/Qwen3-Reranker-0.6B` (`RERANKER_BACKEND=qwen3_causal`, `RERANKER_DEVICE=cuda`) | GPU (reserved) | 90 s | 4 CPU / 5 GiB, `shm_size: 512m` |
| `embedding` | `app.embedding_service` | `EMBEDDING_MODEL_NAME` (Qwen3-Embedding-0.6B, pinned revision) | CPU | 120 s | 4 CPU / 4 GiB |
| `sparse` | `app.sparse_service` | SPLADE++ | CPU | 120 s | 4 CPU / 3 GiB |

One gotcha: the sidecar reads the **same** `RERANKER_BACKEND` variable
as the caller (`app/services/reranker.py`, module scope). With nothing in
`.env`, compose gives the sidecar `qwen3_causal` and the caller `foundry`.
With `.env.example`'s `cross_encoder`, both get `cross_encoder`: the
caller proxies to the sidecar and the sidecar hosts `bge-reranker-base`
instead of Qwen3. `RERANKER_MODEL_PATH` (a local LoRA candidate) is
honoured by both for A/B parity.

### qdrant ([docker-compose.yml:1320](../../../docker-compose.yml))

- **Image** `qdrant/qdrant:v1.17.1` (digest-pinned).
- **Ports** `${QDRANT_PORT:-6333}:6333` HTTP, `${QDRANT_GRPC_PORT:-6334}:6334`.
- **Cluster-level config** HNSW `m=32`, `ef_construct=256`, `ef=200`,
  `max_indexing_threads=4`; WAL capacity 256 MiB per collection. These
  apply only to collections created without an explicit `hnsw_config`;
  quantisation is set per collection in the indexers, not here.
- **Auth** deliberately unset. `QDRANT__SERVICE__API_KEY=""` would
  *enable* auth with an empty-key expectation. See RUNBOOK "Qdrant access
  control".
- **Collection** `georag_chunks` — 1024-dim dense + SPLADE++ sparse
  ([Ch 02](02-data-stores.md)).
- **Volume** `qdrant_data:/qdrant/storage`. **Healthcheck** a bash
  `/dev/tcp` GET of `/readyz` (the image has bash but no curl/wget).
  **Stop grace** 30 s. **Limits** 2 CPU / 4 GiB.

### minio — SeaweedFS ([docker-compose.yml:1422](../../../docker-compose.yml)) + minio-init ([1477](../../../docker-compose.yml))

- **Image** `chrislusf/seaweedfs:4.35` (digest-pinned). The service and
  container are still named `minio` so every `http://minio:…` reference
  keeps resolving (ADR-0001). Production uses Azure Blob instead
  (`STORAGE_BACKEND=azure_blob`, ADR-0020); no SeaweedFS runs on Azure.
- **Entrypoint** `sh /usr/local/bin/entrypoint.sh`, bind-mounted from
  [docker/seaweedfs/entrypoint.sh](../../../docker/seaweedfs/entrypoint.sh).
- **Ports** `${S3_API_PORT:-8333}:8333` S3 API (was 9000 under MinIO),
  `${S3_CONSOLE_PORT:-8888}:8888` filer HTTP. Master on 9333 internally,
  which is what the healthcheck probes
  (`wget http://127.0.0.1:9333/cluster/status`; `localhost` resolves to
  `::1` in this image and fails).
- **Credentials** `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`, sourced from
  `S3_ACCESS_KEY` / `S3_SECRET_KEY` with the `MINIO_*` names as fallback.
- **Volume** `minio_data:/data`. **Stop grace** 30 s. **Limits**
  2 CPU / 2 GiB.
- **minio-init** runs `minio/mc` once (`restart: "no"`) after `minio` is
  healthy and idempotently creates `bronze`, `exports`, `bronze-raster`,
  `georag-backups`, `tier-hot`, `tier-warm`, `tier-cold`. SeaweedFS also
  auto-creates buckets on first PUT; this is belt and braces for a fresh
  stack. `georag-backups` has had no writer since the `backup_*`
  workflows were deleted on 2026-08-23.

### hatchet-lite ([docker-compose.yml:1585](../../../docker-compose.yml))

- **Image** `ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.86.12` — tag-
  pinned, the one third-party image without a digest. Aligned with
  `hatchet-sdk>=1.33`; the 2026-06-02 Cameco-recovery incident was a silent
  engine bump invalidating the SDK contract.
- **What it is** engine + admin + dashboard + migrations in one container.
  `SERVER_MSGQUEUE_KIND=postgres`, so no RabbitMQ; `DATABASE_URL` is the
  `hatchet` logical DB on `postgresql:5432` as the `hatchet` role, created
  by `docker/postgresql/init/20-hatchet-database.sql`.
- **Ports** `${HATCHET_API_PORT:-8889}:8888` (UI + REST),
  `${HATCHET_GRPC_PORT:-7077}:7077` (workers).
- **Dev-only insecure flags** `SERVER_AUTH_COOKIE_INSECURE`,
  `SERVER_GRPC_INSECURE`, `SERVER_AUTH_SET_EMAIL_VERIFIED` all default `t`
  via `HATCHET_*` env; `.env.production.example` flips them to `f` and the
  `cd.yml` pre-flight asserts it.
- **Token bootstrap** (once per environment):
  `docker exec georag-hatchet /hatchet-admin --config /config token create
  --name georag-worker --tenant-id <default-tenant-uuid>` → paste into
  `HATCHET_CLIENT_TOKEN`. The tenant id is `SELECT id FROM "Tenant" WHERE
  slug='default'` in the `hatchet` DB.
- **Volume** `hatchet_config:/config`. **Depends on** `postgresql`
  healthy. **Healthcheck** `wget http://localhost:8888/api/ready`, 90 s
  start period. No resource limits declared.

### hatchet-worker ([docker-compose.yml:1662](../../../docker-compose.yml))

- **Image** `georag/fastapi:latest`; **command**
  `python3 -m app.hatchet_workflows.worker`. The 2026-06-23 sweep removed
  the bootstrap `pip install` shim — everything is in `pyproject.toml`.
- **Pool** `WORKER_POOL=all`: 51 workflows registered
  (`python -m app.hatchet_workflows.worker --list` prints them without
  connecting). That is 13 in the `ingestion` list (`outbox_dispatcher`,
  `ingest_pdf`, `tiff_normalize`, `ingest_zip_archive`, `ingest_spatial`,
  `ingest_tabular`, `ingest_well_logs`, `stale_run_detector`,
  `nightly_ingestion_integrity`, `reliability_metrics_publisher` and the
  three Phase 0 ingestion agents) and 38 in the `ai` list (crons such as
  `audit_ledger_verify`, `retention_sweep`, `mv_refresh_silver`,
  `promote_silver_to_gold`, `embed_pending_passages`,
  `enrich_passage_context`, `answer_quality_watch`, `public_geo_sync`,
  `cost_burn_watcher`, `pg_partman_maintenance`, plus the report / target /
  learning workflows and seven Phase 0 AI agents). The full inventory is
  §07b of the architecture doc and [Ch 07](07-orchestration.md). No
  `hatchet-worker-ingestion` / `hatchet-worker-ai` split exists.
- **Slots** `HATCHET_WORKER_SLOTS=20`.
- **Postgres** direct to `postgresql:5432` as the **`georag` owner role**
  (not `georag_app`) — transactions and RLS GUCs need a stable session.
- **Stores** `qdrant:6333`; object storage through the same `S3_*` /
  `MINIO_*` / `AWS_*` aliases as fastapi (`http://minio:8333`). It also
  sets `SEAWEEDFS_S3_ENDPOINT` defaulting to `http://minio:9000` — the old
  MinIO port — but nothing under `app/` reads that variable, so it is
  dead config rather than a bug.
- **OCR / parsing** `OCR_ENGINE=cohere_parse` with
  `AZURE_FOUNDRY_PARSE_DEPLOYMENT` (empty default raises the adapter's
  NotConfigured error loudly), `COHERE_PARSE_TIMEOUT_S=120`,
  `COHERE_PARSE_MAX_PIXELS=4000000`, `COHERE_PARSE_OUTPUT_FORMAT=blocks`,
  `PDF_PARSER_TESSERACT_FALLBACK_ENABLED=true`, `OCR_PAGES_PER_BATCH=8`,
  `OCR_MAX_PAGES_PER_DOC=300`, `PDF_PARSE_PAGE_WORKERS=4`,
  `PARSE_SUBPROCESS_MAX_WORKERS` (empty → `min(cpu_count, 4)`),
  `PARSE_MIN_FREE_RAM_MB=4500`, `PARSE_MEMORY_WAIT_MAX_S=120`.
  `OCR_ROUTING_THRESHOLDS_JSON` carries hand-chosen bands with a
  `floor_tier: spot_check` for Cohere Parse pages; they are **not
  calibrated** (see the `.env.example` note).
- **Embedding on the ingest path** `EMBEDDING_BACKEND=${EMBEDDING_BACKEND:-foundry}`
  with the `AZURE_FOUNDRY_EMBED_*` block, and the same
  `EMBEDDING_MODEL_NAME` / revision / dimension as fastapi. The worker has
  **no** `EMBEDDING_SERVICE_URL` or `SPARSE_SERVICE_URL`, so under
  `local` it loads its own model copies in-process (hence the GPU
  reservation); under `foundry` it calls Azure. Whatever `.env` sets must
  match the fastapi service.
- **Required secrets** `HATCHET_CLIENT_TOKEN`,
  `EXTERNAL_NOTIFICATION_HMAC_SECRET`, `AUDIT_ENCRYPTION_KEY`,
  `FASTAPI_SERVICE_KEY`, `KESTRA_FLOW_JWT_SECRET` (same stale requirement
  as fastapi), `POSTGRES_PASSWORD`, an S3 secret.
- **Bridges** `LARAVEL_INTERNAL_URL=http://laravel-octane` for the Reverb
  broadcast bridge; `redis:6379` for the per-sender rate-limit bucket;
  `LANGFUSE_BASE_URL` pinned in-network because the worker has no lifespan
  hook to rewrite it.
- **Volumes** `./src/fastapi:/app:cached`, `georag-phase-b-extract:/extract`.
  The Dagster bind mount is gone (2026-07-28).
- **Depends on** `hatchet-lite`, `postgresql`, `qdrant`, `minio` healthy.
  **Healthcheck** `grep -q app.hatchet_workflows.worker /proc/1/cmdline`.
  **Limits** 6 CPU / 24 GiB (reservation 8 GiB) plus the reserved GPU.
  `OMP_NUM_THREADS=6` to match. `LOG_LEVEL` defaults to `info` here
  (`debug` elsewhere); the worker emits JSON logs since 2026-08-21.

---

## 5. Removed services

Older documents, memory notes and the compose file's own comments still
mention these. None is defined anywhere in the repo.

| Service(s) | Removed | Replacement / why |
|---|---|---|
| `ollama` | 2026-05-17 | vLLM, then Azure AI Foundry |
| `neo4j`, `neo4j-warmup`, `neo4j_exporter` | 2026-07-28 | No knowledge graph (hard rule 9); Layer 4 graph half is fail-open |
| `dagster-daemon`, `dagster-webserver` (`dev-ingest` profile) | 2026-07-28 (tree deleted 2026-08-28) | Hatchet `ingest_*` workflows + `promote_silver_to_gold` |
| `kestra`, `caddy` | 2026-07-28 | Never live; Caddy existed only to front Kestra |
| `activepieces` | Phase 3 | Sunset before Kestra |
| `vllm`, `vllm-warmup` | 2026-07-30 | Azure AI Foundry (Cohere Command A+); `LLM_BACKEND=vllm` still accepted for an external endpoint |
| `hatchet-worker-ingestion`, `hatchet-worker-ai` | merged | One `hatchet-worker` with `WORKER_POOL=all` |
| `otel-collector`, `tempo`, `prometheus`, `alertmanager`, `redis_exporter`, `postgres_exporter`, `loki`, `promtail`, `grafana` | by 2026-08-25 | Azure Monitor + Log Analytics in production; Laravel Pulse locally ([Ch 12](12-observability.md)) |
| `ofelia`, `backup-agent` and the `backup_*` Hatchet workflows | 2026-08-23 | Azure PITR for Postgres; Qdrant is rebuildable; Blob is the one irreplaceable copy |
| `martin` | removed with the demo services, **restored 2026-08-25** | still here — listed in §2 |

---

## 6. Override compose files

Three overlays exist under `docker/`. None is used by CI or by the
documented dev profiles, and two of them reference services that no longer
exist in the base file.

| File | What it adds | Status |
|---|---|---|
| [docker/compose.langfuse.yml](../../../docker/compose.langfuse.yml) | Self-hosted Langfuse v3 (`langfuse-web`, `langfuse-worker`, ClickHouse, `langfuse-init`) reusing the stack's Postgres / Redis / SeaweedFS | Optional. Every app service already carries `LANGFUSE_*` env pointing at `langfuse-web:3000`; empty keys disable the SDK. |
| [docker/compose.redis-staging.yml](../../../docker/compose.redis-staging.yml) | Three Redis instances (`redis-cache`, `redis-queue`, `redis-sessions`) under `staging` / `prod` profiles | Dormant. Its runbooks are under `ops/runbooks/_archived/`. Production uses one `redis-cc`. |
| [docker/compose.wal-archiving.yml](../../../docker/compose.wal-archiving.yml) | `pg_wal_archive` volume + `archive_mode` for on-prem PITR | Dormant and partly broken: it expects `georag-backup-agent` and Ofelia, both deleted 2026-08-23. Keep for an on-prem build; do not apply as-is. |

The previously listed `docker/compose.exporters.yml` and
`docker/compose.vllm.yml` do not exist.

---

## 7. Resource budget snapshot (dev workstation, 64 GiB RAM, one GPU)

| Service | CPU limit | Mem limit | Mem reservation | GPU |
|---|---|---|---|---|
| postgresql | 6.0 | 16 GiB | 10 GiB | |
| pgbouncer | 0.5 | 256 MiB | 64 MiB | |
| redis | 1.0 | 1 GiB | 128 MiB | |
| martin | 0.5 | 512 MiB | | |
| laravel-octane | 2.0 | 2 GiB | 256 MiB | |
| laravel-horizon | 2.0 | 1 GiB | 128 MiB | |
| laravel-reverb | 0.5 | 512 MiB | 64 MiB | |
| fastapi | 5.0 | 16 GiB | 5 GiB | reserved (training only) |
| reranker | 4.0 | 5 GiB | 2 GiB | reserved |
| embedding | 4.0 | 4 GiB | 2 GiB | |
| sparse | 4.0 | 3 GiB | 1 GiB | |
| qdrant | 2.0 | 4 GiB | 512 MiB | |
| minio | 2.0 | 2 GiB | 256 MiB | |
| hatchet-lite | — | — | | |
| hatchet-worker | 6.0 | 24 GiB | 8 GiB | reserved |

Summed limits exceed the host by design: a `dev-light` + `dev-data` day
never runs all of these at their ceilings at once. Three services reserve
the single GPU (ADR-0018 covers the allocation).

---

## 8. Stale comments inside `docker-compose.yml`

The service blocks are correct; several comments are not. Listed here so
the next compose tidy can clear them without re-deriving the facts.

- The postgresql section banner still reads "POSTGRESQL 17"; the image is
  18.
- The pgbouncer pool-sizing note counts Dagster and backup-agent
  connections and "4 uvicorn workers".
- The hatchet-lite header says "Phase 0 only registers the synthetic
  acceptance-test workflow"; the hatchet-worker header says it "registers
  two workflows". The worker registers 51.
- The hatchet-worker `SEAWEEDFS_S3_ENDPOINT` default names port 9000 and
  its comment describes an "ADR-0001 transitional dual-run" that ended.
- The RAGFlow-removal note lists "Azure Document Intelligence" and
  "Qwen-VL on vLLM" as the OCR / VL stack; both are gone (ADR-0019).
- The fastapi `OMP_NUM_THREADS` and hatchet-worker GPU comments still talk
  about "contending with vLLM".
- The `KESTRA_FLOW_JWT_SECRET` requirement on fastapi and hatchet-worker
  outlived Kestra (§4).
