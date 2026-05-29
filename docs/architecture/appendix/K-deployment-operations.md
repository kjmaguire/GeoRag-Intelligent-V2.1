# Appendix K — Deployment + Operations

Status: **Draft.** Operator handbook. The single page to brief a new
deployer or on-call.

## 1. Fresh dev install (workstation)

Pre-reqs:
- Docker Desktop ≥ 4.30 (or Docker Engine 26+).
- 64 GB RAM, 8+ physical cores, an NVIDIA Ampere or newer GPU (RTX A4500
  / 4080 / 3090 minimum for `gpu-llm` profile).
- 200 GB free disk (vLLM weights + PG data + SeaweedFS + Qdrant).

```bash
git clone <repo> && cd georag
cp .env.example .env                         # fill in secrets (see §6)
docker compose --profile dev-light --profile dev-data up -d  # data tier + app tier
docker compose --profile dev-ingest up -d    # Dagster (optional)
docker compose --profile gpu-llm up -d       # vLLM (when chatting)
docker compose --profile dev-monitor up -d   # Grafana/Prometheus (optional)

# One-time bootstrap:
docker exec -u 0 georag-hatchet-worker-ingestion chown -R 33:33 /tmp/rapidocr_models
docker exec georag-laravel-octane php artisan migrate --database=pgsql_migrations
docker exec georag-laravel-octane php artisan db:seed --class=AcceptanceWorkspaceSeeder

# Hatchet token (per-environment):
docker exec georag-hatchet /hatchet-admin --config /config token create \
    --name georag-worker --tenant-id $(docker exec georag-postgresql psql -U hatchet -d hatchet -tA -c "SELECT id FROM \"Tenant\" WHERE slug='default'")
# paste the JWT into HATCHET_CLIENT_TOKEN in .env, then:
docker compose restart hatchet-worker-ingestion hatchet-worker-ai fastapi
```

Verify:
- `http://localhost:80` — Foundry UI (Sanctum login).
- `http://localhost:7474` — Neo4j browser.
- `http://localhost:6333/dashboard` — Qdrant.
- `http://localhost:8888` — SeaweedFS filer.
- `http://localhost:8889` — Hatchet UI.
- `http://localhost:8086` — Kestra UI.
- `http://localhost:3001` — Dagster UI (when `dev-ingest`).
- `http://localhost:3000` — Grafana (when `dev-monitor`).
- `http://localhost:8001/v1/models` — vLLM model list.

## 2. Production install (single-host)

Differences from dev:

1. **TLS at the edge.** Set `CADDY_TLS_ISSUER=acme` +
   `CADDY_ACME_EMAIL=ops@example.com`. Caddy will fetch from Let's
   Encrypt at first boot.
2. **Sanctum stateful domains** must match the real `APP_URL`.
3. **Qdrant auth** ([Appendix C §6](C-security-posture.md#6-qdrant-access-control)):
   add `QDRANT__SERVICE__API_KEY=${QDRANT_API_KEY:?required}` override.
4. **`martin_readonly`** ([Appendix C §2](C-security-posture.md#2-tenant-isolation)):
   give it a password + flip `DATABASE_URL` for the `martin` service.
5. **`georag` role**: rotate `POSTGRES_PASSWORD` from the bootstrap
   value; restrict via `pg_hba.conf` to the migration sidecar only.
6. **Pin every image by digest** — already done; verify via
   `docker images --digests`.
7. **Backup-agent**: confirm `pg_wal_archive` upload runs every 5 min
   (`docker compose logs backup-agent`).
8. **Langfuse**: bring up `docker/compose.langfuse.yml`, set
   `LANGFUSE_PUBLIC_KEY` + `LANGFUSE_SECRET_KEY` everywhere.
9. **Alertmanager receiver**: wire Slack/PagerDuty webhook via
   `docker/alertmanager/alertmanager.yml`.
10. **Reverse-proxy** (if not using Caddy as the main edge): the front
    proxy must forward `X-Forwarded-*` headers; Octane is configured
    with `--proxy-headers`.

## 3. Offline / on-prem install

The stack is air-gap-friendly:

1. Build all custom images locally:
   `docker compose build`.
2. Save:
   ```
   docker save \
     georag/laravel:latest \
     georag/fastapi:latest \
     georag/dagster:latest \
     georag/postgres:18-ext \
     georag/neo4j-exporter:latest \
     georag/backup-agent:latest \
     <every pinned third-party image with sha digest> \
     > georag-bundle.tar
   ```
3. Ship + `docker load -i georag-bundle.tar` on the target host.
4. Skip the HuggingFace weight download by including the vLLM/HF cache
   volume contents in the bundle (`vllm_hf_cache` + `fastapi_hf_cache`).
5. Disable Anthropic fallback: `LLM_BACKEND=vllm`, `LLM_FALLBACK_ENABLED=false`.

## 4. GPU requirements

| Profile | Min GPU | Why |
|---|---|---|
| `gpu-llm` (vLLM `Qwen3-14B-AWQ`) | A4500 (20 GB VRAM, Ampere SM 8.6) | INT4 weights + FP8 KV cache fits in ~17 GB |
| Embedder/reranker GPU acceleration | Same (shared) | bge-small + bge-reranker + SPLADE++ |
| Optional VL pass (`Qwen2.5-VL-7B`) | 20 GB+ | swap from text model |
| CPU-only mode | n/a | Falls back; embedder/reranker on CPU; Anthropic for LLM gen |

## 5. CPU-only degraded mode

Set:
```
LLM_BACKEND=anthropic
LLM_FALLBACK_ENABLED=false
PDF_PARSER_DOCLING_ENABLED=true      # CPU OK
DOCLING_OCR_ENABLED=false            # falls back to tesseract
PADDLEOCR_USE_GPU=false
```
Don't start the `gpu-llm` profile. Embedding throughput drops 50× — use
`hatchet-worker-ai` with a high HATCHET_WORKER_SLOTS and accept the
hit, or move embeddings off-line.

## 6. `.env` matrix (canonical envs by container)

| Var | Container(s) | Secret | Source |
|---|---|---|---|
| `APP_KEY` | laravel-* | ✅ | `php artisan key:generate` |
| `POSTGRES_PASSWORD` | postgresql + migrations | ✅ | random 32-byte |
| `GEORAG_APP_PASSWORD` | laravel + fastapi + hatchet workers + dagster | ✅ | random 32-byte |
| `REDIS_PASSWORD` | all redis clients | ✅ | random 32-byte |
| `NEO4J_PASSWORD` | neo4j + clients | ✅ | random 32-byte |
| `MINIO_ROOT_PASSWORD` / `S3_SECRET_KEY` | minio + clients | ✅ | random 32-byte |
| `QDRANT_API_KEY` | qdrant + clients (prod) | ✅ | random 32-byte |
| `FASTAPI_SERVICE_KEY` | laravel + fastapi + hatchet + dagster | ✅ | random 64-byte |
| `KESTRA_FLOW_JWT_SECRET` | kestra + fastapi + hatchet-worker-ai | ✅ | random 64-byte |
| `EXTERNAL_NOTIFICATION_HMAC_SECRET` | hatchet-worker-ai + senders | ✅ | random 64-byte |
| `AUDIT_ENCRYPTION_KEY` | fastapi + hatchet-worker-ai | ✅ | random 32-byte (rotation is hard — see [Appendix C §9](C-security-posture.md#9-secret-rotation)) |
| `HATCHET_CLIENT_TOKEN` | hatchet-lite + workers + fastapi | ✅ | from `hatchet-admin token create` |
| `HATCHET_DB_PASSWORD` | postgresql init + hatchet-lite | ✅ | random |
| `KESTRA_PG_PASSWORD` | postgresql init + kestra + laravel-octane | ✅ | random |
| `KESTRA_BASIC_AUTH_PASSWORD` | kestra + laravel-octane + caddy | ✅ | random |
| `REVERB_APP_KEY` / `REVERB_APP_SECRET` / `REVERB_APP_ID` | laravel-* + frontend bundle | ✅ | random; bundle reads from VITE_* |
| `GRAFANA_ADMIN_PASSWORD` | grafana | ✅ | random |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | every container w/ LLM calls | ✅ | from Langfuse self-host setup |
| `ANTHROPIC_API_KEY` | fastapi | ✅ | only if Anthropic enabled |
| `HF_TOKEN` | vllm | ✅ | only for gated models |
| `LOG_LEVEL` | all | ❌ | `info` in prod, `debug` in dev |
| `LLM_BACKEND` | fastapi | ❌ | `vllm` |
| `AGENTIC_RETRIEVAL_V2_ENABLED` | fastapi | ❌ | feature flag |
| `GEO_ANSWER_OIUR_ENABLED` | fastapi | ❌ | feature flag |
| timeouts (`TIMEOUT_*_S`, `TIMEOUT_REDIS_MS`) | fastapi | ❌ | tunable |

## 7. Port matrix (host-side)

| Port | Service | Profile |
|---|---|---|
| 80 | laravel-octane | dev-light |
| 3000 | grafana | dev-monitor |
| 3001 | dagster-webserver | dev-ingest |
| 3002 | martin | dev-light |
| 3100 | loki | dev-monitor |
| 3200 | tempo | dev-data |
| 4317/4318/13133 | otel-collector | dev-data |
| 5432 | postgresql | (NOT exposed — pgbouncer only) |
| 6333/6334 | qdrant | dev-data |
| 6379 | redis | always |
| 6432 | pgbouncer | always |
| 7474/7687 | neo4j | dev-data |
| 8001 | vllm | gpu-llm |
| 8085 | laravel-reverb | dev-light |
| 8086 | kestra | dev-data |
| 8087/8443 | caddy | dev-light + dev-data |
| 8889/7077 | hatchet-lite | dev-data |
| 8333/8888 | minio (SeaweedFS) | dev-data |
| 9090 | prometheus | dev-monitor |
| 9093 | alertmanager | dev-monitor |
| 9105/9121/9187 | exporters | dev-monitor |

## 8. Volume matrix

Named volumes (see `volumes:` in [docker-compose.yml](../../../docker-compose.yml)):
`postgres_data`, `neo4j_data`, `neo4j_logs`, `neo4j_plugins`,
`qdrant_data`, `redis_data`, `minio_data`, `vllm_hf_cache`,
`fastapi_hf_cache`, `grafana_data`, `alertmanager_data`, `loki_data`,
`promtail_positions`, `dagster_home`, `backup_staging`,
`pg_wal_archive`, `hatchet_config`, `kestra_data`, `kestra_workdir`,
`rapidocr_models`, `tempo_data`, `caddy_data`. External: `georag-phase-b-extract`.

## 9. Backup procedure

| Tier | Procedure | Cadence |
|---|---|---|
| PG base | Ofelia → `docker/postgresql/backup.sh` → SeaweedFS `georag-backups/postgres/base/` | daily |
| PG WAL | `archive_command` → `pg_wal_archive` volume → `backup-agent` upload | every 5 min |
| Neo4j | Ofelia → `docker/neo4j/backup.sh` (online dump) → SeaweedFS | daily |
| Qdrant | Ofelia → `docker/qdrant/backup.sh` (snapshot API) → SeaweedFS | daily |
| Redis | Hatchet `backup_redis` workflow (RDB) → SeaweedFS | daily |
| SeaweedFS | Hatchet `backup_seaweedfs` (cross-region replication) | continuous (planned) |

## 10. Restore procedure

1. Stop the application tier (`laravel-*`, `fastapi`, `hatchet-worker-*`).
2. **PG**: download the most recent base + the WAL chain since. Restore
   base via `pg_basebackup` restore, then `recovery.conf`-style WAL
   replay. RTO: ~30 min for ≤ 100 GB.
3. **Neo4j**: stop neo4j → `neo4j-admin database load` → start.
4. **Qdrant**: copy snapshot back into `qdrant_data` → restart.
5. **SeaweedFS**: from cross-region mirror.
6. Start app tier; verify via Grafana SLI panel.

## 11. Upgrade procedure

1. Read the relevant chapter / appendix and the version pin commit.
2. `docker compose pull` then `docker compose build`.
3. **Test on staging clone first** (see RUNBOOK § "Clone for upgrade").
4. Migrations: `php artisan migrate --database=pgsql_migrations` —
   always runs as `georag` owner via the dedicated connection.
5. Rolling restart in order: `pgbouncer → laravel-octane → laravel-horizon
   → laravel-reverb → fastapi → hatchet-worker-* → dagster-*`.
6. Smoke test: chat one golden query end-to-end + verify
   `audit.audit_ledger_verification_runs` latest row = intact.

## 12. Rollback procedure

1. `git revert` the offending commit on a hotfix branch.
2. Rebuild images: `docker compose build`.
3. **DB roll-forward only**: never roll back a migration in prod without
   a written plan. Down-migrations exist for dev tests; prod uses
   forward-only.
4. Rolling restart as in §11.

## 13. Scaling strategy

| Bottleneck | First lever | Then |
|---|---|---|
| Chat p95 | Increase `UVICORN_WORKERS` on fastapi | Add a second `fastapi` container behind a load balancer |
| Ingest throughput | Increase `HATCHET_WORKER_SLOTS` | Add `hatchet-worker-ingestion` replicas |
| Embedding throughput | Add `hatchet-worker-ai` replicas (each grabs a GPU slice) | Promote bge-small to a dedicated GPU pool |
| Query latency on Postgres | Tune `work_mem` / `shared_buffers` | Add a read replica (Hot Standby) |
| Tile latency | Increase Martin `pool_size` / `cache_size_mb` | Add a Martin replica behind a CDN |
| Vector latency | Increase Qdrant `ef` for the hot collection | Add a Qdrant replica |
| Audit chain throughput | None — `FOR UPDATE` serialises per-workspace | Sharding plan documented separately |

## 14. Hardware sizing

| Tier | Profile | Cores | RAM | Disk | GPU |
|---|---|---|---|---|---|
| Workstation dev | `dev-light` + `dev-data` + `gpu-llm` | 8+ | 64 GB | 200 GB NVMe | A4500/3090/4080 (20 GB) |
| Single-host small prod | + `dev-monitor` + `dev-ingest` | 16 | 128 GB | 1 TB NVMe + 4 TB HDD | L40S (48 GB) |
| Single-host medium prod | same | 32 | 256 GB | 2 TB NVMe + 16 TB HDD | A100 80 GB |
| Multi-host | split as below | per role | per role | per role | per role |

## 15. RPO / RTO by store

| Store | RPO | RTO |
|---|---|---|
| Postgres | 10 min (WAL upload cadence) | 30 min (restore + replay) |
| Neo4j | 24 h (daily dump) | 60 min (load + warmup) |
| Qdrant | 24 h (daily snapshot) | 60 min (snapshot restore + payload index rebuild); worst case rebuild from silver |
| SeaweedFS | 0 (immutable + cross-region planned) | 30 min |
| Redis | 24 h (RDB) | 5 min (load) — sessions / queue jobs lost |
| Hatchet engine DB | bundled with Postgres | bundled |
| Langfuse / ClickHouse | 24 h | 60 min |

## 16. Incident playbooks

Each major failure mode has a runbook entry — keep them short and
checklist-style:

- **PG down** → check `docker compose logs postgresql`; pg_isready;
  Alertmanager will already have paged. Restore from latest base if
  data dir is corrupt.
- **PgBouncer pool exhausted** → `SHOW POOLS` from
  `psql -p 6432 -U pgbouncer pgbouncer`; raise `default_pool_size`;
  check for connection leaks in fastapi (look for unclosed asyncpg
  acquire).
- **Hatchet engine unreachable** → `docker compose logs hatchet`; check
  the `hatchet` DB is up; reissue `HATCHET_CLIENT_TOKEN`.
- **Qdrant returns 5xx** → `/cluster`; segments locked?
  `optimizer_status`; if WAL full → raise `WAL_CAPACITY_MB`.
- **Neo4j auth fails after volume recreate** → RUNBOOK § "Neo4j auth
  migration from `NEO4J_AUTH=none`".
- **vLLM cold start hangs** → `start_period: 600s` is the maximum;
  inspect logs for HF download stuck (set `HF_TOKEN` or air-gap pre-load).
- **Tile latency spike** → `martin /metrics` cache hit ratio; raise
  `cache_size_mb`; or add a Martin replica.
- **Reverb 60-s drop loop** → check `REVERB_HOST`/`PORT` server vs
  browser ([notes/INDEX.md#project_reverb_dual_purpose_env_2026_05_21](../notes/INDEX.md#project_reverb_dual_purpose_env_2026_05_21)).
- **Audit chain fork** → `audit.audit_ledger_chain_fork_quarantine` has
  a row → operator runs `audit.recompute_hash` over the suspect
  partition; never delete forked rows without an ADR.
- **Storage full on `bronze`** → run `cold_tier_archive_workflow`
  early; verify lifecycle policy moves to `tier-cold`.
- **Workspace export hung** → `workspace_export` Hatchet workflow run
  dashboard; abort via Hatchet UI; re-run from last checkpoint.
- **Workspace data leak suspicion** → run the cross-workspace audit
  asset; consult [Appendix C §14 table](C-security-posture.md#14-threat-model-summary-table).

## 17. Observability quick-ref

| Pain | First place to look |
|---|---|
| "Chat is slow" | Grafana → "Query latency" panel + Langfuse trace |
| "Ingest stuck" | Hatchet UI → workflow run dashboard |
| "Tiles missing" | Martin `/metrics` + Postgres `silver.pg_*` function logs |
| "Login fails" | Laravel `storage/logs/laravel.log` (auth channel) |
| "Audit chain status" | Grafana → "Audit chain intact" panel |
| "Cost runaway" | Grafana → "Workspace cost burn" + `usage.usage_events` |
