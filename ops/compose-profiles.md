# GeoRAG Docker Compose Profiles

**Authority:** the `profiles:` key on each service in `docker-compose.yml`. If
this file and the compose file disagree, the compose file is right.
**Last updated:** 2026-10-06. Rewritten against the compose file; the
2026-04-19 version described Neo4j, Ollama, Dagster, RAGFlow, vLLM,
Prometheus/Grafana, the backup agent and Ofelia, none of which has a service
block any more. This is the **dev** topology. Production is ECS Fargate
(`deploy/aws/terraform/`), not compose.

---

## Profiles

| Profile | Services | When to use |
|---|---|---|
| _(none)_ | `postgresql`, `pgbouncer`, `redis`, `martin` | Always on. No `--profile` flag needed. |
| `dev-light` | `laravel-octane`, `laravel-horizon`, `laravel-reverb` | UI and Laravel API work. |
| `dev-data` | `fastapi`, `embedding`, `reranker`, `sparse`, `qdrant`, `minio` (SeaweedFS), `minio-init`, `hatchet-lite`, `hatchet-worker` | Retrieval, ingestion and Hatchet work. A working day is `dev-light` + `dev-data`. |
| `dev-full` | Everything above | End-to-end testing only. |

`embedding`, `reranker` and `sparse` are the three model sidecars (Qwen3
embedding and SPLADE++ on CPU, Qwen3 reranker on the one GPU). Production uses
Cohere Embed 5 on Cohere's API and Cohere Rerank 3.5 on Bedrock instead, and
only `sparse` runs as a container there. LLM chat is not a compose service:
`LLM_BACKEND=cohere` (default) calls Cohere's API; `vllm` is for an external
OpenAI-compatible endpoint you run yourself.

Overlays, layered with `-f`: `docker-compose.demo.yml` (small-VM resource
overlay; also pass `docker-compose.demo.env` as a second `--env-file`, after
`.env` — the Postgres and uvicorn sizing it needs is interpolated into
`command:` at parse time and cannot be set from the overlay itself),
`docker-compose.smoke-isolation.yml` (second stack renamed
`smoke-*`), `docker/compose.redis-staging.yml` (3-instance Redis),
`docker/compose.wal-archiving.yml` (Postgres WAL archive volume),
`docker/compose.langfuse.yml` (opt-in Langfuse + ClickHouse).

```sh
# Core infrastructure only
docker compose up -d

# Daily development
docker compose --profile dev-light --profile dev-data up -d

# Everything
docker compose --profile dev-full up -d
```

---

## Restart policy

| Policy | Services | Reason |
|---|---|---|
| `restart: unless-stopped` | every long-running service | Recovers from a crash; an operator can still stop it on purpose. |
| `restart: "no"` | `minio-init` | One-shot bucket provisioning; exits 0 by design. |

---

## Startup ordering (`depends_on`)

| Service | Waits for |
|---|---|
| `pgbouncer`, `martin`, `hatchet-lite` | `postgresql` healthy |
| `laravel-octane` | `postgresql`, `redis` healthy |
| `laravel-horizon` | `pgbouncer`, `redis` healthy; `laravel-octane` started |
| `laravel-reverb` | `redis` healthy; `laravel-octane` started |
| `fastapi` | `pgbouncer`, `redis`, `qdrant`, `minio`, `embedding`, `sparse` healthy |
| `hatchet-worker` | `hatchet-lite`, `postgresql`, `qdrant`, `minio` healthy |
| `minio-init` | `minio` healthy |

`fastapi` is in `dev-data`, so the stores it depends on must be in the same
`up -d` invocation.

---

## Volume safety

- Never run `docker volume rm` on a named volume or `docker compose down -v`:
  `postgres_data`, `qdrant_data`, `redis_data`, `minio_data` and
  `hatchet_config` are all state. `hatchet_config` holds the engine's
  encryption keyset; losing it invalidates every client token.
- `georag-phase-b-extract` is the staging volume for the uranium bulk-ingest
  scripts. It is created empty by compose on a fresh machine.
