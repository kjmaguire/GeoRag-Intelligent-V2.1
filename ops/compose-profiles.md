# GeoRAG Docker Compose Profiles

**Authority:** `01-infrastructure-orchestration.md` §D4, CLAUDE.md §11
**Last updated:** 2026-04-19 (Module 1 Phase B)

---

## Overview

The stack is organized into profiles to avoid melting the dev workstation (AMD Ryzen 8-core, 64GB RAM, RTX 4080 16GB VRAM). Kyle uses the machine for other work — never run all profiles simultaneously unless doing deliberate end-to-end testing.

Services with no profile are always-on infrastructure (PostgreSQL, PgBouncer, Redis). All other services require at least one profile to start.

---

## Profile Definitions

| Profile | Purpose | When to use |
|---|---|---|
| _(none)_ | Core infrastructure: PostgreSQL 18 + PostGIS, PgBouncer, Redis | Always. These start automatically; do not need a `--profile` flag. |
| `dev-light` | Laravel (Octane + Horizon + Reverb) + FastAPI + Martin | Daily development. The minimum application layer for UI + API work. |
| `dev-data` | Neo4j + warmup init, Qdrant, SeaweedFS (MinIO) + bucket init, backup agent, Ofelia | Start when working on graph, vector, or object storage features. |
| `dev-llm` | Ollama (GPU) | Start only when testing LLM chat locally. `OLLAMA_KEEP_ALIVE=5m` auto-unloads the model after 5 minutes of inactivity — frees VRAM for other work. |
| `dev-ingest` | Dagster daemon + webserver, RAGFlow (deferred — stopped) | Start only during ingestion pipeline development. Heavy memory footprint. |
| `dev-monitor` | Prometheus + Grafana | Skip by default. Start when you need metrics dashboards. |
| `dev-full` | Everything | Use only for end-to-end integration testing. Never leave running unattended. |
| `gpu-llm-prod` | vLLM (GPU, production inference) | Production-shaped testing only. Requires a compatible GPU with sufficient VRAM for the target model. |
| `staging` | 3-instance Redis topology (cache / queue / sessions) + per-instance exporters | Staging deployments. Layered on top of `dev-light`+`dev-data` via `-f docker/compose.redis-staging.yml`. See `ops/runbooks/redis-3-instance-rollout.md`. |
| `prod` | Same set as `staging` — 3-instance Redis with role-separated eviction policies | Production deployments. Identical service shape to `staging`; differences are env-only (passwords, memory limits, resource reservations). |

---

## Service × Profile Matrix

| Service | Container | none | dev-light | dev-data | dev-llm | dev-ingest | dev-monitor | dev-full | gpu-llm-prod | staging | prod |
|---|---|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| postgresql | georag-postgresql | YES | | | | | | YES | | YES | YES |
| pgbouncer | georag-pgbouncer | YES | | | | | | YES | | YES | YES |
| redis | georag-redis | YES | | | | | | YES | | | |
| redis-cache | georag-redis-cache | | | | | | | | | YES | YES |
| redis-queue | georag-redis-queue | | | | | | | | | YES | YES |
| redis-sessions | georag-redis-sessions | | | | | | | | | YES | YES |
| redis_exporter_cache | georag-redis-exporter-cache | | | | | | | | | YES | YES |
| redis_exporter_queue | georag-redis-exporter-queue | | | | | | | | | YES | YES |
| redis_exporter_sessions | georag-redis-exporter-sessions | | | | | | | | | YES | YES |
| laravel-octane | georag-laravel-octane | | YES | | | | | YES | |
| laravel-horizon | georag-laravel-horizon | | YES | | | | | YES | |
| laravel-reverb | georag-laravel-reverb | | YES | | | | | YES | |
| martin | georag-martin | | YES | | | | | YES | |
| fastapi | georag-fastapi | | | YES | | | | YES | |
| neo4j | georag-neo4j | | | YES | | | | YES | |
| neo4j-warmup | georag-neo4j-warmup | | | YES | | | | YES | |
| qdrant | georag-qdrant | | | YES | | | | YES | |
| minio (SeaweedFS) | georag-minio | | | YES | | | | YES | |
| minio-init | georag-minio-init | | | YES | | | | YES | |
| backup-agent | georag-backup-agent | | | YES | | | | YES | |
| ofelia | georag-ofelia | | | YES | | | | YES | |
| ollama | georag-ollama | | | | YES | | | YES | |
| dagster-daemon | georag-dagster-daemon | | | | | YES | | YES | |
| dagster-webserver | georag-dagster-webserver | | | | | YES | | YES | |
| ragflow | georag-ragflow | | | | | YES | | YES | |
| prometheus | georag-prometheus | | | | | | YES | YES | |
| grafana | georag-grafana | | | | | | YES | YES | |
| vllm | georag-vllm | | | | | | | | YES |

**Note:** FastAPI moved to dev-data on 2026-04-19 — depends_on service_healthy on neo4j/qdrant/minio requires them to be in the same `up -d` invocation.

---

## Usage Examples

```sh
# Minimal — core infra only (always on)
docker compose up -d

# Daily development (infra + app layer)
docker compose --profile dev-light up -d

# Full backend including graph/vector/object storage
docker compose --profile dev-light --profile dev-data up -d

# LLM testing (requires NVIDIA Container Toolkit)
docker compose --profile dev-light --profile dev-llm up -d

# Ingestion pipeline development
docker compose --profile dev-light --profile dev-data --profile dev-ingest up -d

# Metrics dashboards on demand
docker compose --profile dev-monitor up -d

# Everything — end-to-end integration testing only
docker compose --profile dev-full up -d

# Production-shaped LLM inference (separate from dev-llm)
docker compose --profile gpu-llm-prod up -d vllm
```

---

## Restart Policy Convention

Documented here per B4 (Module 1 Phase B, 2026-04-19). Also referenced in the compose file header.

| Policy | Services | Reason |
|---|---|---|
| `restart: unless-stopped` | All long-running infrastructure and application services | Automatic recovery on crash or unexpected stop. Operator can stop intentionally (e.g., maintenance window) and the service will not restart. |
| `restart: "no"` | `neo4j-warmup`, `minio-init` | One-shot init containers. They run once per `docker compose up`, exit 0 by design, and must NOT restart on completion — doing so would re-run migrations or warmup unnecessarily. |

No `restart: on-failure:N` is used in this stack. All helpers are either `unless-stopped` or `"no"`.

---

## Healthcheck Fidelity Summary (B1, 2026-04-19)

| Service | Healthcheck Type | Endpoint / Command |
|---|---|---|
| postgresql | Native | `pg_isready -U georag -d georag` |
| pgbouncer | Admin SQL | `psql -d pgbouncer -c 'SHOW POOLS'` (verifies proxy is up, not just port open) |
| redis | Native | `redis-cli ping \| grep PONG` |
| laravel-octane | HTTP | `curl -f http://localhost:80/up` |
| laravel-horizon | Process CLI | `php artisan horizon:status \| grep running\|paused` |
| laravel-reverb | HTTP | `curl -f http://localhost:8080/up` (Reverb 1.x built-in endpoint, returns `{"health":"OK"}`) |
| martin | HTTP (wget) | `wget --spider -q http://127.0.0.1:3000/health` (martin has wget; no curl in distroless-adjacent image) |
| fastapi | HTTP | `curl -f http://localhost:8000/health` |
| neo4j | Bolt+Auth | `cypher-shell -a bolt://localhost:7687 -u neo4j -p ... 'RETURN 1'` |
| qdrant | HTTP (bash tcp) | `bash -c 'exec 3<>/dev/tcp/localhost/6333 && ... grep -q 200 OK <&3'` (qdrant has bash, no curl/wget; /readyz is the real readiness endpoint) |
| minio (SeaweedFS) | HTTP | `wget -qO- http://127.0.0.1:9333/cluster/status` (master API) |
| ollama | CLI | `ollama list` |
| dagster-daemon | CLI | `dagster-daemon liveness-check` |
| dagster-webserver | HTTP | `curl -f http://localhost:3001/health` |
| prometheus | HTTP | `curl -f http://localhost:9090/-/healthy` |
| grafana | HTTP | `curl -f http://localhost:3000/api/health` |
| vllm | HTTP (python) | `urllib.request.urlopen('http://localhost:8000/health')` |

---

## B2 Startup Ordering (service_healthy depends_on)

| Service | Depends on (service_healthy) | Notes |
|---|---|---|
| pgbouncer | postgresql | Core chain |
| laravel-octane | pgbouncer, redis | App cannot start without DB pool + queue |
| laravel-horizon | pgbouncer, redis | Horizon needs both for job processing |
| laravel-reverb | redis | WebSocket server needs pub/sub |
| martin | postgresql | **Direct connection, NOT pgbouncer** — locked by §04d-tile |
| fastapi | pgbouncer, redis, neo4j, qdrant, minio | All four backend stores must be ready |
| dagster-daemon | pgbouncer | Dagster instance storage (direct to postgresql for persistent connections) |
| dagster-webserver | pgbouncer, dagster-daemon | Webserver waits for daemon healthy (upgraded from service_started) |
| ofelia | postgresql, neo4j, qdrant | Scheduler waits for all backup targets healthy |
| grafana | prometheus | Datasource must exist before Grafana loads |
| neo4j-warmup | neo4j | Init container runs warmup.cypher after Neo4j is healthy |
| minio-init | minio | Bucket provisioning waits for SeaweedFS healthy |
| backup-agent | minio | Backup uploads go to SeaweedFS |

---

## Volume Safety Rules

- **Never run `docker volume rm` on any named volume** — all are stateful (postgres_data, neo4j_data, qdrant_data, redis_data, minio_data) or contain model weights (ollama_models, vllm_hf_cache) that take hours to re-download.
- **Never run `docker compose down -v`** — this removes all named volumes.
- **postgres_data** contains a hand-added `pg_hba.conf` replication ACL (`172.19.0.0/16 scram-sha-256`) that `georag-backup-agent` depends on. This entry persists across container restarts but NOT across fresh volume provisioning. See `docs/RUNBOOK.md` for the pg_hba rebuild procedure.

---

_See also: `ops/audit/2026-04-19-infra-audit.md` (Phase A findings), `ops/audit/2026-04-19-infra-phase-b-critical-fixes.md` (Phase B evidence)._
