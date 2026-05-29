# Chapter 01 — Services Catalog

Every container in the stack, grouped by tier. All line references point into
[docker-compose.yml](../../../docker-compose.yml) unless otherwise noted.

## Tier overview

| Tier | Containers |
|------|-----------|
| **Edge** | caddy |
| **Application** | laravel-octane, laravel-horizon, laravel-reverb, fastapi, hatchet-worker-ingestion, hatchet-worker-ai |
| **Orchestration** | hatchet-lite, kestra, dagster-daemon, dagster-webserver |
| **Data (always-on substrate)** | postgresql, pgbouncer, redis |
| **Data (profile-gated)** | neo4j (+ neo4j-warmup), qdrant, minio (SeaweedFS) (+ minio-init), martin |
| **LLM** | vllm (+ vllm-warmup) |
| **Observability** | otel-collector, tempo, prometheus, alertmanager, redis_exporter, postgres_exporter, neo4j_exporter, loki, promtail, grafana, ofelia, backup-agent |

---

## Edge

### caddy ([docker-compose.yml:2393](../../../docker-compose.yml))
- **Image** `caddy:2.8-alpine`
- **Ports** `8087:8087`, `8443:8443` (HTTPS via internal CA or ACME)
- **Profile** `dev-light`, `dev-data`, `dev-full`
- **Role** WebSocket-capable reverse proxy in front of Kestra; uses
  `forward_auth` to validate Sanctum sessions/PATs against Laravel and injects
  basic auth before proxying to `kestra:8080`. Coexists with the Laravel-side
  passthrough at `/admin/integrations/kestra/{path?}` (browser-cookie users
  keep that path; PAT/WS operators use the edge).
- **Config** `caddy/Caddyfile` (bind-mounted RO at `/etc/caddy/Caddyfile`)
- **Volumes** `caddy_data:/data` (persists internal CA so the dev cert stays
  stable across restarts)
- **Healthcheck** `wget http://localhost:8087/healthz`
- **Depends on** `kestra` (healthy), `laravel-octane` (healthy)

> Note: Laravel Octane itself listens on `APP_PORT` (default 80) and is the
> primary front door for the React app — Caddy is **not** in that path. It is
> there for the Kestra/PAT/WS use case only.

---

## Application

### laravel-octane ([docker-compose.yml:503](../../../docker-compose.yml))
- **Image** `georag/laravel:latest` (built from [docker/laravel.Dockerfile](../../../docker/laravel.Dockerfile))
- **Ports** `${APP_PORT:-80}:80`
- **Command** `php artisan octane:start --server=swoole --workers=4 --task-workers=6 --max-requests=500`
  with an `exec` wrapper so Swoole becomes PID 1 and receives SIGTERM
  ([docker-compose.yml:510-518](../../../docker-compose.yml)).
- **Role** The user-facing HTTP server. Hosts Inertia pages, REST/Sanctum
  endpoints, the Hatchet Worker Dashboard, the Kestra SSO passthrough.
- **Database paths**: runtime uses `pgbouncer:6432` with the `georag_app`
  role; migrations use the dedicated `pgsql_migrations` connection
  ([docker-compose.yml:538-547](../../../docker-compose.yml)) hitting
  `postgresql:5432` directly as the `georag` owner role — needed so DDL
  works against phase0-owned tables without granting `georag` to `georag_app`.
- **Other DB connections**: read-only secondary into the Hatchet engine’s
  `hatchet` DB ([docker-compose.yml:548-556](../../../docker-compose.yml)) and
  into the Kestra `kestra` DB ([docker-compose.yml:559-564](../../../docker-compose.yml)).
- **Key env**: `FASTAPI_SERVICE_KEY` (shared secret), `REVERB_*` (publisher target
  `laravel-reverb:8080`), `AWS_*` (SeaweedFS S3), `LANGFUSE_*`.
- **Healthcheck** `curl http://localhost:80/up`
- **Stop grace** 30s — Swoole worker drain.
- **Resource limit** 2 CPU / 2 GiB.

### laravel-horizon ([docker-compose.yml:640](../../../docker-compose.yml))
- **Image** `georag/laravel:latest` (same image, different command)
- **Command** `php artisan horizon`
- **Role** Redis-backed queue worker. Drives the queues defined in
  `config/horizon.php` (default, embeddings, notifications, exports). Hard
  rule #7: Horizon = user-triggered async only.
- **Healthcheck** `php artisan horizon:status | grep -E 'running|paused'`
- **Stop grace** 60s — finish in-flight embedding/export jobs.

### laravel-reverb ([docker-compose.yml:722](../../../docker-compose.yml))
- **Image** `georag/laravel:latest`
- **Command** `php artisan reverb:start --host=0.0.0.0 --port=8080`
- **Ports** `${REVERB_HOST_PORT:-8085}:8080` — browser hits `:8085`, internal
  publishers hit `laravel-reverb:8080`. See the `project_reverb_dual_purpose_env`
  memory note for why mixing those up causes 60s channel-drop timeouts.
- **Role** WebSocket server for query streaming, ingestion progress,
  workspace-data-updated cascades.
- **Healthcheck** `curl http://localhost:8080/up` (Reverb 1.x exposes `/up`).

### fastapi ([docker-compose.yml:855](../../../docker-compose.yml))
- **Image** `georag/fastapi:latest` (built from `docker/fastapi.Dockerfile`)
- **Command** `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 6 --no-access-log --proxy-headers`
  ([docker-compose.yml:867-876](../../../docker-compose.yml)). Pinned in the
  compose file because the same image is also used by the Hatchet workers, and
  a stray `docker commit` previously baked the worker CMD into the image
  ([docker-compose.yml:862-866](../../../docker-compose.yml)).
- **User** `33:33` (www-data). HF/numba/mpl/xdg caches redirected to `/tmp/*`
  because `$HOME=/var/www` is read-only for that uid
  ([docker-compose.yml:1001-1012](../../../docker-compose.yml)).
- **Role** The RAG pipeline + Pydantic AI orchestration. Owns: query handling
  (`/v1/query`, `/v1/retrieve`), embedding endpoints, citation enforcement,
  hallucination prevention, lineage writes to `answer_runs`.
- **Backends** Postgres via PgBouncer; Postgres direct via
  `POSTGRES_DIRECT_HOST=postgresql` for transaction-bound writes
  ([docker-compose.yml:910-913](../../../docker-compose.yml)); Redis; Neo4j;
  Qdrant; SeaweedFS via S3 client; vLLM at `http://vllm:8000/v1`.
- **Notable env**:
  - `LLM_BACKEND` / `LLM_PRIMARY_URL` / `LLM_PRIMARY_MODEL` — selects vLLM or
    Anthropic, with `LLM_BACKEND_FALLBACK=downshift` cross-backend failover
    ([docker-compose.yml:957-970](../../../docker-compose.yml)).
  - `AGENTIC_RETRIEVAL_V2_ENABLED` — gates the §04j LangGraph
    ([docker-compose.yml:993-999](../../../docker-compose.yml)).
  - `TIMEOUT_*_S` — per-store wait_for budgets used in the parallel-gather
    retrieval node ([docker-compose.yml:986-991](../../../docker-compose.yml)).
  - `OMP_NUM_THREADS=10`, `TOKENIZERS_PARALLELISM=false` — bge-reranker-base on
    CPU has an explicit thread budget.
- **GPU**: optional. Reserved for in-process LoRA fine-tunes of the reranker
  ([docker-compose.yml:1086-1094](../../../docker-compose.yml)) — for normal
  chat traffic the embedder + reranker run on CPU.
- **Healthcheck** `curl http://localhost:8000/health`
- **Resource limit** 5 CPU / 10 GiB.

### hatchet-worker-ingestion ([docker-compose.yml:1924](../../../docker-compose.yml))
- **Image** `georag/fastapi:latest` (reused; same Python + dependency surface)
- **Command** Bootstrap shell that pip-installs polars/pytesseract if missing,
  then optionally pulls the CUDA-12.6 PaddlePaddle wheel when
  `PADDLEOCR_USE_GPU=true` ([docker-compose.yml:1942-1952](../../../docker-compose.yml)),
  then `exec python3 -m app.hatchet_workflows.worker`.
- **Env** `WORKER_POOL=ingestion` ([docker-compose.yml:1967](../../../docker-compose.yml))
  — selects which workflows this pool registers.
- **Auth** `HATCHET_CLIENT_TOKEN` from `hatchet-admin token create`
  ([docker-compose.yml:1918-1923](../../../docker-compose.yml)).
- **Direct DB** Uses `POSTGRES_DIRECT_HOST=postgresql` not pgbouncer — needed
  for transactions and `SET LOCAL` GUCs to work cleanly.
- **OCR / PDF env** `PDF_PARSER_DOCLING_ENABLED=true`,
  `DOCLING_OCR_ENABLED=true`, `RAPIDOCR_MODEL_DIR=/tmp/rapidocr_models`,
  `PDF_PARSER_TESSERACT_FALLBACK_ENABLED=true`, `PDF_PARSE_PAGE_WORKERS=4`,
  `PARSE_SUBPROCESS_MAX_WORKERS` (empty default → `min(os.cpu_count(), 4)`)
  ([docker-compose.yml:2039-2061](../../../docker-compose.yml)).
- **Volumes** Source tree bind-mount at `/app`, the shared rapidocr ONNX
  model cache.
- **Healthcheck** `grep -q app.hatchet_workflows.worker /proc/1/cmdline`.

### hatchet-worker-ai ([docker-compose.yml:2139](../../../docker-compose.yml))
- **Image** `georag/fastapi:latest`
- **Env** `WORKER_POOL=ai` ([docker-compose.yml:2164](../../../docker-compose.yml)).
  Runs embedding (bge-small), reranker, SPLADE++ workloads.
- **GPU** Nvidia GPU reserved
  ([docker-compose.yml:2286-2292](../../../docker-compose.yml)). Memory note in
  [project_gpu_acceleration_2026_05_22](../notes/INDEX.md#project_gpu_acceleration_2026_05_22):
  bge-small went 3-4 chunks/s (CPU) → 144 chunks/s (GPU) once VLLM_GPU_MEM_UTIL
  was capped at ≤ 0.80.
- **Extra source mount** Dagster code at `/app/georag_dagster:cached`
  ([docker-compose.yml:2266-2267](../../../docker-compose.yml)) — some Hatchet
  flows reach into Dagster modules at import time.
- **Extra env** `EXTERNAL_NOTIFICATION_HMAC_SECRET`, `AUDIT_ENCRYPTION_KEY`
  (pgcrypto key for per-sender HMAC registry), `LANGFUSE_BASE_URL` overridden
  to in-network `http://langfuse-web:3000` because the worker has no
  lifespan hook to swap it at import time ([docker-compose.yml:2247-2260](../../../docker-compose.yml)).

---

## Orchestration

### hatchet-lite ([docker-compose.yml:1861](../../../docker-compose.yml))
- **Image** `ghcr.io/hatchet-dev/hatchet/hatchet-lite:latest`
- **Ports** `${HATCHET_API_PORT:-8889}:8888` (web UI + REST),
  `${HATCHET_GRPC_PORT:-7077}:7077` (workers connect here).
- **Storage** Dedicated `hatchet` logical DB on the existing postgresql
  server (`SERVER_MSGQUEUE_KIND=postgres`). Provisioned by
  [docker/postgresql/init/20-hatchet-database.sql](../../../docker/postgresql/init/20-hatchet-database.sql).
- **One-time setup** Run `hatchet-admin token create` inside the container
  and paste the JWT into `HATCHET_CLIENT_TOKEN` in `.env`
  ([docker-compose.yml:1917-1923](../../../docker-compose.yml)).
- **Healthcheck** `wget http://localhost:8888/api/ready`.

### kestra ([docker-compose.yml:2319](../../../docker-compose.yml))
- **Image** `kestra/kestra:v1.2.18`
- **Mode** `server standalone` — single-process boot, basic auth enabled, no
  Redis dependency (Postgres handles repo+queue).
- **Port** `${KESTRA_PORT:-8086}:8080`
- **Storage** Dedicated `kestra` logical DB. Flows committed under
  [kestra/flows/georag/*.yaml](../../../kestra/flows/georag/), bind-mounted
  read-only at `/app/flows`.
- **First-run admin** `KESTRA_BASIC_AUTH_USER`,
  `KESTRA_BASIC_AUTH_PASSWORD` from `.env`.
- **Healthcheck** `curl http://localhost:8081/health` — note the management
  server is on 8081, not the main 8080
  ([docker-compose.yml:2372-2376](../../../docker-compose.yml)).

### dagster-daemon ([docker-compose.yml:1665](../../../docker-compose.yml)) & dagster-webserver ([docker-compose.yml:1767](../../../docker-compose.yml))
- **Image** `georag/dagster:latest` ([docker/dagster.Dockerfile](../../../docker/dagster.Dockerfile))
- **User** `65534:65534` (nobody).
- **Storage** Dedicated `georag_dagster` DB on the postgresql server.
- **Webserver port** `${DAGSTER_WEBSERVER_PORT:-3001}:3001`.
- **Profile** `dev-ingest`, `dev-full`.
- **Definitions** [src/dagster/georag_dagster/definitions.py](../../../src/dagster/georag_dagster/definitions.py).
- **Stop grace** 120s (CRITICAL) — Dagster run workers can be mid-pipeline
  ([docker-compose.yml:1747-1750](../../../docker-compose.yml)).

---

## Data tier — always-on substrate

### postgresql ([docker-compose.yml:184](../../../docker-compose.yml))
- **Image** `georag/postgres:18-ext` — locally built from
  [docker/postgresql/Dockerfile](../../../docker/postgresql/Dockerfile),
  bundling **h3, hypopg, pg_stat_kcache, pg_partman, pg_repack, pg_ivm** on
  top of `postgis/postgis:18-3.6-alpine`
  ([docker-compose.yml:197-200](../../../docker-compose.yml)).
- **No host port** — only PgBouncer can reach `5432`. Internal `expose: "5432"`.
- **Init scripts** Run from `./docker/postgresql/init:/docker-entrypoint-initdb.d:ro`
  on first init only — these include the role/extension/Hatchet/Kestra DB
  setup. ⚠️ Note from memory: `init-roles.sql` lives *outside* this auto-init
  dir and must be applied manually on a fresh cluster
  ([project_init_roles_gap.md](../notes/INDEX.md#project_init_roles_gap)).
- **WAL archive** `archive_mode=on`, `archive_timeout=300`, writes to
  the `pg_wal_archive` named volume. Backup-agent uploads to SeaweedFS
  every 5 min (10‑min worst-case data‑loss window).
- **Auto-explain + pg_stat_kcache + pg_stat_statements** preloaded
  ([docker-compose.yml:249-260](../../../docker-compose.yml)).
- **Memory tuning** 64 GiB workstation: `shared_buffers=8GB`,
  `effective_cache_size=24GB`, `work_mem=128MB`, `maintenance_work_mem=1GB`.
  Container limit 16 GiB.
- **Healthcheck** `pg_isready -U georag -d georag`.

### pgbouncer ([docker-compose.yml:339](../../../docker-compose.yml))
- **Image** `edoburu/pgbouncer:v1.25.1-p0` (digest-pinned). Bitnami’s image
  moved behind a paywall in 2024
  ([docker-compose.yml:340-346](../../../docker-compose.yml)).
- **Pool mode** Transaction — required for asyncpg.
- **Port** Internal `6432`; the host port is the same.
- **Note** Martin, Dagster, and the Hatchet workers all bypass PgBouncer and
  go to `postgresql:5432` directly (transactions, session GUCs).

### redis ([docker-compose.yml:427](../../../docker-compose.yml))
- **Image** `redis:8.6.3-alpine` (digest-pinned).
- **Single instance shared by**: Horizon supervisor state, Laravel queues,
  sessions, cache. AOF on (`appendonly yes`, `appendfsync everysec`).
- **DB layout** 4 logical databases — `db0=queue/horizon/sessions`,
  `db1=cache`, `db2=spare`, `db3=spare`
  ([docker-compose.yml:449-462](../../../docker-compose.yml)).
- **Auth** `REDIS_PASSWORD` (empty in dev).
- **Stop grace** 15s — finish AOF fsync.

---

## Data tier — profile-gated

### neo4j ([docker-compose.yml:1111](../../../docker-compose.yml)) + neo4j-warmup ([docker-compose.yml:1221](../../../docker-compose.yml))
- **Image** `neo4j:2026-community` (digest-pinned). Resolved version
  is 2026.03.1 Community (note the doc drift call-out at
  [docker-compose.yml:1113-1118](../../../docker-compose.yml)).
- **Ports** 7474 (HTTP), 7687 (Bolt).
- **Auth** `NEO4J_AUTH=neo4j/<NEO4J_PASSWORD>` — handled by the Docker
  entrypoint, only honored on a fresh data volume. See RUNBOOK § "Neo4j
  auth migration from NEO4J_AUTH=none".
- **Memory** 4G heap (initial=max), 4G pagecache. Container limit 9 GiB.
- **Plugins** APOC auto-installed via `NEO4J_PLUGINS='["apoc"]'`, restricted
  to `apoc.*` (no APOC Extended).
- **Transactions** Server-side timeout 60s; FastAPI applies per-tool 3s
  `asyncio.wait_for` at the call site
  ([docker-compose.yml:1159-1167](../../../docker-compose.yml)).
- **Warmup** `neo4j-warmup` is a one-shot init container that runs
  `init-schema.cypher` then `warmup.cypher` to pre-populate the page cache
  (replaces Enterprise-only `db.memory.pagecache.warmup.enable`).

### qdrant ([docker-compose.yml:1259](../../../docker-compose.yml))
- **Image** `qdrant/qdrant:v1.17` (digest-pinned).
- **Ports** 6333 (HTTP), 6334 (gRPC).
- **HNSW** `m=32`, `ef_construct=256`, `ef=200`, max indexing threads=4
  ([docker-compose.yml:1268-1280](../../../docker-compose.yml)).
- **WAL** 256 MiB cap per collection
  ([docker-compose.yml:1290-1294](../../../docker-compose.yml)).
- **Auth** Off by default. ⚠️ `QDRANT__SERVICE__API_KEY=""` would *enable*
  auth with empty-key expectation, so it’s deliberately unset
  ([docker-compose.yml:1295-1306](../../../docker-compose.yml)).
- **Quantisation** Set per-collection in
  `index_public_geoscience.py::_ensure_collection` and
  `index_reports.py::_ensure_collection` — cluster-level defaults removed
  to prevent dead-config confusion
  ([docker-compose.yml:1281-1288](../../../docker-compose.yml)).
- **Healthcheck** Raw bash `/dev/tcp` HTTP probe to `/readyz` — the image
  ships bash but no curl/wget.

### minio (SeaweedFS) ([docker-compose.yml:1360](../../../docker-compose.yml)) + minio-init ([docker-compose.yml:1410](../../../docker-compose.yml))
- **Image** `chrislusf/seaweedfs:4.20` (per [ADR-0001](../../adr/) — MinIO
  went AGPL v3 then archived its repo Feb 2026).
- **Ports** `8333` (S3 API; was 9000 under MinIO), `8888` (filer HTTP).
- **Bucket bootstrap** `minio-init` uses `minio/mc` against the S3 API to
  idempotently create `bronze`, `exports`, `bronze-raster`, `georag-backups`,
  `tier-hot`, `tier-warm`, `tier-cold`
  ([docker-compose.yml:1416-1431](../../../docker-compose.yml)).
- **Backward-compat env** All Python/PHP code still reads `MINIO_*` /
  `AWS_*` vars; SeaweedFS only sees the values, not the names.
- **Container name** Stays `georag-minio` to avoid breaking depending
  services that hard-code the network alias `minio`.

### martin ([docker-compose.yml:795](../../../docker-compose.yml))
- **Image** `ghcr.io/maplibre/martin:1.7.0` (digest-pinned). Bumped from 1.5.0
  to 1.7.0 for the native `/metrics` endpoint that powers the four alert rules
  in [docker/prometheus/rules/martin-alerts.yml](../../../docker/prometheus/rules/martin-alerts.yml).
- **Port** `${MARTIN_PORT:-3002}:3000`.
- **Config** [docker/martin/martin.yaml](../../../docker/martin/martin.yaml) —
  function sources from `silver.pg_*` + table sources from `public_geo.v_pg_*_mvt`.
  See Chapter 09.
- **Database** `DATABASE_URL` points at `postgresql:5432` directly (Martin uses
  persistent connections incompatible with PgBouncer transaction pooling).
- **Healthcheck** `wget /health`.

---

## LLM

### vllm ([docker-compose.yml:1476](../../../docker-compose.yml)) + vllm-warmup ([docker-compose.yml:1629](../../../docker-compose.yml))
- **Image** `vllm/vllm-openai:v0.21.0` (digest-pinned).
- **Profile** `gpu-llm` (alias `gpu-llm-prod`).
- **Port** `${VLLM_PORT:-8001}:8000`.
- **Model** Default `Qwen/Qwen3-14B-AWQ` — quantisation `awq_marlin`,
  `max-model-len=16384`, `gpu-memory-utilization=0.93`, `kv-cache-dtype=fp8`,
  `max-num-seqs=12`, prefix caching + chunked prefill on,
  speculative-decoding ngram (`num_speculative_tokens=2`)
  ([docker-compose.yml:1518-1577](../../../docker-compose.yml)).
- **GPU** A4500 (20 GB VRAM, Ampere SM 8.6). KV cache FP8 storage / FP16
  compute. Cudagraph capture trimmed to `[1,2,4,8,12]` to free ~1-3 GiB the
  graph buffers were reserving but couldn’t use at `max-num-seqs ≤ 8`.
- **IPC** Host IPC namespace + 4 GiB shm.
- **Warmup sidecar** Fires 5 throwaway 16-token completions once vLLM is
  healthy — burns the FlashInfer JIT tax (~28 tok/s cold → ~154 tok/s warm).
- **Healthcheck start_period** 600s — covers a cold first-pull (~17 GB AWQ
  download) on a fresh host.

---

## Observability

### otel-collector ([docker-compose.yml:2440](../../../docker-compose.yml))
- **Image** `otel/opentelemetry-collector-contrib:0.151.0` (distroless — no
  shell utilities, hence no healthcheck; Prometheus scrapes its `/metrics`
  on 8888 and an alert fires on absence-of-samples
  ([docker-compose.yml:2453-2461](../../../docker-compose.yml))).
- **Ports** 4317 (OTLP/gRPC), 4318 (OTLP/HTTP), 13133 (health_check ext).
- **Config** [docker/otel-collector/otel-collector-config.yaml](../../../docker/otel-collector/otel-collector-config.yaml).
- **Pipeline** Receives spans/metrics/logs from Laravel, FastAPI, Hatchet
  workers, Dagster, vLLM. Forwards traces to Tempo, exports metrics for
  Prometheus.

### tempo ([docker-compose.yml:2475](../../../docker-compose.yml))
- **Image** `grafana/tempo:2.6.1`
- **Port** 3200 (HTTP API + UI)
- **Config** [docker/tempo/tempo-config.yaml](../../../docker/tempo/tempo-config.yaml).
- **Storage** Local-disk blocks on the `tempo_data` volume.

### prometheus ([docker-compose.yml:2502](../../../docker-compose.yml))
- **Image** `prom/prometheus:v3.11.3` (digest-pinned). `wget`, not `curl`, in
  healthcheck — busybox-based image
  ([docker-compose.yml:2523-2528](../../../docker-compose.yml)).
- **Retention** 7d.
- **Config** [docker/prometheus/prometheus.yml](../../../docker/prometheus/prometheus.yml) + rules under [docker/prometheus/rules/](../../../docker/prometheus/rules/).

### alertmanager ([docker-compose.yml:2542](../../../docker-compose.yml))
- **Image** `prom/alertmanager:v0.32.1` (digest-pinned).
- **Config** [docker/alertmanager/alertmanager.yml](../../../docker/alertmanager/alertmanager.yml). Webhook receiver — operator wires Slack/email per env.

### redis_exporter / postgres_exporter / neo4j_exporter
([docker-compose.yml:2571](../../../docker-compose.yml), [2600](../../../docker-compose.yml), [2641](../../../docker-compose.yml))
- `oliver006/redis_exporter:v1.74.0-alpine` on :9121
- `prometheuscommunity/postgres-exporter:v0.17.1` on :9187 (direct PG; auto-discovers DBs)
- Custom `georag/neo4j-exporter:latest` ([docker/neo4j-exporter/](../../../docker/neo4j-exporter/)) on :9105 — polls Neo4j JMX over Bolt and exposes Prometheus exposition, because Neo4j 2026 Community rejects the Enterprise-only `server.metrics.prometheus.*`.

### loki ([docker-compose.yml:2679](../../../docker-compose.yml)) + promtail ([docker-compose.yml:2706](../../../docker-compose.yml))
- Loki 3.4.2 / Promtail 3.4.2. Promtail tails container stdout via the
  Docker socket plus the Laravel `storage/logs/authz_audit-*.log` files
  (mounted read-only). Grafana queries via LogQL.

### grafana ([docker-compose.yml:2738](../../../docker-compose.yml))
- **Image** `grafana/grafana:11.6.1` (digest-pinned).
- **Port** 3000.
- **Provisioning** [docker/grafana/provisioning](../../../docker/grafana/provisioning) (datasources + dashboard providers) and [docker/grafana/dashboards](../../../docker/grafana/dashboards).

### ofelia ([docker-compose.yml:2788](../../../docker-compose.yml)) + backup-agent ([docker-compose.yml:2831](../../../docker-compose.yml))
- **Ofelia** Cron-style sidecar that fires `job-exec` against
  postgresql/neo4j/qdrant containers to run the backup scripts under each
  `docker/<service>/backup.sh`.
- **backup-agent** `georag/backup-agent:latest`
  ([docker/backup-agent/Dockerfile](../../../docker/backup-agent/Dockerfile))
  reads the staging volume + `pg_wal_archive` and uploads to SeaweedFS
  every 5 min (PG WAL) and on cron (per-store snapshots).

---

## Override compose files

These are not normally used — they exist for staged operations.

| File | What it adds |
|------|--------------|
| [docker/compose.exporters.yml](../../../docker/compose.exporters.yml) | Additional Prometheus exporters |
| [docker/compose.langfuse.yml](../../../docker/compose.langfuse.yml) | Self-hosted Langfuse (langfuse-web + langfuse-worker + ClickHouse) |
| [docker/compose.redis-staging.yml](../../../docker/compose.redis-staging.yml) | Separate Redis instance for staging tests |
| [docker/compose.vllm.yml](../../../docker/compose.vllm.yml) | Alternate vLLM configurations |
| [docker/compose.wal-archiving.yml](../../../docker/compose.wal-archiving.yml) | WAL-archiving-only stack (DR builds) |

---

## Resource budget snapshot (dev workstation, 64 GiB RAM, A4500)

| Service | CPU limit | Mem limit | Why |
|---|---|---|---|
| postgresql | 6.0 | 16G | `shared_buffers=8G` + WAL buffers + OS page cache headroom |
| neo4j | 4.0 | 9G | 4G heap + 4G pagecache + headroom |
| qdrant | 2.0 | 4G | HNSW build + serve |
| redis | 1.0 | 1G | Cache + queues; AOF on |
| laravel-octane | 2.0 | 2G | Swoole workers |
| laravel-horizon | 2.0 | 1G | Queue worker pool |
| fastapi | 5.0 | 10G | Embedder + reranker + uvicorn × 6 |
| dagster-daemon | 1.0 | 3G | SPLADE + dense encoder + daemon base |
| martin | 0.5 | 512M | Tile cache pinned at 512 MB |
| vllm | (none) | (none) | Intentionally unlimited — CUDA runtime is sensitive |
