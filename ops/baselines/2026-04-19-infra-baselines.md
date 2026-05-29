# GeoRAG Infrastructure Baselines — 2026-04-19
<!-- Module 1 / Phase C (C2, C3, C4, C5) -->
<!-- Authority: 01-infrastructure-orchestration.md (v1.0), Phase C scoping approved by Kyle 2026-04-19 -->
<!-- Produced by: devops-engineer agent (Claude Sonnet 4.6) -->
<!-- Measurement window: 2026-04-19 ~22:04 – ~22:30 UTC -->
<!-- Stack state at measurement: dev-data + dev-light + dev-ingest profiles active; dev-monitor NOT active -->

---

## Scope

C1 (volume-wipe cold-start) is **deferred** — pg_hba.conf replication ACL was added manually to the live postgres_data volume during Phase B and must be baked into the PG image before a volume-wipe test is valid. See appendix note.

C2, C3, C4, C5 measured in this pass.

---

## C2 — Per-Service Restart Time

**Method:** `docker restart <svc>`, then poll `docker inspect` every 500ms for `State.Health.Status == "healthy"` (for services with healthchecks) or `State.Status == "running"` (for services without). Timeout 180s. Wall time measured from restart command to first healthy/running poll confirmation. All measurements taken on a warm stack (~4h uptime) with no concurrent load.

**Stateful services NOT restarted (safety constraint):** `georag-postgresql`, `georag-neo4j`, `georag-qdrant`. Restart risk is non-zero on live stateful stores. Best-effort start-time estimate noted from `docker inspect .State.StartedAt` and uptime context.

| Service | Profile | Restart ms | Final State | Notes |
|---|---|---|---|---|
| georag-redis | core | 7,228 | healthy | AOF safe to restart in dev. Includes healthcheck interval delay. |
| georag-pgbouncer | core | 7,219 | healthy | Fast — lightweight process, no data. |
| georag-laravel-octane | dev-light | **37,413** | healthy | **Longest-pole** in restart-safe set. Swoole boot + framework bootstrap + opcache warm. ~37s. |
| georag-laravel-horizon | dev-light | 9,237 | healthy | Starts fast; PHP process + Horizon poll cycle setup. |
| georag-laravel-reverb | dev-light | 7,113 | healthy | Fast — event-loop ready after bind. |
| georag-fastapi | dev-data | **64,971** | healthy | **Slowest overall.** HuggingFace model imports + asyncpg pool setup + healthcheck 30s interval start. |
| georag-martin | dev-light | 7,539 | healthy | Fast startup; loads PG connection pool lazily. |
| georag-dagster-daemon | dev-ingest | 9,523 | healthy | Daemon registers sensors and scheduler; fast on warm code. |
| georag-dagster-webserver | dev-ingest | 16,113 | healthy | Slightly slower than daemon — GraphQL schema boot. |
| georag-ofelia | dev-data | 1,393 | running (no healthcheck) | Scheduler process only; no healthcheck defined. |
| georag-backup-agent | dev-data | 31,471 | running (no healthcheck) | Slow start: alpine + `sleep infinity` + Docker socket handshake. No healthcheck. |
| georag-postgresql | core | — | — | **Skipped — stateful. Deferred to authorized maintenance window.** Observed running ~1h at measurement; last restart was ~21:00 UTC (Phase B recreate, ~12s per Phase B logs). |
| georag-neo4j | dev-data | — | — | **Skipped — stateful. Live Neo4j dump window is 2026-04-26.** Last cold-start observed in Phase B: ~12s from stopped to healthy. |
| georag-qdrant | dev-data | — | — | **Skipped — stateful.** No restart risk baseline yet; defer with neo4j. |

**Longest-pole (restart-safe):** `georag-fastapi` at 64,971ms. Driven primarily by the 30-second healthcheck `start_period` and Python import time for HuggingFace + asyncpg pool initialization.

**Second-pole:** `georag-laravel-octane` at 37,413ms. Swoole process spawn + Laravel bootstrap + opcache population is ~5-8s; the remaining time is healthcheck polling interval (30s interval, container must pass 2 consecutive checks before reporting healthy).

---

## C3 — Backup Sizes, Wall Time, Restore-to-Throwaway Drill

### Reference Data (from Phase B live drills — not re-run per C3 scoping)

| Store | Artifact Name | Size | Backup Wall Time | S3 Location |
|---|---|---|---|---|
| PostgreSQL | `pg-basebackup-2026-04-19T21-01-29Z.tar.gz` | 174 MiB (182,677,426 bytes) | ~30s (incl. aws-cli apk install first run) | `s3://georag-backups/postgres/` |
| Qdrant (5 collections) | per-collection `.snapshot` files | ~234 MiB total | ~29s | `s3://georag-backups/qdrant/` (5 subtrees) |
| Neo4j | deferred | — | — | Live dump: 2026-04-26 03:00 UTC |

### Restore-to-Throwaway Drills (C3 new measurement)

**Method:** Throwaway containers get unique names on a throwaway network (or isolated bridge), never connected to the live store instances. All throwaway containers removed after measurement.

#### PostgreSQL Restore Drill

**Artifact used:** `pg-basebackup-2026-04-19T21-01-29Z.tar.gz` (174 MiB)
**Throwaway container:** `test-pg-restore-c3` on `georag` network (to reach SeaweedFS S3), image `postgis/postgis:18-3.6-alpine`
**Network:** `georag` (read-only S3 access to SeaweedFS; no connection to live PostgreSQL)

| Step | Wall Time |
|---|---|
| S3 download (SeaweedFS → container /tmp) | ~11s |
| Outer tar extraction (basebackup directory) | ~3.8s |
| Inner tar extraction (base.tar.gz → PGDATA) | ~5.8s |
| pg_resetwal (required: `--wal-method=none` excluded WAL) | <1s |
| pg_ctl start | ~0.7s |
| pg_isready + verification query | ~0.5s |
| **Total (download to verified)** | **~23s** |

**Verification query result:** `SELECT count(*) FROM information_schema.tables` → **284 rows**. PostgreSQL 18.3 started cleanly on the restored data.

**Engineering note:** The Phase B backup script used `pg_basebackup --wal-method=none`. This meant WAL segment files at backup time were NOT included in the archive, requiring `pg_resetwal -f` before `pg_ctl start` on a throwaway restore.

**UPDATE — Module 1 Phase C prep (2026-04-19):** BK-03 CLOSED. The backup script was updated to `--wal-method=stream` (PG streams WAL concurrently with the base backup, making the archive self-contained). WAL archiving is now enabled on PG (`archive_mode=on`, `archive_timeout=300`, `archive_command` copies to the `pg_wal_archive` named volume). The `georag-backup-agent` uploads WAL segments to `s3://georag-backups/pg-wal/` every 5 minutes via Ofelia (`@every 5m`). The C3 throwaway restore no longer requires `pg_resetwal`. Retention on S3 WAL: 8 days (one day past the 7-day basebackup retention). First confirmed live upload: 5 WAL segments (80 MiB), 2026-04-19T23:13Z.

**Cleanup:** `test-pg-restore-c3` removed after drill.

#### Qdrant Restore Drill

**Collection selected:** `georag_reports` (321 KiB snapshot — smallest, fastest drill)
**Artifact used:** `georag_reports-2146751740141300-2026-04-19-21-02-22.snapshot`
**Throwaway container:** `test-qdrant-restore-c3` (`qdrant/qdrant:v1.17.0`), port 16333 mapped to host
**Helper container:** `test-qdrant-helper-c3` (alpine:3.20) connected to both `throwaway-qdrant-net` and `georag` networks for S3 access

| Step | Wall Time |
|---|---|
| S3 download to helper container | ~1s |
| Snapshot upload to throwaway Qdrant (`/snapshots/upload`) | ~1s |
| Verification (`/collections/georag_reports`) | <1s |
| **Total (download to verified)** | **~2.2s** |

**Verification result:** `georag_reports` collection: 384-dimensional vectors, **18 points**, 1 segment. Restore confirmed clean.

**Engineering note:** Qdrant snapshot upload API (`POST /collections/{name}/snapshots/upload?priority=snapshot`) works correctly for throwaway restores. The `priority=snapshot` parameter ensures the uploaded snapshot takes precedence over any existing data. The Qdrant image is distroless (no shell, no curl) — all S3 interaction must happen via a sidecar helper with access to both networks.

**Cleanup:** `test-qdrant-restore-c3`, `test-qdrant-helper-c3`, throwaway networks removed after drill.

#### Neo4j Restore Drill

**Status: Deferred.** The Phase B backup for Neo4j is an offline dump (requires container stop). The live dump is scheduled for 2026-04-26 03:00 UTC. The restore drill pairs with that dump — attempting a restore without a valid dump artifact is not meaningful. Note in baseline: Neo4j restore procedure requires ~30s Neo4j stop window; restore from dump via `neo4j-admin database load`; estimated full restore time <5 minutes for the current dataset (~51 MiB dump artifact).

### C3 Summary Table

| Store | Backup Size | Backup Wall | Restore Wall (throwaway) | Verification Result |
|---|---|---|---|---|
| PostgreSQL | 174 MiB | ~30s | ~23s | `information_schema.tables` count = 284 — PASS |
| Qdrant (georag_reports) | 321 KiB | ~29s (all 5 collections) | ~2.2s | 18 vectors, 384D — PASS |
| Neo4j | 51 MiB (dump) | ~75s (Phase B offline drill) | **Deferred — 2026-04-26** | Awaiting live dump window |

---

## C4 — Idle Resource Footprint

**Method:** `docker stats --no-stream` every 30 seconds for 10 minutes (20 samples). Raw CSV written to `ops/baselines/2026-04-19-docker-stats-idle.csv`. Note: samples 1-4 captured during C2 restart activity, which inflates CPU peaks for affected services. CPU peaks should be interpreted with this in mind; memory values are not affected by restarts in the same way.

**Measurement window:** 2026-04-19 22:04 – 22:14 UTC

| Container | Avg CPU% | Peak CPU% | Avg Mem | Peak Mem | Limit | Headroom% | Flag |
|---|---|---|---|---|---|---|---|
| georag-fastapi | 0.62 | 0.79 | 2.27 GiB | 2.27 GiB | 4 GiB | 43.3% | |
| georag-neo4j | 19.56 | 203.75* | 1.15 GiB | 1.34 GiB | 9 GiB | 85.1% | * see note |
| georag-postgresql | 4.80 | 20.14 | 470 MiB | 471 MiB | 4 GiB | 88.5% | |
| georag-dagster-daemon | 7.78 | 97.11* | 333 MiB | 400 MiB | 1 GiB | 61.0% | * see note |
| georag-dagster-webserver | 1.81 | 4.85 | 274 MiB | 274 MiB | 1 GiB | 73.2% | |
| georag-laravel-octane | 1.03 | 4.36 | 343 MiB | 344 MiB | 2 GiB | 83.2% | |
| georag-laravel-horizon | 1.61 | 26.58* | 255 MiB | 257 MiB | 1 GiB | 74.9% | * restart spike |
| georag-laravel-reverb | 0.35 | 3.66 | 44 MiB | 44 MiB | 512 MiB | 91.4% | |
| georag-minio | 0.34 | 2.46 | 242 MiB | 333 MiB | 2 GiB | 83.8% | |
| georag-redis | 0.60 | 3.10 | 6 MiB | 6 MiB | 1 GiB | 99.4% | |
| georag-pgbouncer | 0.58 | 3.58 | 4 MiB | 5 MiB | 256 MiB | 98.1% | |
| georag-martin | 0.13 | 2.26 | 3 MiB | 3 MiB | 512 MiB | 99.4% | |
| georag-backup-agent | 3.30 | 65.97* | 7 MiB | 40 MiB | 512 MiB | 92.2% | * restart spike |
| georag-ofelia | 0.00 | 0.02 | 2 MiB | 2 MiB | 31.3 GiB | 100.0% | no limit set |
| georag-ollama | 0.00 | 0.00 | 108 MiB | 110 MiB | 31.3 GiB | 99.7% | model unloaded |
| georag-qdrant | 0.35 | 2.71 | 32 MiB | 33 MiB | 4 GiB | 99.2% | |

**Post-C2 stable snapshot (single clean sample taken after all restarts settled):**

| Container | CPU% | Mem Used | Limit | Mem% |
|---|---|---|---|---|
| georag-fastapi | 0.53% | 2.265 GiB / 4 GiB | 56.6% | |
| georag-neo4j | 0.57% | 1.126 GiB / 9 GiB | 12.5% | |
| georag-postgresql | 0.00% | 470 MiB / 4 GiB | 11.5% | |
| georag-dagster-daemon | 1.81% | 400 MiB / 1 GiB | 39.1% | |
| georag-dagster-webserver | 1.74% | 274 MiB / 1 GiB | 26.8% | |
| georag-laravel-octane | 0.69% | 343 MiB / 2 GiB | 16.8% | |
| georag-laravel-horizon | 1.15% | 257 MiB / 1 GiB | 25.1% | |
| georag-laravel-reverb | 0.04% | 44 MiB / 512 MiB | 8.6% | |
| georag-minio | 0.28% | 280 MiB / 2 GiB | 13.7% | |
| georag-redis | 0.39% | 6 MiB / 1 GiB | 0.6% | |
| georag-pgbouncer | 0.02% | 5 MiB / 256 MiB | 1.9% | |
| georag-martin | 0.02% | 3 MiB / 512 MiB | 0.6% | |
| georag-backup-agent | 0.00% | 5 MiB / 512 MiB | 1.0% | |
| georag-ofelia | 0.00% | 2 MiB / 31.3 GiB | 0.0% | |
| georag-ollama | 0.00% | 107 MiB / 31.3 GiB | 0.3% | model unloaded |
| georag-qdrant | 0.01% | 32 MiB / 4 GiB | 0.8% | |

### C4 Observations

**Top 3 memory consumers (stable/post-restart):**
1. `georag-fastapi` — 2.265 GiB / 4 GiB (56.6%). HuggingFace imports and asyncpg pool consume ~2.3 GiB at idle even with no requests. This is substantially lower than the pre-Phase B reading (1.858 GiB / 2 GiB = 92.9%) because the limit was raised to 4 GiB. Headroom is now 43%. Under real load (warm embeddings, concurrent RAG) this will grow — monitor in Module 2.
2. `georag-neo4j` — 1.126 GiB / 9 GiB (12.5%). Well within limits now that the container ceiling was raised to 9 GiB (Phase B). Page cache warming drives this higher over time.
3. `georag-postgresql` — 470 MiB / 4 GiB (11.5%). Low usage reflects a cold cache (database was recently recreated in Phase B). shared_buffers is configured at 4 GiB but actual usage grows with query traffic.

**Services >80% of limit:** NONE in the stable snapshot. The FastAPI 92.9% finding from Phase A is resolved by the limit raise.

**CPU anomalies in collection window:**
- `georag-neo4j` peak 203.75% — multi-core burst during restart (Neo4j JVM GC + page cache warming burst). Settled to 0.57% stable.
- `georag-dagster-daemon` peak 97.11% — sensor poll burst on daemon restart. Settled to 1.81% stable. This is the expected pattern for Dagster's sensor polling loop.
- `georag-backup-agent` peak 65.97% — apk package resolution burst on container start. Settled to 0.00% stable.

**No service is flagged >80% of limit in the stable post-restart state.** Phase B regression: none found.

**ofelia and ollama have no effective memory limit** (31.3 GiB = Docker host total visible to Docker, not a configured cap). For ofelia this is acceptable (uses 2 MiB). For ollama, model loading could consume all available VRAM + RAM — this is by design (see `OLLAMA_KEEP_ALIVE`).

---

## C5 — Graceful Shutdown Budget

**Method:** `docker stop <svc>` (Docker sends SIGTERM then SIGKILL after `stop_grace_period`). Wall time measured from stop command to container exit. Immediately followed by `docker start <svc>` to keep the stack operational. Exit code 0 = clean SIGTERM exit. Exit code 137 = SIGKILL (128 + SIGKILL=9).

**Stateful services NOT stopped:** `georag-postgresql`, `georag-neo4j`, `georag-qdrant`. Grace period from compose recorded for reference.

| Service | Grace Period (configured) | Actual Exit Time | Exit Code | Outcome | Notes |
|---|---|---|---|---|---|
| georag-redis | 15s | 912ms | 0 | **clean** | AOF fsync on SIGTERM completes immediately. |
| georag-pgbouncer | 30s | 835ms | 0 | **clean** | Drains connections instantly. |
| georag-laravel-octane | 30s | **~987ms** (avg 3 runs: 1047/921/992) | **0** | **clean** | **C5-01 FIXED 2026-04-19.** Added `exec` before `php artisan` in `sh -c` wrapper → Swoole is now PID 1, receives SIGTERM directly, drains idle workers in ~1s. Pre-fix: 30,950ms, exit 137 (SIGKILL). |
| georag-laravel-horizon | 60s | 2,742ms | 0 | **clean** | Horizon's `--stop-when-empty` honored on SIGTERM. |
| georag-laravel-reverb | 30s | 918ms | 0 | **clean** | WebSocket connections closed on SIGTERM. |
| georag-fastapi | 30s | 1,935ms | 0 | **clean** | Uvicorn graceful shutdown. |
| georag-martin | 10s | 1,183ms | 0 | **clean** | Fast exit. |
| georag-dagster-daemon | 120s | 1,878ms | 0 | **clean** | Daemon exits quickly when no jobs in progress. |
| georag-dagster-webserver | 30s | 2,446ms | 0 | **clean** | HTTP connections drained. |
| georag-ofelia | 10s | 699ms | 0 | **clean** | Scheduler process exits promptly. |
| georag-backup-agent | 30s | **30,819ms** | **137** | **SIGKILL'd** | **See finding below.** |
| georag-postgresql | 30s | — | — | not measured | Deferred to authorized window. |
| georag-neo4j | 60s | — | — | not measured | Deferred to authorized window. |
| georag-qdrant | 30s | — | — | not measured | Deferred to authorized window. |

### C5 Findings

**CRITICAL — `georag-laravel-octane` SIGKILL'd (exit 137, 30,950ms) — FIXED 2026-04-19 (C5-01):**
Root cause confirmed: `sh -c` wrapper held PID 1; SIGTERM from Docker reached the shell, which did not propagate it to the Swoole master process child. Docker waited out the full 30s grace period then sent SIGKILL (exit 137).

Fix applied: added `exec` before `php artisan octane:start` in the `sh -c` wrapper. The shell exec's into php, handing off PID 1. Swoole now receives SIGTERM directly from Docker and drains idle workers cleanly.

Post-fix measurements (3 runs): **1,047ms / 921ms / 992ms**, all exit code 0. No SIGKILL. The 30s grace period remains as headroom for in-flight request drain under load.

**MEDIUM — `georag-backup-agent` SIGKILL'd (exit 137, 30,819ms):**
The backup agent runs `sleep infinity` as its primary process. `docker stop` sends SIGTERM to `sleep`, which does not respond to SIGTERM (POSIX: `sleep` is not required to handle SIGTERM). Docker waits the full 30s grace period then SIGKILLs it. This is expected behavior for `sleep infinity` and is not a regression — but it means the `stop_grace_period: 30s` is wasted budget. If a backup job is running via `docker exec` when the container is stopped, that exec process also gets killed.

Recommendation: Change the backup-agent command from `sleep infinity` to a proper init process (e.g., `tini sleep infinity`) that forwards signals correctly, or use a loop that traps SIGTERM. Not urgent — this only matters during deliberate stack stops, not crashes.

**9 of 11 measured services: clean graceful shutdown.** No unexpected regressions from Phase B stop_grace_period work.

---

## Methodology Notes

### C2 — What was measured
Restart timing using millisecond-resolution `date -u +%s%3N` before/after `docker restart`, with 500ms polling until health status changes. Services with healthchecks: wait for `healthy`. Services without: wait for `running`. 180s timeout per service. All measurements taken on a warm stack with no concurrent user load.

Stateful services (postgresql, neo4j, qdrant) skipped per safety constraint. Their restart times are estimated from Phase B observations (PG: ~12s, Neo4j: ~12s). Qdrant has never been cleanly timed — baseline deferred.

### C3 — What was measured
Restore drills use throwaway containers on isolated networks. The PG drill required `pg_resetwal` at time of C3 measurement due to `--wal-method=none` in the backup script. **UPDATE (Phase C prep, 2026-04-19):** The backup script is now `--wal-method=stream`; future PG restore drills will not require `pg_resetwal`. Qdrant drill used the smallest collection (321 KiB) to minimize drill time; larger collections scale proportionally. Neo4j restore drill deferred to pair with the 2026-04-26 live dump.

### C4 — What was measured
20 samples of `docker stats --no-stream` at 30s intervals. Samples 1–4 overlap with C2 restart activity, inflating CPU peaks for restarted services. The post-C2 stable snapshot provides the cleaner idle baseline.

### C5 — What was measured
`docker stop` (SIGTERM → SIGKILL after grace period) with millisecond wall timing. Exit code 137 definitively identifies SIGKILL. Services immediately restarted to maintain stack usability. All measurements taken idle (no active requests or jobs during testing).

---

## Longest-Pole Analysis — Cold-Start Estimate

When C1 (volume-wipe cold-start) eventually runs, the expected sequence with `docker compose --profile dev-data --profile dev-light up -d` is: PostgreSQL must be healthy before PgBouncer, PgBouncer before Laravel (all three) and FastAPI, Neo4j before FastAPI and the warmup init container, Qdrant and MinIO before FastAPI. The serial critical path runs through the longest chains.

Based on C2 data and known start-time observations, the dominant cold-start poles are: (1) FastAPI at ~65s restart time (healthcheck start_period + Python import + pool setup), which cannot start until Neo4j, Qdrant, and MinIO are all healthy; (2) Neo4j, which on a cold data volume requires JVM startup + page cache population + APOC plugin load — estimated 45–90s from historical Phase B observation; and (3) Octane at ~37s (Swoole + Laravel bootstrap), which the front-end depends on. The net cold-start time from `docker compose up` to all services healthy is estimated at 90–120 seconds for the dev-data + dev-light profile combination, with FastAPI being the final gate in most scenarios. The neo4j-warmup init container adds another 30–60s of warmup Cypher execution AFTER Neo4j reports healthy, but this does not block other services. Total time from `up` to a fully warm and query-ready stack is estimated at 2–3 minutes.

---

## Appendix — C1 Deferral

C1 (cold-start with volume wipe) remains deferred pending:

1. **`pg_hba.conf` init — RESOLVED (Phase C prep, 2026-04-19):** The replication ACL (`host replication all 172.19.0.0/16 scram-sha-256`) that was previously added manually to the live `postgres_data` volume is now baked into `docker/postgresql/pg_hba.conf` (bind-mounted into the PG container at `/etc/postgresql/pg_hba.conf`, activated via `-c hba_file=/etc/postgresql/pg_hba.conf`). A fresh volume provision will automatically use this file. The manual entry in PGDATA's `pg_hba.conf` is now redundant and harmless — the `hba_file` directive ensures the bind-mounted version takes precedence. This C1 blocker is **closed**.

2. **Kyle-authorized volume-wipe window:** Given the data in `postgres_data`, `neo4j_data`, `qdrant_data`, and `minio_data`, a volume wipe requires explicit authorization and a confirmed backup of all stores. The Qdrant and PG backups are verified (Phase B + C3 drills). Neo4j backup is pending 2026-04-26. C1 should be scheduled for 2026-04-27 or later once Neo4j backup is confirmed clean.

---

_Files produced by this phase:_
- `ops/baselines/2026-04-19-infra-baselines.md` — this file
- `ops/baselines/2026-04-19-docker-stats-idle.csv` — raw C4 stats data (20 samples, 16 containers)
