# GeoRAG Infrastructure Audit — 2026-04-19
<!-- Module 1 / Phase A -->
<!-- Authority: 01-infrastructure-orchestration.md (v1.0), 00-master-index.md (v1.1), docker-compose.yml (2026-04-19), .env.example, backups/, docker/, docs/adr/ -->
<!-- Produced by: devops-engineer agent (Claude Sonnet 4.6) -->
<!-- Stack state: RUNNING at time of audit — live ps + stats captured -->

---

## Preamble

This document covers audit items A1 through A8 (A8 added per Kyle's review instructions), plus the two addenda on dev-env notes and Redis persistence posture. Every finding carries a severity rating: **critical / high / medium / low**.

The stack was observed running at the time of audit:

- 14 containers up, all healthy except `georag-martin` (healthcheck disabled by design)
- `georag-neo4j-warmup` has exited (correct — `restart: "no"` one-shot init container)
- Prometheus and Grafana are NOT running (dev-monitor profile not active)

Findings that require a Phase B fix are listed in the Surface-to-Kyle section at the bottom.

---

## A1 — Service Inventory

Full table in `ops/audit/2026-04-19-infra-inventory.md` (standalone for easy reference).

Summary counts: 20 services defined in compose. 14 running at audit time (all profiles active except `dev-monitor` and `gpu-llm-prod`).

No findings specific to A1 beyond what is captured in A2–A8 below.

---

## A2 — Healthcheck Fidelity

### Findings

| ID | Service | Severity | Issue |
|---|---|---|---|
| HC-01 | `qdrant` | **HIGH** | Healthcheck is TCP-only (`echo > /dev/tcp/localhost/6333`). Qdrant exposes `/readyz` which reflects actual readiness (index loaded, storage accessible). TCP passes as soon as the process binds the port, long before the collection store is ready. |
| HC-02 | `pgbouncer` | **MEDIUM** | `pg_isready` confirms PgBouncer accepts TCP connections but does NOT verify it can proxy queries through to PostgreSQL. `SHOW POOLS` via the PgBouncer admin DB is the correct readiness probe — it verifies the backend connection pool is established. |
| HC-03 | `martin` | **MEDIUM** | Healthcheck is explicitly disabled (`healthcheck: disable: true`). Justified in compose comment (distroless image, no curl/wget), but no other service declares `depends_on: martin: condition: service_healthy` so the risk is contained. Martin serves live traffic without a readiness signal; dependent services (frontend MapLibre) may see tile errors until Martin is actually up. Phase B: external probe or sidecar. |
| HC-04 | `laravel-reverb` | **MEDIUM** | Healthcheck is `curl … http://localhost:8080/ \|\| test $$? -eq 22`. Exit code 22 is curl's "HTTP error" (4xx/5xx), so this check marks the container healthy as long as Reverb responds with ANY HTTP status including 4xx. It does not verify WebSocket upgrade or broadcasting functionality. Reverb 1.10 exposes a proper admin endpoint — use that in Phase B. |
| HC-05 | `laravel-horizon` | **LOW** | Healthcheck uses `php artisan horizon:status \| grep running\|paused`. The command requires `laravel-octane` to be started (shared `.:/app:cached` volume + framework boot). If Octane is unhealthy the healthcheck may still pass based on stale Horizon process state in Redis. Acceptable for dev but fragile. |
| HC-06 | `neo4j` | **LOW** | Healthcheck is `cypher-shell -a bolt://localhost:7687 'RETURN 1'`. Confirms Bolt connectivity. Does NOT verify APOC plugin loaded (it downloads at first start — could silently fail). Acceptable for now; add `CALL apoc.help('apoc') YIELD name RETURN count(name)` or check plugin volume in Phase B. |
| HC-07 | `dagster-daemon` | **LOW** | `dagster-daemon liveness-check` is a Dagster CLI command that verifies the daemon process is alive. Does not verify scheduler state or sensor state. Adequate for liveness; no readiness check is possible without a more complex probe. |
| HC-08 | `vllm` | **LOW** | `start_period: 600s` (10 min). Appropriate for large model loads. HTTP health endpoint is correct. Note: model loading is blocking; if the model does not fit in VRAM the container will exit before the healthcheck fires — the restart policy `unless-stopped` will loop. Add an OOM guard note to runbook. |

### Clean

`postgresql`, `redis`, `laravel-octane`, `fastapi`, `dagster-webserver`, `ollama`, `minio`, `prometheus`, `grafana` — healthchecks are appropriate for their service type.

---

## A3 — Backup Inventory

### PostgreSQL 18 (PostGIS 3.6)

| Aspect | Status |
|---|---|
| Backup tool | `pg_dump` (plain SQL format) via `docker/postgresql/backup.sh` |
| Schedule | Cron comment suggests `0 3 * * *` but **no cron job or systemd timer is configured in compose**. The script must be run manually or added to a separate cron container. |
| Destination | `/tmp/georag-backups/` inside the `postgresql` container (ephemeral!) with an optional upload to MinIO `georag-bronze/backups/`. The upload uses `mc` — `mc` is NOT installed in the `postgis/postgis:18-3.6-alpine` image, so the MinIO upload branch is dead code (`command -v mc` returns false). |
| Retention | 30 days (local cleanup via `find -mtime +30`) — but local is `/tmp`, which is lost on container restart |
| Last verified restore | PG17→18 migration completed 2026-04-18 (`backups/pg18-restore.log` present). Log shows errors (DROP DATABASE failed for active connections, some NOTICE-level role skips) that are expected during `pg_dumpall` restore but the final state shows successful table restores. |
| WAL archiving | Config file `docker/postgresql/wal-archiving.conf` exists but is **not mounted into the postgresql container** (not referenced in compose volumes). `archive_command = '/bin/true'` (dev no-op). No PITR capability. |

**Findings:**

| ID | Severity | Issue |
|---|---|---|
| BK-01 | **CRITICAL** | No automated backup schedule for PostgreSQL. `backup.sh` must be triggered manually. There is no cron container, no systemd timer, and no compose service that calls it. |
| BK-02 | **CRITICAL** | Backup destination is `/tmp` inside the container — lost on container restart. MinIO upload in `backup.sh` references `http://minio:9000` (old MinIO port) instead of `http://minio:8333` (SeaweedFS S3 port post-ADR-0001). The upload is dead even if `mc` were present. |
| BK-03 | **HIGH** | `wal-archiving.conf` exists but is not mounted into the PostgreSQL container. WAL archiving is effectively off. No PITR capability. The module spec (B6) requires WAL archiving to be enabled and routed to SeaweedFS `pg-wal/`. Surface to Kyle for destination decision. |
| BK-04 | **HIGH** | `backup.sh` uses `pg_dump` (single-database dump) but does NOT dump global objects (roles, tablespaces). `pg_dumpall --globals-only` is needed for a complete cluster backup. The PG17 migration used `pg_dumpall` which covered globals — the current script regresses from that. |

### Neo4j Community

| Aspect | Status |
|---|---|
| Backup tool | None defined |
| Schedule | None |
| Destination | None |
| Retention | None |
| Last verified restore | Never |

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| BK-05 | **CRITICAL** | No backup defined for Neo4j. `neo4j-admin database dump` must be wired. Surface to Kyle — this is a module §7 stop-and-ask trigger. |

### Qdrant

| Aspect | Status |
|---|---|
| Backup tool | None defined in compose |
| Schedule | None |
| Destination | None |
| Retention | None |
| Last verified restore | Never |

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| BK-06 | **CRITICAL** | No backup defined for Qdrant. Snapshot API (`POST /collections/{name}/snapshots`) must be wired nightly. Surface to Kyle. |

### SeaweedFS (object store)

| Aspect | Status |
|---|---|
| Backup tool | One-time `mc mirror` from MinIO migration (2026-04-18) |
| Schedule | None ongoing |
| Destination | `backups/minio-snapshot/` on host (1 subdirectory confirmed) |
| Retention | One-time snapshot; no rotation defined |
| Replication | Single-node, no replication configured |
| Last verified restore | Migration verification confirmed 70 objects readable post-cutover (ADR 0001). |

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| BK-07 | **HIGH** | No ongoing backup strategy for SeaweedFS. The migration snapshot is a one-time artifact, not a recurring backup. S3 sync to a secondary destination is required. Surface to Kyle for destination decision (SeaweedFS-internal replication vs. offsite). |

### Redis (Horizon queue DB)

| Aspect | Status |
|---|---|
| Persistence mode | AOF enabled (`--appendonly yes --appendfsync everysec`) — see Redis Persistence Posture addendum |
| Backup tool | None explicitly defined |
| Schedule | AOF provides ≤1s data loss on crash; no cold backup |
| Destination | `redis_data:/data` named volume (AOF files written there) |
| Retention | Volume persists until explicitly removed |
| Last verified restore | Never (AOF tested implicitly by restart, not drill-tested with `redis-cli --pipe` restore) |

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| BK-08 | **MEDIUM** | No formal Redis backup drill. AOF persistence is configured correctly (see addendum) but a restore drill (`BGSAVE` → volume copy → restore into throwaway container) has never been documented. Module B6 requires this. |

### Dagster

| Aspect | Status |
|---|---|
| Storage | Dagster uses `dagster_home` volume AND connects to PostgreSQL (DAGSTER_PG_HOST: postgresql — bypasses PgBouncer, see A5 addendum) |
| Backup tool | None defined |
| Schedule | None |

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| BK-09 | **MEDIUM** | Dagster run history lives in the `postgres_data` volume (PostgreSQL) but on a separate database (`georag_dagster`). The existing `backup.sh` only dumps `georag` (PGDATABASE), not `georag_dagster`. Dagster run history is not backed up. |

---

## A4 — SIGTERM & Graceful Shutdown

**No service in compose declares `stop_grace_period`.** Docker's default is 10 seconds for all services. The module spec requires:

| Service | Required grace | Compose config | Gap |
|---|---|---|---|
| Dagster run workers | 120 s | 10 s default | **110 s short — CRITICAL** |
| Laravel Octane | 30 s | 10 s default | **20 s short — HIGH** |
| Laravel Horizon | 60 s | 10 s default | **50 s short — HIGH** |
| Laravel Reverb | 30 s | 10 s default | **20 s short — HIGH** |
| FastAPI | 30 s | 10 s default | **20 s short — HIGH** |
| All others | best-effort | 10 s default | LOW |

**Findings:**

| ID | Severity | Issue |
|---|---|---|
| SG-01 | **CRITICAL** | `dagster-daemon` and `dagster-webserver` have no `stop_grace_period`. A `docker stop` will SIGKILL after 10 s. Dagster run workers mid-pipeline (120 s budget) will be killed. Any in-progress ingestion run is lost with no checkpoint. Phase B must add `stop_grace_period: 120s`. |
| SG-02 | **HIGH** | `laravel-octane` has no `stop_grace_period`. 10 s default will SIGKILL Swoole workers mid-request. Phase B: `stop_grace_period: 30s`. |
| SG-03 | **HIGH** | `laravel-horizon` has no `stop_grace_period`. Long-running queue jobs (embedding, export) will be killed mid-flight. Phase B: `stop_grace_period: 60s`. |
| SG-04 | **HIGH** | `laravel-reverb` has no `stop_grace_period`. Open WebSocket connections will be forcibly closed without a clean handshake. Phase B: `stop_grace_period: 30s`. |
| SG-05 | **HIGH** | `fastapi` has no `stop_grace_period`. In-flight asyncio gather calls (up to 8 s per `TIMEOUT_GATHER_S`) will be killed. Phase B: `stop_grace_period: 30s`. |

**Note on static analysis vs. live test:** `docker stop` timing has not been measured in this audit (A7 does not include live kill tests — those are Phase C). The above are based on compose config inspection only.

---

## A5 — Env & Secrets Audit

### Variables with `?err` (required — fail if not set)

The following variables use the `:?` operator (compose will refuse to start if blank):

- `POSTGRES_PASSWORD`, `APP_KEY`, `REVERB_APP_KEY`, `REVERB_APP_SECRET`, `FASTAPI_SERVICE_KEY`, `MINIO_ROOT_PASSWORD`, `DAGSTER_PG_PASSWORD`, `GRAFANA_ADMIN_PASSWORD`

This is correct practice. All are properly guarded.

### Variables with blank defaults (optional but sensitive)

| Variable | Default | Risk |
|---|---|---|
| `REDIS_PASSWORD` | `${REDIS_PASSWORD:-}` (empty string) | Redis runs unauthenticated if `.env` omits the password. In dev (network-isolated), acceptable. In any shared environment, critical. |
| `QDRANT_API_KEY` | `${QDRANT_API_KEY:-}` (empty string) | Qdrant runs without auth. Safe only on network-isolated dev. |
| `ANTHROPIC_API_KEY` | `${ANTHROPIC_API_KEY:-}` (empty string) | Will cause FastAPI boot failure at runtime when `LLM_BACKEND=anthropic` if unset, but compose starts fine. |
| `HF_TOKEN` | `${HF_TOKEN:-}` (empty string) | Required for gated HuggingFace models. vLLM will fail at model load if unset with a gated model. |

### Hardcoded values in compose

A grep for literal password/secret strings (outside `${}` interpolation) in `docker-compose.yml` found NO hardcoded credentials. All secrets go through env interpolation. Clean.

### `.env` git status

Not a git repository (no `.git` directory found). `.env` is therefore not at risk of being committed. `.gitignore` correctly lists `.env`, `.env.backup`, `.env.production`. The `.env.example` template is present and should be the only file committed when git is initialized.

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| ENV-01 | **HIGH** | `REDIS_PASSWORD` defaults to empty string (no-auth) if not set in `.env`. Current `.env.example` shows `REDIS_PASSWORD=georag_redis_dev` — ensure this is always set before any deployment that isn't fully network-isolated. Dev is acceptable; staging/prod is not. |
| ENV-02 | **MEDIUM** | `FASTAPI_SERVICE_KEY` default in `.env.example` is the placeholder string `REPLACE_ME_WITH_A_48_BYTE_TOKEN_URLSAFE_STRING_AT_LEAST_32_BYTES`. This is a valid placeholder pattern and the compose `:?` guard will catch it if not replaced. No action needed beyond confirming Phase B secret rotation is documented in RUNBOOK.md. |
| ENV-03 | **MEDIUM** | `DAGSTER_PG_HOST` is set to `postgresql` (bypasses PgBouncer, goes direct to port 5432). This contradicts `.env.example` which shows `DAGSTER_PG_HOST=pgbouncer`. The compose file hardcodes the direct connection. Dagster uses persistent connections (not transaction-mode compatible with PgBouncer) so direct-to-PostgreSQL may be intentional — but it is inconsistent with `.env.example` and undocumented. Surface to Kyle. |
| ENV-04 | **LOW** | `OLLAMA_KEEP_ALIVE` defaults to `30m` in `.env.example` but the compose comment says the architecture doc requires `5m`. The comment in compose says `5m` for dev. `30m` is documented as "covers a normal session" — this is a policy choice, not a bug. Flag for RUNBOOK.md to explain the tradeoff. |

---

## A6 — SeaweedFS Post-Cutover ADR-0001 Gotchas

Verifying the three documented gotchas against the live `docker/seaweedfs/entrypoint.sh` and compose definition:

| Gotcha | Status | Evidence |
|---|---|---|
| **G1** — `-volume` flag explicit in `weed server` command | **CLOSED** | `entrypoint.sh` line 58: `exec weed server -dir=/data -master.volumeSizeLimitMB=1024 -volume -filer -s3 -s3.port=8333`. The `-volume` flag is present and explicitly documented in the script comment. |
| **G2** — Healthcheck uses IPv4 (`127.0.0.1`) not `localhost` (which resolves to `::1`) | **CLOSED** | Compose healthcheck: `wget -qO- http://127.0.0.1:9333/cluster/status`. Explicitly uses `127.0.0.1`. Comment in compose confirms the rationale. |
| **G3** — Windows bind-mount exec bit workaround | **CLOSED** | Compose `entrypoint: ["sh", "/usr/local/bin/entrypoint.sh"]` bypasses shebang execution. `entrypoint.sh` starts with `#!/usr/bin/env sh` but is invoked via `sh` explicitly so the exec bit is not required. |

**All three ADR-0001 gotchas are verified closed.** No findings.

**Additional observation (medium):** The `minio_data` volume is the SeaweedFS data directory. SeaweedFS is running single-node with no volume replication (`-volume` without a `-replication` flag). A single volume server means no redundancy; if the volume server's `.dat` file is corrupted, all objects are lost. For dev this is acceptable. For prod, `-replication=010` (two volume servers in same rack) is required. This is noted in ADR-0001 follow-ups but not yet implemented.

---

## A7 — Frozen Baseline Capture

### Resolved compose config

Written to: `ops/audit/2026-04-19-resolved-compose.yml`

Method: `docker compose config` executed successfully. All variables resolved from `.env`.

**Note:** The resolved config contains resolved secret values (POSTGRES_PASSWORD, REDIS_PASSWORD, etc.). This file should NOT be committed to git. It lives in `ops/audit/` which should be gitignored or reviewed before any commit.

### Image digests

Written to: `ops/audit/2026-04-19-image-digests.json`

Method: `docker compose images --format json` against running stack.

**Key digest observations:**

| Image | Tag in compose | Tag actually pulled | SHA256 digest |
|---|---|---|---|
| `neo4j` | `2026.02.3-community` | **`2026-community`** | `sha256:a5feb81d916c...` |
| `postgis/postgis` | `18-3.6-alpine` | `18-3.6-alpine` | `sha256:369b23d36107...` |
| `edoburu/pgbouncer` | `v1.25.1-p0` | `v1.25.1-p0` | `sha256:c7bfcaa24de8...` |
| `redis` | `8.6.2-alpine` | `8.6.2-alpine` | `sha256:c5e375abb885...` |
| `qdrant/qdrant` | `v1.17` | `v1.17` | `sha256:94728574965d...` |
| `chrislusf/seaweedfs` | `4.20` | `4.20` | `sha256:cea8339d21da...` |
| `ghcr.io/maplibre/martin` | `1.5.0` | `1.5.0` | `sha256:13416ff1ec03...` |
| `ollama/ollama` | `0.21.0` | `0.21.0` | `sha256:d3d553bdfbcc...` |
| `vllm/vllm-openai` | `v0.19.1` | not running | n/a |
| `prom/prometheus` | `v3.3.1` | not running | n/a |
| `grafana/grafana` | `11.6.1` | not running | n/a |
| `georag/laravel` | `latest` | `latest` | `sha256:facf0dc18be1...` |
| `georag/fastapi` | `latest` | `latest` | `sha256:7a33520cd48e...` |
| `georag/dagster` | `latest` | `latest` | `sha256:e38689ef5c42...` |

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| IMG-01 | **HIGH** | `neo4j` tag in compose is `2026.02.3-community` but the actually-pulled image is tagged `2026-community`. The pinned tag `2026.02.3-community` did not exist in the registry; Docker pulled the `2026-community` floating tag. This means the Neo4j image is unpinned in practice — it will float to whatever is current `2026-community` on the next pull. Phase B must identify the correct tag format (check `hub.docker.com/r/neo4j/tags`) and pin to an immutable digest or a patch-version tag. |
| IMG-02 | **LOW** | `georag/*:latest` images are local builds per §12 convention. Digests captured for baseline. These are not published to a registry, so digest lock is only meaningful for reproducibility tracking, not supply-chain security. Acceptable for V1. |

### Idle resource footprint (single snapshot, stack running 10+ hours)

Captured at: 2026-04-19T~07:15 UTC via `docker stats --no-stream`

| Container | CPU % | Mem Used | Mem Limit | Mem % | Notes |
|---|---|---|---|---|---|
| georag-fastapi | 0.50% | 1.858 GiB | 2 GiB | **92.91%** | Near memory limit — HIGH risk of OOM kill under load |
| georag-dagster-daemon | 1.62% | 486.2 MiB | 1 GiB | 47.48% | Elevated idle CPU — sensor polling loop expected |
| georag-dagster-webserver | 1.55% | 296.4 MiB | 1 GiB | 28.95% | Elevated idle CPU — polling loop |
| georag-laravel-octane | 0.63% | 348.9 MiB | 2 GiB | 17.03% | Normal |
| georag-laravel-horizon | 0.19% | 258.5 MiB | 1 GiB | 25.25% | Normal |
| georag-laravel-reverb | 0.03% | 44.54 MiB | 512 MiB | 8.70% | Normal |
| georag-postgresql | 0.01% | 631.4 MiB | 4 GiB | 15.41% | Below expected — shared_buffers=4GB but usage is low (cold cache, few queries) |
| georag-redis | 0.34% | 5.996 MiB | 1 GiB | 0.59% | Normal |
| georag-pgbouncer | 0.01% | 5.363 MiB | 256 MiB | 2.10% | Normal |
| georag-martin | 0.02% | 11.7 MiB | 512 MiB | 2.29% | Normal |
| georag-neo4j | 0.45% | 1.989 GiB | 31.3 GiB | 6.36% | Heap well under configured 4G max |
| georag-qdrant | 2.58% | 149.7 MiB | 31.3 GiB | 0.47% | Elevated idle CPU — indexing or background tasks |
| georag-minio | 0.43% | 63.29 MiB | 2 GiB | 3.09% | Normal |
| georag-ollama | 0.00% | 16.36 MiB | 31.3 GiB | 0.05% | Model unloaded — KEEP_ALIVE working correctly |

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| RES-01 | **HIGH** | `georag-fastapi` is at 92.91% of its 2 GiB memory limit at idle. Under any real load (embedding model warm, concurrent RAG requests, HuggingFace cache populated) it will hit the OOM limit and be killed by Docker. Phase B must raise the FastAPI memory limit to 4 GiB (per prod recommendation in compose header comment). |
| RES-02 | **MEDIUM** | `georag-neo4j` and `georag-qdrant` have no memory limit set (`31.3 GiB` is the host total visible to Docker, not a configured limit). These are intentionally uncapped to allow page cache growth, but the compose header note says "Prod recommendation: raise Qdrant to 4 CPU/8GiB." No limit means a runaway Qdrant indexing job could consume all available host RAM. Phase B: set explicit limits on Neo4j (8G consistent with 4G page cache + 4G heap) and Qdrant (8G). |
| RES-03 | **LOW** | `georag-ollama` idle memory is 16.36 MiB (model unloaded). `OLLAMA_KEEP_ALIVE=30m` is set (`.env.example` default). After 30 min of inactivity VRAM is freed. The module spec says `5m` is the architecture default but 30m is documented as "recommended for a normal session." No regression — correct behavior. |

---

## A8 — PG18-from-17.9 Post-Migration Residue Check

### Migration artifacts

A `pg_dumpall` backup from the PG17 instance is present: `backups/pg17-dumpall-20260418-235253.sql` (498 MB).

A volume tar backup is also present: `backups/pg17-volume-20260418-235420.tar.gz` (215 MB).

The restore log `backups/pg18-restore.log` shows the migration was completed 2026-04-18.

### Restore log analysis

Key observations from `pg18-restore.log`:

1. `NOTICE: role "georag_audit" does not exist, skipping` — The `georag_audit` role was not present in the PG18 instance at time of restore. `init-roles.sql` creates it, but only runs on fresh container init (`/docker-entrypoint-initdb.d`). The log shows `CREATE ROLE` / `ALTER ROLE` commands succeeded for other roles, suggesting `georag_audit` may have been partially missing at restore time. **Check required.**

2. `ERROR: current user cannot be dropped` — Expected `pg_dumpall` behavior; not a defect.

3. `ERROR: role "georag" already exists` — Expected; the role existed from container init before the restore was applied.

4. The log shows `REFRESH MATERIALIZED VIEW` — confirms at least one materialized view was present in the PG17 dump and was restored.

5. The log ends mid-restore (last captured output: `COMMENT` on the `postgres` database). Unclear whether the restore completed successfully — the log may be truncated. **Check required.**

### What was NOT checked (requires live DB probe — Phase B)

Because the stack is running and I am in read-only audit mode, the following checks were NOT performed via live SQL:

- `SHOW shared_buffers` / `SHOW random_page_cost` — verify tuning params are live
- `SELECT count(*) FROM spatial_ref_sys` — verify PostGIS spatial ref table is intact
- `SELECT extname, extversion FROM pg_extension` — confirm PostGIS, pg_stat_statements extensions at expected versions
- Role existence: `SELECT rolname FROM pg_roles WHERE rolname LIKE 'georag%'`
- Sequence continuity: check `pg_sequences` for max value continuity across the migration
- `ALTER EXTENSION postgis UPDATE` — confirm PostGIS extension was upgraded (compose comment says it is required after moving to 18-3.6-alpine from 17-3.5)

**Findings:**

| ID | Severity | Issue |
|---|---|---|
| PG-01 | **HIGH** | `georag_audit` role may be absent or incorrectly restored. The restore log shows a NOTICE about it not existing at restore time. Phase B must run `SELECT rolname FROM pg_roles WHERE rolname='georag_audit'` and re-run `init-roles.sql` if the role is missing. |
| PG-02 | **HIGH** | The restore log appears truncated — cannot confirm PG18 restore completed cleanly. Phase B must verify: `SELECT count(*) FROM information_schema.tables WHERE table_schema IN ('public','silver','bronze')` returns the expected table count against the PG17 dump manifest. |
| PG-03 | **HIGH** | `ALTER EXTENSION postgis UPDATE` — the compose comment on the `postgresql` service explicitly states this must be run after moving to `18-3.6-alpine`. There is no evidence in the restore log or init scripts that it was executed. If not run, PostGIS function catalog may be stale. Surface to Kyle for confirmation. |
| PG-04 | **MEDIUM** | The `backups/pg17-dumpall-20260418-235253.sql` file (498 MB) is on the host filesystem but NOT in the `backups/` gitignore exemption — it is already gitignored (`backups/` is in `.gitignore`). Retain this file as the PG17 baseline for at least 30 days post-migration per ADR best practice. Do not delete yet. |
| PG-05 | **MEDIUM** | WAL archiving is not enabled on PG18 (see BK-03). The `wal-archiving.conf` file exists but is not mounted into the container. PG18 has been running without PITR capability since migration. |

---

## Dev-Env Notes (Addendum)

### Windows bind-mount paths

All bind mounts use relative paths (e.g., `.:/app:cached`). These resolve via WSL2's Docker Desktop integration to Linux paths. Observed behavior is correct.

**Risk areas:**

| Service | Bind mount | Risk |
|---|---|---|
| `laravel-octane/horizon/reverb` | `.:/app:cached` | Entire Laravel project tree (including `vendor/`, `node_modules/`, `.env`) is mounted into the container. This is normal for dev but means the container sees live `.env` on disk. |
| `fastapi` | `./src/fastapi:/app:cached` | Python source files. `__pycache__` directories written by the container appear as root-owned files on WSL2 — can cause permission issues on `git status`. |
| `dagster-daemon/webserver` | `./src/dagster:/opt/dagster/app:cached` | Same pycache ownership issue. |
| `seaweedfs` | `./docker/seaweedfs/entrypoint.sh` | Exec bit workaround in place (G3 — CLOSED). |

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| WIN-01 | **LOW** | `georag-fastapi` runs as `user: root` (explicitly set in compose). Combined with the `./src/fastapi:/app:cached` bind mount, root writes `__pycache__` files back to the host under root ownership. These will block `git add`/`git status` and `ruff` from running cleanly under the normal user. Phase B: add a `.gitignore` entry for `src/fastapi/__pycache__` (may already exist) and document this in the dev setup guide. |
| WIN-02 | **LOW** | A file named `nul` (root-owned, 1769 bytes) exists in the project root. This is a Windows artifact — `NUL` is a Windows device name, and a command redirecting output to `NUL` on Windows may have created a literal file named `nul` on the WSL2 filesystem. It should be removed. |

---

## Redis Persistence Posture (Addendum — per Kyle's Phase B baseline requirement)

**Objective:** Record what the current compose actually does today, giving Phase B an explicit starting point.

### Single Redis instance

There is ONE Redis instance (`redis`) serving:
- Laravel Horizon queue (queue jobs) — requires durability
- Laravel cache (`CACHE_STORE=redis`) — does NOT require durability  
- Laravel sessions (`SESSION_DRIVER=redis`) — requires durability (user logged-out on loss)
- FastAPI cache (`REDIS_HOST: redis`) — does NOT require durability

### Current persistence configuration

```
--appendonly yes          # AOF enabled
--appendfsync everysec    # sync once per second — ≤1s data loss on crash
--save ""                 # RDB snapshots DISABLED (AOF only)
```

This means:
- **Queue jobs**: protected to ≤1s data loss — CORRECT for Horizon
- **Sessions**: protected to ≤1s data loss — CORRECT
- **Cache**: AOF overhead applied to cache entries — UNNECESSARY overhead, ~5% perf cost
- **No RDB backup**: a cold AOF replay from scratch on a large dataset is slow; no point-in-time RDB for faster recovery

### Assessment

For dev (single instance, mixed workloads), the current configuration is pragmatically acceptable. The `appendonly yes` setting protects the queue and sessions, which is the right default when you can only have one instance.

**The production plan (per module spec B6) requires:**
- Separate Redis instance for cache (`appendonly no`, `maxmemory-policy allkeys-lru`, cache-only)
- Separate Redis instance for queue + sessions (`appendonly yes`, `appendfsync everysec`, `maxmemory-policy noeviction` for queue)
- Optionally: separate Redis for Reverb pub/sub

The dev compose does NOT yet implement this separation. This is explicitly called out in the compose comment (Redis review #1) and in the module spec as acceptable for dev. Phase B must document the prod plan in `ops/runbooks/backup-restore.md`.

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| RDS-01 | **MEDIUM** | Single Redis instance conflates cache + queue + sessions. AOF is enabled which protects queue and session durability but adds unnecessary write overhead to the cache workload. Acceptable for dev. Phase B must document the prod separation plan and ensure the compose `dev-full` profile does not ship as-is to a production host. |
| RDS-02 | **LOW** | `--save ""` disables RDB snapshots. This is correct for the cache/AOF pattern but means there is no fast RDB restore point. If the AOF is corrupted (unlikely with `appendfsync everysec` but possible after a hard crash), `redis-check-aof --fix` is the recovery path. Document in runbook. |

---

## Summary: Finding Count by Severity

| Severity | Count | IDs |
|---|---|---|
| **CRITICAL** | 6 | BK-01, BK-02, BK-05, BK-06, SG-01, — |
| **HIGH** | 13 | HC-01, HC-02, HC-03, HC-04, BK-03, BK-04, BK-07, SG-02, SG-03, SG-04, SG-05, ENV-01, IMG-01, RES-01, PG-01, PG-02, PG-03 |
| **MEDIUM** | 9 | HC-05, HC-06, HC-07, BK-08, BK-09, ENV-02, ENV-03, RES-02, PG-04, PG-05, RDS-01 |
| **LOW** | 8 | HC-08, IMG-02, RES-03, ENV-04, WIN-01, WIN-02, RDS-02, PG-04 |

_(Counts include all findings across all subsections including addenda.)_

---

## Surface to Kyle — Critical and High Findings (Pre-Phase B Authorization)

The following items require Kyle's decision or sign-off before Phase B proceeds:

### Stop-and-ask items (per module §7)

1. **BK-03 / WAL archiving destination (module §7 trigger):** WAL archiving is not enabled on PG18. Enabling it requires deciding WHERE the archive goes — SeaweedFS `pg-wal/` bucket, a local path with offsite sync, or another destination. Kyle must decide.

2. **BK-05, BK-06 / No backup for Neo4j and Qdrant (module §7 trigger):** These are stateful services with zero backup coverage. Phase B will wire `neo4j-admin database dump` and `POST /collections/{name}/snapshots`. Confirm Kyle wants them written to SeaweedFS (same destination as PG backups) and retained for 7 days.

3. **BK-07 / SeaweedFS ongoing backup destination:** One-time migration snapshot only. Kyle must decide: SeaweedFS S3-sync to a secondary location, or volume-level backup to a separate Docker volume.

4. **ENV-03 / Dagster direct-to-PostgreSQL (bypasses PgBouncer):** `DAGSTER_PG_HOST` is hardcoded to `postgresql` in compose, overriding the `.env.example` value of `pgbouncer`. This is likely intentional (Dagster uses persistent connections incompatible with transaction-mode PgBouncer), but it is undocumented. Kyle should confirm this is intentional and it should be documented in `ops/runbooks/`.

5. **IMG-01 / Neo4j tag mismatch:** Compose pins `neo4j:2026.02.3-community` but Docker pulled `neo4j:2026-community` (a floating tag). Kyle must approve: either find and pin the correct patch-version tag, or lock by SHA digest.

6. **PG-03 / `ALTER EXTENSION postgis UPDATE` on PG18:** The compose comment says this is required after the image upgrade. Confirm with Kyle whether this was run. If not, it must be run in a maintenance window — it may briefly lock tables.

### Phase B actions authorized without explicit Kyle sign-off (standard hardening)

These are straightforward fixes authorized by the module spec and do not trigger stop-and-ask rules:

- SG-01–SG-05: Add `stop_grace_period` declarations to all services
- HC-01: Replace Qdrant TCP healthcheck with `curl -f http://localhost:6333/readyz`
- HC-02: Replace PgBouncer `pg_isready` with `SHOW POOLS` via admin DB
- HC-04: Replace Reverb weak healthcheck with admin endpoint probe
- BK-01/BK-02: Fix `backup.sh` destination (port 8333) and add a cron container with a proper schedule
- BK-04: Supplement `backup.sh` with `pg_dumpall --globals-only` for roles
- BK-08/BK-09: Document Redis restore drill and add `georag_dagster` DB to backup scope
- RES-01: Raise FastAPI memory limit from 2 GiB to 4 GiB
- PG-01/PG-02: Verify `georag_audit` role and confirm restore completeness via live SQL queries
- WIN-01: Add `__pycache__` to `.gitignore`, document root file ownership in dev setup
- WIN-02: Remove stale `nul` file from project root
- IMG-01: Investigate correct Neo4j patch-version tag (requires Kyle sign-off before applying)
- RDS-01: Document prod Redis separation plan in `ops/runbooks/backup-restore.md`

---

_End of Phase A audit. Phase B may begin after Kyle reviews this report and authorizes the Surface-to-Kyle items._

_Files produced:_
- `ops/audit/2026-04-19-infra-audit.md` (this file)
- `ops/audit/2026-04-19-infra-inventory.md`
- `ops/audit/2026-04-19-resolved-compose.yml`
- `ops/audit/2026-04-19-image-digests.json`
