# Datastore Tuning Runbook
<!-- What: Per-store tuning reference — current values, rationale, adjustment procedures, monitoring metrics -->
<!-- When: Consult before changing any database memory/connection/index setting. Required reading before Module 4+ load testing. -->
<!-- Authority: 02-data-stores-hardening.md §6 Phase B; 06-database-performance-configuration in georag-architecture.html -->
<!-- Produced by: devops-engineer agent (Claude Sonnet 4.6) -->
<!-- Date: 2026-04-20 (Module 2 Phase D) -->

---

## 1 — PostgreSQL 18.3 + PostGIS 3.6.3

### Current tuned values

| Parameter | Live value | How set | Context |
|-----------|-----------|---------|---------|
| `shared_buffers` | 8 GB | `docker-compose.yml` env `POSTGRES_SHARED_BUFFERS=8GB` | Requires restart |
| `effective_cache_size` | 24 GB | `POSTGRES_EFFECTIVE_CACHE_SIZE=24GB` | Reload only |
| `work_mem` | 128 MB | `POSTGRES_WORK_MEM=128MB` | Per-session; reload only |
| `maintenance_work_mem` | 1 GB | `POSTGRES_MAINTENANCE_WORK_MEM=1GB` | Reload only |
| `random_page_cost` | 1.1 | `POSTGRES_RANDOM_PAGE_COST=1.1` | NVMe setting; reload only |
| `max_connections` | 200 | default | Restart required |
| `io_method` | worker | default | io_uring rejected (Docker seccomp blocks it — see note below) |
| Container memory limit | 12 G | `deploy.resources.limits.memory: 12G` | Compose change + recreate |
| Container reservation | 6 G | `deploy.resources.reservations.memory: 6G` | Compose change |

### Rationale

**`shared_buffers=8GB`** — the architecture target for a 64 GB workstation is 25% (~16 GB). The
moderate path (8 GB = 12.5%) was Kyle-approved 2026-04-19 to avoid the 12 GB container limit
needed to support 16 GB shared_buffers without OOM risk while Kyle uses the workstation for other
tasks simultaneously. Upgrade path: raise to 16 GB by increasing container limit to 24 G, then
restart PG. This requires `shared_buffers` change to take effect (postmaster context).

**`effective_cache_size=24GB`** — tells the planner how much memory the OS page cache + shared
buffers combined can hold. Architecture target: 75% of 64 GB = 48 GB. Set at 24 GB (the moderate
path) to guide the planner conservatively. Does not allocate memory — planner-only hint.

**`random_page_cost=1.1`** — NVMe SSD setting. The default 4.0 is for spinning disks. At 1.1,
the planner correctly favors index scans over sequential scans for selective queries. Critical for
geological query performance. Never change to 4.0 on this hardware.

**`io_method=worker`** — io_uring was tested 2026-04-19 and crashed immediately. Root cause:
Docker's seccomp allowlist does not include `io_uring_setup/enter/register` syscalls. The WSL2
kernel (6.6.87) has io_uring compiled in but Docker's sandbox blocks it. Reverted to `worker`.
To enable io_uring in future: supply a custom Docker seccomp profile granting io_uring syscalls,
or deploy bare-metal. Tracked in module-10-doc-sweep.md.

**`autovacuum_vacuum_scale_factor`** — currently at the 0.2 default (20% dead-tuple threshold).
Architecture target is 0.05 for large ingestion tables. Apply per-table storage parameters once
Module 3 ingestion starts adding bulk rows. Example:
```sql
ALTER TABLE silver.collars SET (autovacuum_vacuum_scale_factor = 0.05,
                                autovacuum_analyze_scale_factor = 0.025);
```

### How to adjust safely

**Reload-only parameters** (work_mem, effective_cache_size, maintenance_work_mem, random_page_cost):
```bash
docker exec georag-postgresql psql -U georag -d georag -c "SELECT pg_reload_conf();"
# verify:
docker exec georag-postgresql psql -U georag -d georag -c "SHOW work_mem;"
```

**Restart-required parameters** (shared_buffers, max_connections, io_method):
1. Update the env var in `docker-compose.yml`
2. `docker compose stop georag-postgresql` — wait for PgBouncer to log reconnection failures
3. `docker compose up -d georag-postgresql` — PgBouncer reconnects automatically
4. Verify: `docker exec georag-postgresql psql -U georag -c "SHOW shared_buffers;"`

**Container limit changes:**
1. Edit `docker-compose.yml` `deploy.resources.limits.memory`
2. `docker compose up -d --force-recreate georag-postgresql`
3. PgBouncer and application services reconnect automatically (asyncpg + phpredis retry).

### What to watch

- **Cache hit ratio**: `SELECT sum(blks_hit) / (sum(blks_hit) + sum(blks_read)) FROM pg_stat_database WHERE datname='georag'` — target >99%.
- **Bloat**: `pg_stat_user_tables.n_dead_tup / n_live_tup` — alert if >20% on any large table.
- **Connection saturation**: `SELECT count(*) FROM pg_stat_activity` — alert if >180 (90% of max_connections).
- **Slow queries**: `pg_stat_statements` where `mean_exec_time > 500ms`.

---

## 2 — PgBouncer 1.25.1 (edoburu)

### Current tuned values

| Parameter | Live value | How set |
|-----------|-----------|---------|
| `pool_mode` | transaction | `pgbouncer.ini` auto-generated from `POOL_MODE` env |
| `default_pool_size` | 50 | `DEFAULT_POOL_SIZE=50` env |
| `min_pool_size` | 5 | `MIN_POOL_SIZE=5` env |
| `reserve_pool_size` | 5 | `RESERVE_POOL_SIZE=5` env |
| `max_client_conn` | 1000 | `MAX_CLIENT_CONN=1000` env |
| `server_idle_timeout` | 600 | `SERVER_IDLE_TIMEOUT=600` env |
| `server_lifetime` | 3600 | hardcoded in pgbouncer.ini template |
| `query_wait_timeout` | 120 | hardcoded in pgbouncer.ini template |

### Rationale

**`pool_mode=transaction`** — required for asyncpg (Python) and Octane/Swoole (PHP). Neither
runtime maintains persistent connection state between requests. Transaction mode means a server
connection is borrowed for one transaction then returned to the pool.

**`default_pool_size=50`** — reduced from 100 (Phase A finding PGB-01) to prevent PgBouncer from
consuming all of PG's `max_connections=200`. At 50, PgBouncer's pool leaves 150 connections for
direct tools (Dagster, psql admin). If PG `max_connections` is raised to 400 in a future tuning
pass, this can be raised to 100.

**`max_client_conn=1000`** — allows 1000 simultaneous client-side connections (from Octane workers,
FastAPI async workers, Horizon queue workers, Dagster). In transaction mode with low hold times,
this is safe because server connections are much fewer than client connections.

**`server_idle_timeout=600`** — server connections are held for 10 minutes of idle before being
closed. Reduces reconnection overhead under bursty workloads. The Phase A value was 300s.

### How to adjust

PgBouncer re-reads its config on `SIGHUP` without downtime:
```bash
docker exec georag-pgbouncer sh -c 'PGPASSWORD=georag_dev_password psql -h 127.0.0.1 -p 6432 \
  -U georag -d pgbouncer -c "RELOAD;"'
```

For pool size changes (env var changes require container recreation — the edoburu image regenerates
`pgbouncer.ini` from env on startup):
```bash
# Edit DEFAULT_POOL_SIZE in docker-compose.yml, then:
docker compose up -d --force-recreate georag-pgbouncer
```

### What to watch

```bash
docker exec georag-pgbouncer sh -c 'PGPASSWORD=georag_dev_password psql -h 127.0.0.1 -p 6432 \
  -U georag -d pgbouncer -c "SHOW STATS; SHOW POOLS;"'
```

- `cl_waiting > 0` — clients queued for a server connection; raise `default_pool_size` if sustained
- `maxwait > 0` — non-zero max wait time; investigate long-running transactions holding connections
- `avg_wait_time > 50ms` — pool exhaustion; action needed before Module 4 load

---

## 3 — Neo4j 2026.03.1 Community Edition

### Current tuned values

| Parameter | Live value | How set |
|-----------|-----------|---------|
| `server.memory.pagecache.size` | 4 GiB | `NEO4J_server_memory_pagecache_size: 4G` env |
| `server.memory.heap.initial_size` | 4 GiB | `NEO4J_server_memory_heap_initial__size: 4G` env (Phase B applied) |
| `server.memory.heap.max_size` | 4 GiB | `NEO4J_server_memory_heap_max__size: 4G` env |
| `bolt.thread_pool.max_size` | 50 | `NEO4J_bolt_thread__pool_max__size: 50` env |
| `transaction.timeout` | 60s | `NEO4J_db_transaction_timeout: 60s` env |
| `query.log.threshold` | 1000ms | env |
| Container memory limit | 9 GiB | compose deploy.resources |

### Rationale

**Heap initial = max = 4 GB** — eliminates JVM heap resizing overhead (GC pressure during expansion
from 2G to 4G on load). Phase A finding N4J-02 required them to match.

**Page cache 4 GiB** — the OS-level page cache for Neo4j's store files. The JVM object heap is
separate: the 4 GB JVM heap holds live Java objects (node/relationship objects). The page cache holds
raw store file pages. With the current graph (~56K nodes, ~51K relationships, ~333 MiB store), the
entire store fits in page cache and JVM heap simultaneously, explaining the 0/0 page cache hit/miss
readings in Phase C — all data is served from Java heap without touching the page cache.

**Warmup:** Neo4j Community Edition does not support `db.memory.pagecache.warmup.enable` as a
functional setting (it exists in the config schema but the automated warmup is Enterprise-only).
The `georag-neo4j-warmup` init container runs a manual warmup Cypher script on each cold start.
The script is at `docker/neo4j/warmup.cypher` (owned by graph-engineer agent).

**No online backup** — `neo4j-admin database backup` is Enterprise-only. Only `dump` is available,
which requires the database to be offline. See `ops/runbooks/neo4j-backup.md`.

### How to adjust

Neo4j env var naming convention: `NEO4J_<SETTING_PATH>` where dots become underscores and double
underscores represent a single dot in the setting name. The image auto-maps these to `neo4j.conf`.
Do not use the `NEO4J_` prefix for settings that already exist as bare env vars.

Memory changes require a full Neo4j restart:
```bash
docker compose stop georag-neo4j georag-neo4j-warmup
docker compose up -d georag-neo4j
# warmup runs automatically after neo4j healthcheck passes:
docker compose up -d georag-neo4j-warmup
```

### What to watch

- **Page cache hit ratio**: Will appear non-zero in PROFILE output only when the graph exceeds JVM
  heap size. Until then, 0/0 is healthy (all in heap).
- **GC pauses**: `docker logs georag-neo4j 2>&1 | grep -i "gc pause"` — long GC pauses (>200ms)
  indicate heap pressure; consider increasing heap.
- **Transaction timeouts**: `docker logs georag-neo4j 2>&1 | grep -i timeout` — indicates queries
  exceeding the 60s transaction.timeout.
- **Query log**: slow queries above 1000ms are logged automatically.

---

## 4 — Qdrant v1.17.0

### Current tuned values

| Parameter | All collections | Notes |
|-----------|-----------------|-------|
| HNSW `m` | 16 | Index build connections; matches arch spec |
| HNSW `ef_construct` | 200 | Index build search width — higher recall, more build time. Arch spec says 128; Phase A QDR-03 deferred lowering until Phase C recall baseline established |
| Query `ef` | 128 | Search-time beam width; matches arch spec |
| `on_disk_payload` | true (all except `georag_reports`: false) | QDR-05: `georag_reports` inconsistency flagged |
| Quantization | none | QDR-04: evaluate after Phase C workload baseline |
| Sparse vectors | configured (empty slot) | Slot exists; no indexed points until Module 3 |
| workspace_id payload index | present on all collections | Added Phase B |
| Container memory limit | 4 GiB | compose |

### Rationale

**`ef_construct=200`** — higher than the 128 in the arch spec. Provides better recall quality at
index build time. Should not be lowered without a before/after recall measurement. Recall baseline
deferred to Module 4 Phase C once real document embeddings exist in `georag_reports`.

**Sparse vector slot** — added Phase B via `PATCH .../update?wait=true` with
`sparse_vectors_config` (not `sparse_vectors` — see datastore gotchas). The slot is empty until
Module 3 backfills sparse IDF vectors during document ingestion. Hybrid search cannot be tested
until points have both dense and sparse components.

**Quantization deferred** — `pg_drillhole_collar` (33,490 vectors × 384 dim × 4 bytes ≈ 50 MiB
raw) and `pg_mineral_occurrence` (~34 MiB) are candidates for int8 scalar quantization. Quantizing
reduces memory by ~4x with <5% recall drop. Defer until Module 4 measures actual recall impact.

### How to adjust

Qdrant collection parameters (HNSW config, quantization) cannot be changed in-place — they require
collection recreation with migration of points. Use the pattern in `ops/runbooks/qdrant-snapshot.md`
(snapshot → restore to new collection → rename). Do not ALTER collections mid-ingestion.

Payload indices can be added live:
```bash
curl -X PUT "http://localhost:6333/collections/{name}/index" \
  -H "Content-Type: application/json" \
  -d '{"field_name": "new_field", "field_schema": "keyword"}'
```

### What to watch

- **Search latency**: target <10ms for dense search on large collections. Phase C baseline: 1–2ms.
  Alert if >50ms after Module 3 scales point counts.
- **Segment count**: `GET /collections/{name}/cluster` — many segments indicate unmerged writes;
  trigger manual optimization: `POST /collections/{name}/index`.
- **Memory**: Qdrant is at 42–43 MiB idle with 5 collections and ~56K total points. Expect
  growth proportional to vector count × 384 dim × 4 bytes + HNSW graph overhead.

---

## 5 — Redis 8.6.2

### Current tuned values

| Parameter | Value | Notes |
|-----------|-------|-------|
| `maxmemory` | 512 MB | Dev workstation allocation |
| `maxmemory_policy` | allkeys-lru | Evict least-recently-used keys when at limit |
| `appendonly` | yes | AOF enabled (Module 1 decision — protects Horizon queue jobs) |
| `appendfsync` | everysec | Balance between durability and write throughput |
| `save` | disabled | No RDB snapshots (AOF is the persistence layer) |
| `databases` | 4 | 0=cache+sessions, 1=horizon-queues, 2=pulse, 3=spare |
| Container memory limit | 1 GiB | compose |

### Rationale

**AOF on** — Laravel Horizon queue jobs are written to Redis. Losing Redis without AOF means losing
queued jobs that have not yet been processed. The everysec fsync is a one-second durability window
acceptable for job queues.

**allkeys-lru** — when Redis reaches 512 MB, it evicts the least recently used key across all
databases. This is correct for a mixed cache+queue instance in dev. In production (three separate
Redis instances), the cache instance would use allkeys-lru while the queue instance would use
noeviction to prevent job loss.

**Single instance in dev** — production requires three separated instances (cache, queue, sessions)
per arch spec §5. The production compose profile does not yet exist. Before staging deployment,
add `redis-cache`, `redis-queue`, `redis-sessions` as separate services. The queue instance needs
AOF on with `appendfsync always` for maximum durability; the cache instance can use AOF off.

**AOF file size** — Phase A found ~40 MB AOF with only 6 active keys. High churn from Pulse
aggregates and Dagster heartbeats creates many SET+DEL pairs. Consider lowering
`auto-aof-rewrite-percentage` from 100 to 50 to trigger more frequent compaction:
```bash
docker exec georag-redis redis-cli -a georag_redis_dev --no-auth-warning \
  CONFIG SET auto-aof-rewrite-percentage 50
```

### How to adjust

Runtime changes (no restart):
```bash
docker exec georag-redis redis-cli -a georag_redis_dev --no-auth-warning \
  CONFIG SET maxmemory 1073741824  # raise to 1GB
```

Persist a runtime change to docker-compose.yml Redis command args (the `redis.conf` approach or
`--maxmemory` flag in the service command).

### What to watch

```bash
docker exec georag-redis redis-cli -a georag_redis_dev --no-auth-warning info memory
docker exec georag-redis redis-cli -a georag_redis_dev --no-auth-warning info keyspace
```

- `used_memory_human` approaching `maxmemory` (512 MB) — alert at 80% (410 MB)
- `evicted_keys > 0` — if rising, the cache is at capacity and evicting real data
- `keyspace_misses` — Phase A baseline was 98.6% miss rate (expected pre-ingestion); track after
  Module 3 adds real cached data
- `aof_current_size` — growing unbounded indicates auto-rewrite is not triggering; check threshold

---

## 6 — SeaweedFS 4.20 (S3-compatible object store)

### Current tuned values

| Parameter | Value | Notes |
|-----------|-------|-------|
| Volume max | 32 | `-volume.max=32` in `docker/seaweedfs/entrypoint.sh` |
| Volume slots used | 22 / 32 | Phase A baseline; 10 free |
| Max volume size | 30 GiB | SeaweedFS default per volume |
| S3 API port | 8333 | All services connect via `http://minio:8333` |
| Container memory limit | 2 GiB | compose |

### Live buckets (as of 2026-04-20)

| Bucket | Created | Purpose |
|--------|---------|---------|
| `georag-backups` | 2026-04-19 | PG basebackup + WAL + Qdrant snapshots |
| `georag-bronze` | 2026-04-19 | Bronze layer raw file archive |
| `georag-exports` | 2026-04-19 | Report export artifacts |

**Bucket naming decision pending** — see `ops/backlog/module-10-doc-sweep.md` "SeaweedFS Bucket
Naming". Arch addendum §02b specifies `bronze` and `bronze-raster` (without the `georag-` prefix).
Live buckets use `georag-bronze` and `georag-exports`. Resolve before Module 3 ingestion writes
to the Bronze layer.

### How to adjust

Volume max is a startup flag in `docker/seaweedfs/entrypoint.sh`. If volume slot usage approaches
32, increase the flag and recreate the container (no data loss — volumes are persistent).

Bucket operations:
```bash
docker exec georag-backup-agent aws s3 mb s3://new-bucket-name \
  --endpoint-url http://minio:8333
```

### What to watch

- Volume slot usage: `curl http://localhost:9333/cluster/status` — watch `MaxVolumeId` approaching
  the `-volume.max` limit
- S3 upload success rate: check `docker logs georag-backup-agent` for aws-cli errors
- Volume server disk: SeaweedFS volumes live on the `seaweedfs_data` named volume; check with
  `docker run --rm -v seaweedfs_data:/data alpine df -h /data`

---

## Provenance

- Date: 2026-04-20
- Module: 2 Phase D
- Produced by: devops-engineer agent (Claude Sonnet 4.6)
- Authority: 02-data-stores-hardening.md §6 Phase D; georag-architecture.html §06, §07
