# GeoRAG service inventory

**Last refreshed:** 2026-05-14 (Phase F.5)
**Purpose:** One-stop reference for the 24 containers that make up a full
GeoRAG deployment. Use this on-call to answer "what is this thing, who
talks to it, and how do I tell if it's actually broken vs. just lying
about its healthcheck."

## How to read this doc

* **Profile** — Docker Compose profile flag(s) that include the service.
  `default` means the service starts on a plain `docker compose up`.
  Profiles like `dev-monitor` / `dev-full` are opt-in via
  `--profile dev-full` or `COMPOSE_PROFILES=dev-full`.
* **Healthcheck status** — what the container's healthcheck command does
  AND whether it actually reflects reality. Some images (distroless,
  scratch-based) can't run shell commands at all, so a "false unhealthy"
  is sometimes the truth.
* **Talks to** — the other services it actively connects to.
* **Failure mode hint** — first thing to check when the on-call alarm
  fires for this service.

## Application tier

| Service | Image | Profile | Healthcheck | Talks to | Failure-mode first check |
|---|---|---|---|---|---|
| caddy | `caddy:2.8-alpine` | default | `wget /` — accurate | (front door) | `docker compose logs caddy` for TLS / upstream errors |
| laravel-octane | `georag/laravel:latest` | default | `php artisan octane:status` — accurate | pgbouncer · redis · fastapi · reverb · seaweedfs | `docker compose logs laravel-octane`; check `php artisan octane:status` inside |
| laravel-horizon | `georag/laravel:latest` | default | Horizon `horizon:status` — accurate | pgbouncer · redis | `php artisan horizon:status`; queues backed up = redis pressure |
| laravel-reverb | `georag/laravel:latest` | default | WebSocket ping — accurate | redis | `wscat -c ws://localhost:8080` from host |
| fastapi | `georag/fastapi:latest` | default | `curl /health` (image installs curl) — accurate | pgbouncer · redis · qdrant · neo4j · vllm · seaweedfs · hatchet-lite | `curl :8000/health`; check uvicorn child-process death loop in logs |
| hatchet-worker-ingestion | `georag/fastapi:latest` | default | `grep app.hatchet_workflows.worker /proc/1/cmdline` — accurate | hatchet-lite · pgbouncer · qdrant · neo4j · seaweedfs | `docker compose logs hatchet-worker-ingestion`; check WORKER_POOL=ingestion |
| hatchet-worker-ai | `georag/fastapi:latest` | default | same as above | hatchet-lite · pgbouncer · qdrant · neo4j · seaweedfs | same; WORKER_POOL=ai |
| kestra | `kestra/kestra:v1.2.18` | default | Java HTTP probe — flaky during boot, accurate once running | postgresql | `docker compose logs kestra`; Java apps take 60-90s to boot |
| hatchet-lite | `ghcr.io/hatchet-dev/hatchet/hatchet-lite:latest` | default | Hatchet engine HTTP probe — accurate | postgresql (own DB) | Hatchet engine UI at `http://localhost:7070` |

## Data tier

| Service | Image | Profile | Healthcheck | Talks to | Failure-mode first check |
|---|---|---|---|---|---|
| postgresql | `georag/postgres:18-ext` | default | `pg_isready` — accurate | (foundational) | `psql -U georag` inside; check `pg_stat_activity` for stuck queries |
| pgbouncer | `edoburu/pgbouncer:1.25.1-p0` | default | `pg_isready` against the pool — accurate | postgresql | `psql -p 6432 -U pgbouncer pgbouncer` then `SHOW POOLS` |
| neo4j | `neo4j:2026-community` | default | `cypher-shell RETURN 1` — accurate | (none upstream) | Neo4j Browser at `http://localhost:7474` |
| qdrant | `qdrant/qdrant:v1.17` | default | `/readyz` — accurate | (none upstream) | `curl :6333/collections` from host |
| redis | `redis:8.6.3-alpine` | default | `redis-cli ping` — accurate | (foundational) | `redis-cli -a $REDIS_PASSWORD ping` |
| clickhouse | `clickhouse/clickhouse-server:24.10-alpine` | default | `wget /ping` — accurate | (Langfuse backend) | `docker compose logs clickhouse`; Langfuse depends on it |
| minio | `chrislusf/seaweedfs:4.20` | default | `curl /cluster/healthz` — accurate (S3 gateway) | (foundational) | Despite the container name being `minio`, this is SeaweedFS per ADR-0001 |
| martin | `ghcr.io/maplibre/martin:1.7.0` | default | `wget /health` — accurate | postgresql (PostGIS) | `curl :3000/catalog` for tile sources |

## LLM tier

| Service | Image | Profile | Healthcheck | Talks to | Failure-mode first check |
|---|---|---|---|---|---|
| vllm | `vllm/vllm-openai:v0.19.1` | default | `/health` — accurate | (none upstream) | `curl :8001/v1/models` should show Qwen3-30B; logs show GPU mem |

## Audit / observability tier

| Service | Image | Profile | Healthcheck | Talks to | Failure-mode first check |
|---|---|---|---|---|---|
| langfuse-web | `langfuse/langfuse:3` | default | HTTP `/api/public/health` — accurate | postgresql · clickhouse · redis · seaweedfs | UI at `http://localhost:3000` |
| langfuse-worker | `langfuse/langfuse-worker:3` | default | HTTP `/api/health` — accurate | same | `docker compose logs langfuse-worker` |
| tempo | `grafana/tempo:2.6.1` | dev-data · dev-full | `wget /ready` — accurate | (storage layer; otel-collector → tempo) | `curl :3200/ready`; check `/var/tempo` disk |
| otel-collector | `otel/opentelemetry-collector-contrib:0.151.0` | dev-data · dev-full | **Removed (Phase F.5)** — distroless image has no shell | tempo · prometheus (export targets) | `curl :13133/` from host; Prometheus scrape on `:8888/metrics` |
| prometheus | `prom/prometheus:v3.11.3` | dev-monitor · dev-full | `wget /-/healthy` (busybox) — accurate after Phase F.5 fix; previously used `curl` which the image doesn't ship | many (scrapes everything) | UI at `http://localhost:9090`; check `/targets` for scrape failures |
| backup-agent | `georag/backup-agent:latest` | default | (none — cron container) | postgresql · neo4j · qdrant · seaweedfs | `docker compose logs backup-agent`; check backup destination |

## Healthcheck-vs-reality notes

Two known false-alarm patterns historically muddied the dashboards. Phase F.5
addressed both:

1. **Prometheus** — `prom/prometheus` is built on busybox, which provides
   `wget` but **not** `curl`. The old `curl -f http://localhost:9090/-/healthy`
   healthcheck failed at the exec step, so Docker reported the container
   "unhealthy" even when Prometheus was serving 200 OK. Fixed in
   `docker-compose.yml` by switching to `wget -q -O -`.

2. **OTel-collector** — `otel/opentelemetry-collector-contrib` is fully
   distroless (no `/bin/sh`, no `wget`, no `curl`). Any `CMD-SHELL`-based
   healthcheck is unreachable. Two valid fixes exist:
   * bind-mount a static busybox binary and call it directly, or
   * remove the healthcheck and rely on Prometheus scraping
     `otel-collector:8888/metrics` plus an alert rule on absence of
     samples.

   We chose option 2 — the alert rule is the correct production signal
   anyway; the in-container healthcheck would only have meant "container
   started," not "exporter pipeline working."

## When a service is "unhealthy" but actually fine

Quick triage order:

1. Run the documented "Failure-mode first check" above. If the service
   answers, the healthcheck is the bug.
2. Check `docker compose logs <service> --tail 100` for an actual error
   trace. Java services (Kestra, Hatchet) spam INFO during boot for
   60-90s and false-positive during that window — wait it out.
3. Confirm the image's shell utilities. Distroless and scratch-based
   images can break naive `wget` / `curl` healthchecks even when they
   originally worked, after image upstream rebases.
4. If genuinely broken, check upstream dependencies in the table above
   first (a `pgbouncer` outage cascades to Laravel + FastAPI + Hatchet
   simultaneously).

## Adding a new service

When you add a service, update this doc in the same commit. Specifically:

* Pick the right tier table.
* Verify the image actually ships the binary your healthcheck uses
  (`docker run --rm <image> which wget` is the quick test).
* Pick a `Failure-mode first check` that doesn't require shelling into
  the container — something an on-call can run from their laptop.
