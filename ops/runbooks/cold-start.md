# Cold-Start Runbook

Brings the GeoRAG stack up from zero on a new host. Use this the first time you clone the repo onto any machine, or after a full Docker environment rebuild.

---

## Prerequisites

| Requirement | Minimum | Notes |
|---|---|---|
| Docker Engine | 26.0+ | `docker --version` |
| Docker Compose | v2.27+ (plugin, not standalone) | `docker compose version` |
| NVIDIA Container Toolkit | 555.x driver, CUDA 12.4+ | Only needed for the `gpu-llm` profile (vLLM) or the deprecated `dev-llm` Ollama profile. Skip if not running LLM locally. |
| Free disk (data volumes) | 40 GB | postgres_data ~3–5 GB, neo4j_data ~1 GB, qdrant_data ~2 GB, minio_data variable, vllm_hf_cache ~17 GB (one-time AWQ checkpoint download); deprecated ollama_models ~4–8 GB per model |
| Free RAM | 32 GB recommended, 16 GB minimum | Running `dev-light + dev-data` consumes ~8–10 GB. Never start `dev-full` with less than 32 GB free. |
| Network (fresh host) | Outbound HTTPS to Docker Hub, ghcr.io, dl-cdn.alpinelinux.org | Image pulls and Alpine edge repo for backup-agent build |
| Network (restore from backup) | `S3_ENDPOINT_URL` reachable from host | Only if pre-populating data from a SeaweedFS backup |

> This stack runs on the GeoRAG developer workstation: AMD Ryzen Threadripper Pro 5955WX (16 cores / 32 threads), 64 GB RAM, NVIDIA RTX A4500 20 GB VRAM (Ampere), 1.8 TB NVMe — single-purpose, dedicated to GeoRAG. It is not a laptop stack. If you have less than 64 GB RAM or less than ~20 GB VRAM, only run `dev-light` at a time and skip the LLM profile (Qwen/Qwen3-14B-AWQ needs ~10 GB of dedicated VRAM with the hatchet-worker-ai co-tenant). Pre-2026-05-08 this stack ran on an 8-core / RTX 4080 16 GB box; settings throughout the repo have been re-baselined for the new hardware (see ops/baselines/capacity-planning.md).

---

## Step 1 — Clone the repository

```bash
# Replace <tag> with the pinned release tag (e.g. v1.0.0-rc1).
# If no tag is established yet for this environment, use main and document the commit SHA.
git clone --depth 1 --branch <tag> https://github.com/your-org/georag.git
cd georag
```

**Directory permissions gotcha (WSL2 / Linux):** The Laravel bind-mount (`.:/app:cached`) will write `storage/` and `bootstrap/cache/` from inside the container as the `www-data` user. Pre-create them with world-writable permissions so the container can write on first boot:

```bash
mkdir -p storage/framework/{sessions,views,cache} storage/logs bootstrap/cache
chmod -R 777 storage bootstrap/cache
```

---

## Step 2 — Set up environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in every value flagged `REQUIRED`. The compose file uses `:?err` on these — it will refuse to start if they are blank:

| Variable | How to generate |
|---|---|
| `APP_KEY` | `docker run --rm php:8.3-cli php artisan key:generate --show` — or start the octane container once and run `php artisan key:generate` inside it |
| `POSTGRES_PASSWORD` | `openssl rand -base64 24` |
| `REVERB_APP_KEY` | `openssl rand -hex 16` |
| `REVERB_APP_SECRET` | `openssl rand -base64 32` |
| `FASTAPI_SERVICE_KEY` | `openssl rand -base64 48` — must be at least 32 bytes |
| `DAGSTER_PG_PASSWORD` | `openssl rand -base64 24` |
| `GRAFANA_ADMIN_PASSWORD` | `openssl rand -base64 24` |

**SeaweedFS / backup credentials — fresh host:**

SeaweedFS generates no credentials by default. The `MINIO_ROOT_USER` and `MINIO_ROOT_PASSWORD` in `.env` are what you choose — they are injected at first container start and become the S3 access key pair.

```bash
# Pick values and set them in .env:
MINIO_ROOT_USER=georag_admin
MINIO_ROOT_PASSWORD=$(openssl rand -base64 24)
```

Then set the backup agent variables to match:

```bash
# In .env:
AWS_ACCESS_KEY_ID=<same as MINIO_ROOT_USER>
AWS_SECRET_ACCESS_KEY=<same as MINIO_ROOT_PASSWORD>
S3_ENDPOINT_URL=http://minio:8333
```

**OLLAMA_KEEP_ALIVE:** Only relevant if running the deprecated `dev-llm` Ollama profile. The architecture spec required `5m`; the `.env.example` default is `30m` (adequate for a normal session). Set `OLLAMA_KEEP_ALIVE=5m` if sharing the GPU with other workloads; `30m` otherwise. This auto-unloads the model from VRAM after the configured idle period.

**vLLM (`gpu-llm` profile):** vLLM keeps the model resident for the lifetime of the container — there's no idle-unload knob. The named volume `vllm_hf_cache` keeps the AWQ checkpoint between container recreations so the cold weight-load only happens once per host (~60–90 s on the A4500 first run, ~10 s warm).

---

## Step 3 — Build local images

Three images must be built before the stack can start (they are not published to a registry):

```bash
docker compose build laravel-octane fastapi dagster-daemon backup-agent
```

Build time on a warm cache: ~2–3 minutes. On a cold Docker layer cache: 8–12 minutes.

> `laravel-horizon`, `laravel-reverb` share the same image as `laravel-octane` — build once.
> `dagster-webserver` shares the `dagster-daemon` image.

---

## Step 4 — Select a profile and start

See `ops/compose-profiles.md` for the full profile matrix. For daily development (the standard invocation):

```bash
docker compose --profile dev-light --profile dev-data up -d
```

This starts: PostgreSQL, PgBouncer, Redis (always-on core), plus Laravel Octane/Horizon/Reverb, Martin, FastAPI, Neo4j + warmup, Qdrant, SeaweedFS, backup-agent, and Ofelia.

**RAGFlow is intentionally excluded** — it is deferred to Module 2 and requires MySQL, Elasticsearch, and additional Redis sidecars that are not yet wired. Do not attempt to start it.

Other profile combinations:

```bash
# Core infrastructure only (database tier, no application layer):
docker compose up -d

# Add LLM inference (requires NVIDIA Container Toolkit):
docker compose --profile dev-light --profile dev-data --profile dev-llm up -d

# Add Dagster ingestion pipeline:
docker compose --profile dev-light --profile dev-data --profile dev-ingest up -d

# Add metrics dashboards:
docker compose --profile dev-monitor up -d

# Everything (end-to-end testing only — never leave unattended):
docker compose --profile dev-full up -d
```

---

## Step 5 — Expected startup order and timing

Services start in dependency order. The critical path for `dev-light + dev-data`:

```
postgresql (~12s)
  └─ pgbouncer (~7s)
       ├─ laravel-octane (~37s)    ← longest application pole
       ├─ laravel-horizon (~9s)
       └─ laravel-reverb (~7s)
  └─ martin (~8s)
redis (~7s)
neo4j (~45–90s cold)
  └─ neo4j-warmup (30–60s Cypher, runs after neo4j healthy, does NOT block other services)
qdrant
minio (SeaweedFS)
  └─ minio-init (one-shot bucket provisioning)
  └─ backup-agent (~31s)
       └─ ofelia (~1s)
fastapi (~65s)   ← final gate; waits for pgbouncer + redis + neo4j + qdrant + minio
```

**Total cold-start estimate: 90–120 seconds** to all services healthy. Add 30–60 seconds for Neo4j warmup Cypher (runs concurrently, does not block application traffic).

Timings are from Phase C baselines measured 2026-04-19 on a warm stack. A true cold start (first volume provision) will be longer for Neo4j (JVM startup + APOC plugin load on a new volume).

---

## Step 6 — First-run-only steps

These run automatically via one-shot init containers. Verify they completed:

```bash
# neo4j-warmup: should show "exited 0"
docker compose ps neo4j-warmup

# minio-init: should show "exited 0"
docker compose ps minio-init
```

If either shows a non-zero exit, check its logs:

```bash
docker compose logs neo4j-warmup
docker compose logs minio-init
```

**Laravel database migrations:** On a fresh `postgres_data` volume, run migrations once:

```bash
docker exec georag-laravel-octane php artisan migrate --force
```

If the project includes seeders for dev reference data:

```bash
docker exec georag-laravel-octane php artisan db:seed --force
```

---

## Step 7 — Post-up verification checklist

Run these commands after the stack is up. All should succeed before declaring the stack ready.

```bash
# 1. All services healthy (adjust profiles to match what you started)
docker compose --profile dev-light --profile dev-data ps

# 2. Laravel liveness
curl -sf http://localhost:8888/up && echo "Octane OK"

# 3. Laravel Pulse (admin dashboard)
curl -sf http://localhost:8888/pulse && echo "Pulse OK"
# Expected: 200 with HTML. Requires you to be logged in for full dashboard;
# the endpoint itself must return 200 to confirm Octane + DB connectivity.

# 4. FastAPI health
curl -sf http://localhost:8000/health && echo "FastAPI OK"

# 5. Ofelia — 4 jobs loaded (pg-backup, neo4j-backup, qdrant-backup, pg-wal-upload)
docker compose logs ofelia 2>&1 | grep -i "registered\|loaded\|job"
# Expected: 4 "New job registered" lines

# 6. PgBouncer pool established
docker exec georag-pgbouncer psql -d pgbouncer -c 'SHOW POOLS'
# Expected: rows for 'georag' database pool

# 7. PostgreSQL tuning confirmed live
docker exec georag-postgresql psql -U georag -c "SHOW shared_buffers; SHOW random_page_cost; SHOW archive_mode;"
# Expected: shared_buffers=4GB, random_page_cost=1.1, archive_mode=on

# 8. pg_hba.conf bind-mount active (not the PGDATA default)
docker exec georag-postgresql psql -U georag -c "SHOW hba_file;"
# Expected: /etc/postgresql/pg_hba.conf  (NOT $PGDATA/pg_hba.conf)

# 9. Martin tile endpoint (only meaningful if Module 8 tile layers are wired)
# Skip this check if Module 8 spatial tile work is not yet complete.
curl -sf http://localhost:3000/health && echo "Martin OK"

# 10. SeaweedFS S3 API reachable
curl -sf http://localhost:8333/healthz && echo "SeaweedFS OK"
```

---

## Common first-start failures

See `ops/runbooks/service-outage.md` for triage procedures. The most common first-start issues:

- **PgBouncer unhealthy** — `ADMIN_USERS` env not set or `POSTGRES_PASSWORD` mismatch. See outage runbook §1.
- **Neo4j memory validation error** — pagecache + heap exceeds the container memory limit. See outage runbook §4.
- **FastAPI stays unhealthy** — it depends on neo4j, qdrant, and minio all being healthy first. If any upstream is down, FastAPI will never become healthy.
- **`minio-init` exited non-zero** — bucket creation failed because SeaweedFS volume server was not ready. Re-run: `docker compose up minio-init` (it is idempotent).
- **`neo4j-warmup` exited non-zero** — Neo4j was not yet accepting Cypher. Re-run: `docker compose up neo4j-warmup`.

---

## Success criteria

The stack is ready when:

1. `docker compose ps` shows every container as `healthy` (except `martin` which has no healthcheck by design, `neo4j-warmup` and `minio-init` which show `exited 0`, and `ragflow` which is stopped/deferred).
2. `curl -sf http://localhost:8888/up` returns 200.
3. `curl -sf http://localhost:8000/health` returns 200.
4. Ofelia logs show 4 registered jobs.

A smoke test query (once Module 3 RAG pipeline is wired):

```bash
curl -sf -X POST http://localhost:8000/api/v1/query \
  -H "Content-Type: application/json" \
  -d '{"query": "What drill holes are in the system?"}' | jq .
```

---

_Written 2026-04-19 during Module 1 Phase D. Update this file whenever the underlying procedure changes._
