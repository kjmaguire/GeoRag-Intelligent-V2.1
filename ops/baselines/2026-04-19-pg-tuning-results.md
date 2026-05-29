# PostgreSQL Tuning Results — 2026-04-19 Module 2 Phase B

<!-- Produced by: devops-engineer agent (Claude Sonnet 4.6) -->
<!-- Date: 2026-04-19 -->
<!-- Authority: Kyle-approved decisions from Module 2 Phase B surface -->

## Changes Applied

| Parameter | Before | After | Status |
|---|---|---|---|
| `shared_buffers` | 4 GB | **8 GB** | Applied (restart required — done) |
| `effective_cache_size` | 12 GB | **24 GB** | Applied (reload-only) |
| `maintenance_work_mem` | 512 MB | **1 GB** | Applied (reload-only) |
| `work_mem` | 128 MB | 128 MB | Unchanged |
| `io_method` | worker | **worker** (io_uring REVERTED) | See note below |
| Container memory limit | 4 G | **12 G** | Applied in compose |
| Container memory reservation | 1 G | **6 G** | Applied in compose |

### io_uring Revert Note

`io_method=io_uring` was set and PG was restarted. PG immediately crashed with:

```
FATAL:  could not setup io_uring queue: Operation not permitted
HINT:  Check if io_uring is disabled via /proc/sys/kernel/io_uring_disabled.
```

Root cause: Docker's default seccomp profile does not include `io_uring_setup`, `io_uring_enter`,
or `io_uring_register` in its allowlist. The WSL2 kernel (6.6.87) has io_uring compiled in, but
the Docker container sandbox blocks the syscalls before PG can use them. The kernel-level check
(`/proc/sys/kernel/io_uring_disabled`) was not the blocker — seccomp was.

To enable io_uring in future, either:
- Add `--security-opt seccomp=unconfined` to the container (broad; not recommended for prod)
- Supply a custom seccomp profile that permits `io_uring_setup`, `io_uring_enter`, `io_uring_register`

Reverted to `io_method=worker`. Tracked as Module 10 doc-sweep item (io_uring status on WSL2/Docker).

---

## EXPLAIN Before vs After

Queries run: `SELECT count(*) FROM public_geoscience.pg_drillhole_collar` and
`SELECT count(*) FROM public_geoscience.pg_mineral_occurrence`. Both use Index Only Scan
(cold read on first run after restart — shared buffers were empty).

### pg_drillhole_collar (33,490 rows)

| Metric | Before | After | Delta |
|---|---|---|---|
| Execution Time | 10.601 ms | 4.771 ms | -55% |
| I/O read time (index) | 5.166 ms | 0.653 ms | -87% |
| Planning Time | 9.949 ms | 8.203 ms | -18% |
| Buffers read | 31 | 31 | — |
| Plan | Index Only Scan | Index Only Scan | Same |

### pg_mineral_occurrence (22,229 rows)

| Metric | Before | After | Delta |
|---|---|---|---|
| Execution Time | 6.352 ms | 2.934 ms | -54% |
| I/O read time (index) | 3.320 ms | 0.494 ms | -85% |
| Planning Time | 12.174 ms | 3.120 ms | -74% |
| Buffers read | 21 | 21 | — |
| Plan | Index Only Scan | Index Only Scan | Same |

Note: the large I/O time reduction on the first post-restart query (cold buffer cache) reflects
the larger shared_buffers pool now available. The planner time reduction is noise-level on such
a small schema — the buffer stats tell the real story. Meaningful throughput comparison will be
possible in Module 4 Phase C with a realistic geological query workload.

---

## Verification

```
name                 | setting | unit | context
---------------------|---------|------|------------
effective_cache_size | 3145728 | 8kB  | user          → 24 GB
io_method            | worker  |      | postmaster    → io_uring reverted
maintenance_work_mem | 1048576 | kB   | user          → 1 GB
shared_buffers       | 1048576 | 8kB  | postmaster    → 8 GB
work_mem             | 131072  | kB   | user          → 128 MB
```

Container limit confirmed: `memory: 12G` in docker-compose.yml deploy.resources.limits.

---

## Dependent Services — Reconnection Status

| Service | Status after PG restart |
|---|---|
| georag-pgbouncer | Healthy — reconnected cleanly; SHOW POOLS normal |
| georag-fastapi | Healthy (19h uptime; no restart needed — asyncpg reconnects) |
| georag-laravel-octane | Healthy — /up endpoint 200 OK |
| georag-laravel-horizon | Healthy |
| georag-laravel-reverb | Healthy |
| georag-dagster-daemon | Brief reconnection warning during restart window; recovered |
| georag-dagster-webserver | Healthy |
| georag-backup-agent | DRY_RUN exit 0 — correct plan printed |

---

## Config Snapshot Files

- Before: `ops/baselines/2026-04-19-pg-config-before-tuning.txt`
- After: `ops/baselines/2026-04-19-pg-config-after-tuning.txt`
