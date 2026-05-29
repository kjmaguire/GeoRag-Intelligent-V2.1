# GeoRAG Service Inventory — 2026-04-19
<!-- Phase A, Item A1 -->
<!-- Authority: docker-compose.yml (58 KB, 2026-04-19), docker compose ps output, docker stats snapshot -->
<!-- Status: Static analysis + live runtime observation -->

## How to read this table

- **Profile** — compose profiles the service belongs to; blank = always starts (no profile required)
- **Image pin** — third-party images show exact tag; `georag/*:latest` means last local build per §12 convention
- **Healthcheck** — fidelity rating: `HTTP` = real HTTP endpoint, `TCP` = port open only, `CMD` = process check, `DISABLED` = healthcheck.disable=true
- **stop_grace_period** — `--` means compose default (10 s) is applied; no service declares an explicit override

---

## Service Inventory Table

| Service | Container | Profile | Image Pin | Command (summary) | Healthcheck | HC Fidelity | Restart | depends_on (condition) | stop_grace_period | Ports (host→container) | Volumes | Networks | Env Source |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| postgresql | georag-postgresql | _(always)_ | `postgis/postgis:18-3.6-alpine` | `postgres -c shared_buffers=4GB …` (18 tuning flags) | `pg_isready -U georag -d georag` | CMD/native | `unless-stopped` | — | `--` (10 s default) | _(internal only, 5432 not exposed)_ | `postgres_data:/var/lib/postgresql/data`, `./docker/postgresql/init:/docker-entrypoint-initdb.d:ro` | georag | `.env` |
| pgbouncer | georag-pgbouncer | _(always)_ | `edoburu/pgbouncer:v1.25.1-p0` | _(image entrypoint)_ | `pg_isready -h 127.0.0.1 -p 6432 -U georag` | TCP-via-pg_isready | `unless-stopped` | `postgresql` (healthy) | `--` (10 s default) | `6432→6432` | — | georag | `.env` |
| redis | georag-redis | _(always)_ | `redis:8.6.2-alpine` | `redis-server --maxmemory 512mb --appendonly yes --appendfsync everysec …` | `redis-cli -a $REDIS_PASSWORD ping \| grep PONG` | CMD/native | `unless-stopped` | — | `--` (10 s default) | `6379→6379` (host port from `$REDIS_PORT`) | `redis_data:/data` | georag | `.env` |
| laravel-octane | georag-laravel-octane | `dev-light`, `dev-full` | `georag/laravel:latest` | `sh -c "… php artisan octane:start --host=0.0.0.0 --port=80 --server=swoole --workers=4 …"` | `curl -f http://localhost:80/up` | HTTP | `unless-stopped` | `pgbouncer` (healthy), `redis` (healthy) | `--` (10 s default) | `8888→80` | `.:/app:cached` | georag | `.env` |
| laravel-horizon | georag-laravel-horizon | `dev-light`, `dev-full` | `georag/laravel:latest` | `php artisan horizon` | `php artisan horizon:status \| grep -E 'running\|paused'` | CMD | `unless-stopped` | `pgbouncer` (healthy), `redis` (healthy), `laravel-octane` (**started**) | `--` (10 s default) | _(none)_ | `.:/app:cached` | georag | `.env` |
| laravel-reverb | georag-laravel-reverb | `dev-light`, `dev-full` | `georag/laravel:latest` | `php artisan reverb:start --host=0.0.0.0 --port=8080` | `curl -sf -o /dev/null http://localhost:8080/ \|\| test $$? -eq 22` | HTTP (weak — accepts HTTP 4xx as healthy) | `unless-stopped` | `redis` (healthy), `laravel-octane` (**started**) | `--` (10 s default) | `8085→8080` | `.:/app:cached` | georag | `.env` |
| martin | georag-martin | `dev-light`, `dev-full` | `ghcr.io/maplibre/martin:1.5.0` | `--config /config/martin.yaml` | **DISABLED** | DISABLED | `unless-stopped` | `postgresql` (healthy) | `--` (10 s default) | `3002→3000` | `./docker/martin/martin.yaml:/config/martin.yaml:ro` | georag | `.env` |
| fastapi | georag-fastapi | `dev-light`, `dev-full` | `georag/fastapi:latest` | _(image entrypoint — uvicorn)_ | `curl -f http://localhost:8000/health` | HTTP | `unless-stopped` | `pgbouncer` (healthy), `redis` (healthy) | `--` (10 s default) | `8000→8000` | `./src/fastapi:/app:cached`, `fastapi_hf_cache:/tmp/hf_cache` | georag | `.env` |
| neo4j | georag-neo4j | `dev-data`, `dev-full` | `neo4j:2026.02.3-community` _(pin in compose)_ / **`neo4j:2026-community`** _(tag pulled)_ | _(image entrypoint — tini)_ | `cypher-shell -a bolt://localhost:7687 'RETURN 1'` | CMD/native | `unless-stopped` | — | `--` (10 s default) | `7474→7474`, `7687→7687` | `neo4j_data:/data`, `neo4j_logs:/logs`, `neo4j_plugins:/plugins`, `./docker/neo4j/conf:/conf:ro` | georag | `.env` |
| neo4j-warmup | georag-neo4j-warmup | `dev-data`, `dev-full` | `neo4j:2026.02.3-community` | `sh -c "until cypher-shell … ; schema init … warmup …"` | _(none — one-shot)_ | N/A | `"no"` | `neo4j` (healthy) | N/A | — | `./docker/neo4j/warmup.cypher:ro`, `./docker/neo4j/init-schema.cypher:ro` | georag | hardcoded in entrypoint |
| qdrant | georag-qdrant | `dev-data`, `dev-full` | `qdrant/qdrant:v1.17` | _(image entrypoint)_ | `bash -c 'echo > /dev/tcp/localhost/6333'` | TCP only | `unless-stopped` | — | `--` (10 s default) | `6333→6333`, `6334→6334` | `qdrant_data:/qdrant/storage` | georag | `.env` |
| minio (SeaweedFS) | georag-minio | `dev-data`, `dev-full` | `chrislusf/seaweedfs:4.20` | `sh /usr/local/bin/entrypoint.sh` → `weed server -dir=/data -volume -filer -s3 -s3.port=8333` | `wget -qO- http://127.0.0.1:9333/cluster/status` | HTTP (master status) | `unless-stopped` | — | `--` (10 s default) | `8333→8333` (API), `8888→8888` (filer UI). **Note: live shows 9002→8333, 9003→8888 — port conflict override in .env** | `minio_data:/data`, `./docker/seaweedfs/entrypoint.sh:/usr/local/bin/entrypoint.sh:ro` | georag | `.env` |
| minio-init | georag-minio-init | `dev-data`, `dev-full` | `minio/mc:RELEASE.2025-04-08T15-39-49Z` | `sh -c "mc alias set … && mc mb …"` | _(none — one-shot)_ | N/A | `"no"` | `minio` (healthy) | N/A | — | — | georag | `.env` |
| ollama | georag-ollama | `dev-llm`, `dev-full` | `ollama/ollama:0.21.0` | _(image entrypoint — ollama serve)_ | `ollama list` | CMD | `unless-stopped` | — | `--` (10 s default) | `11434→11434` | `ollama_models:/root/.ollama` | georag | `.env` |
| vllm | georag-vllm | `gpu-llm-prod` | `vllm/vllm-openai:v0.19.1` | `--model deepseek-ai/… --port 8000 …` | `python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health', timeout=5)"` | HTTP | `unless-stopped` | — | 600 s start_period | `8001→8000` | `vllm_hf_cache:/root/.cache/huggingface` | georag | `.env` |
| dagster-daemon | georag-dagster-daemon | `dev-ingest`, `dev-full` | `georag/dagster:latest` | `dagster-daemon run` | `dagster-daemon liveness-check` | CMD | `unless-stopped` | `pgbouncer` (healthy) | `--` (10 s default) | — | `dagster_home`, `./src/dagster:/opt/dagster/app:cached`, `dagster.yaml:ro`, `workspace.yaml:ro` | georag | `.env` |
| dagster-webserver | georag-dagster-webserver | `dev-ingest`, `dev-full` | `georag/dagster:latest` | `dagster-webserver --host 0.0.0.0 --port 3001` | `curl -f http://localhost:3001/health` | HTTP | `unless-stopped` | `pgbouncer` (healthy), `dagster-daemon` (**started**) | `--` (10 s default) | `3001→3001` | `dagster_home`, `./src/dagster:/opt/dagster/app:cached` | georag | `.env` |
| ragflow | georag-ragflow | `dev-ingest`, `dev-full` | `infiniflow/ragflow:v0.17.2` | _(image entrypoint)_ | `curl -f http://localhost:9380/health` | HTTP | `unless-stopped` | `pgbouncer` (healthy), `redis` (healthy) | `--` (10 s default) | `9380→9380` | `ragflow_data:/ragflow/data` | georag | `.env` |
| prometheus | georag-prometheus | `dev-monitor`, `dev-full` | `prom/prometheus:v3.3.1` | `--config.file=… --storage.tsdb.retention.time=7d --web.enable-lifecycle` | `curl -f http://localhost:9090/-/healthy` | HTTP | `unless-stopped` | — | `--` (10 s default) | `9090→9090` | `./docker/prometheus/prometheus.yml:ro`, `./docker/prometheus/rules:ro` | georag | `.env` |
| grafana | georag-grafana | `dev-monitor`, `dev-full` | `grafana/grafana:11.6.1` | _(image entrypoint)_ | `curl -f http://localhost:3000/api/health` | HTTP | `unless-stopped` | `prometheus` (healthy) | `--` (10 s default) | `3000→3000` | `grafana_data`, `./docker/grafana/provisioning:ro`, `./docker/grafana/dashboards:ro` | georag | `.env` |

---

## Named Volumes

| Volume | Driver | Stateful? | Backup status |
|---|---|---|---|
| postgres_data | local | YES — primary DB | Partial (see A3) |
| neo4j_data | local | YES — graph | None defined |
| neo4j_logs | local | NO — logs only | — |
| neo4j_plugins | local | YES — APOC plugin | Reconstructible from image |
| qdrant_data | local | YES — vector index | None defined |
| redis_data | local | YES — queue + cache | None defined |
| minio_data | local | YES — object store | One-time migration snapshot only |
| ollama_models | local | YES — model weights | Reconstructible (re-pull) |
| vllm_hf_cache | local | YES — model weights | Reconstructible (re-pull) |
| fastapi_hf_cache | local | NO — embedding model cache | Reconstructible |
| grafana_data | local | YES — dashboard state | None defined |
| dagster_home | local | YES — run history | None defined |
| ragflow_data | local | YES — chunked docs | None defined |

---

## Bind Mounts (Windows/WSL2 path risk)

| Service | Host path | Container path | Risk |
|---|---|---|---|
| laravel-octane/horizon/reverb | `.:/app:cached` | `/app` | Entire project tree. Windows NTFS; exec bits not preserved. |
| fastapi | `./src/fastapi:/app:cached` | `/app` | Python source. Exec bits not preserved. |
| dagster-daemon/webserver | `./src/dagster:/opt/dagster/app:cached` | `/opt/dagster/app` | Python source. Exec bits not preserved. |
| neo4j | `./docker/neo4j/conf:/conf:ro` | `/conf` | Config files only — OK |
| neo4j-warmup | `./docker/neo4j/warmup.cypher:ro`, `./docker/neo4j/init-schema.cypher:ro` | `/scripts/` | Cypher scripts — OK (not executable) |
| martin | `./docker/martin/martin.yaml:/config/martin.yaml:ro` | `/config/martin.yaml` | Config — OK |
| minio (SeaweedFS) | `./docker/seaweedfs/entrypoint.sh:/usr/local/bin/entrypoint.sh:ro` | `/usr/local/bin/entrypoint.sh` | **Exec bit risk** — mitigated by `entrypoint: ["sh", "…"]` |
| prometheus | `./docker/prometheus/prometheus.yml:ro`, `./docker/prometheus/rules:ro` | `/etc/prometheus/` | Config — OK |
| grafana | `./docker/grafana/provisioning:ro`, `./docker/grafana/dashboards:ro` | `/etc/grafana/`, `/var/lib/grafana/dashboards` | Config — OK |
| postgresql | `./docker/postgresql/init:/docker-entrypoint-initdb.d:ro` | `/docker-entrypoint-initdb.d` | Init SQL/sh scripts |

---

_Generated: 2026-04-19 | Source: docker-compose.yml static analysis + docker compose ps + docker stats_
