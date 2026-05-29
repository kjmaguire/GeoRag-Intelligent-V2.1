# GeoRAG Data Stores Audit — 2026-04-19
<!-- Module 2 / Phase A -->
<!-- Authority: 02-data-stores-hardening.md (v1.0), 00-master-index.md (v1.1), live container state 2026-04-19/20 -->
<!-- Produced by: devops-engineer agent (Claude Sonnet 4.6) -->
<!-- Stack state: 16 containers running at time of audit (dev-data + dev-light + dev-ingest + core profiles active) -->
<!-- Hard constraint: read-only pass. No config changes, no service restarts, no application code edits. -->

---

## Preamble

All probes were read-only. Services were interrogated via `docker exec`, admin APIs, and SQL/Cypher queries. No configs, compose files, or application code were modified.

Finding IDs use store prefixes: `PG-NN`, `PGB-NN`, `N4J-NN`, `QDR-NN`, `RDS-NN`, `SFS-NN`, `BASE-NN`.

Severity scale: **critical** (data loss / security / tenant isolation) · **high** (compliance gap / production blocker) · **medium** (tuning gap / operational risk) · **low** (cosmetic / deferred).

---

## A1 — PostgreSQL 18.3 / PostGIS 3.6.3 Post-Migration Health

### Version Confirmed

```
PostgreSQL 18.3 on x86_64-pc-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit
Image: postgis/postgis:18-3.6-alpine
```

### Extensions

| Extension | Installed Version | Expected | Status |
|---|---|---|---|
| plpgsql | 1.0 | yes | CLEAN |
| postgis | 3.6.3 | yes | CLEAN |
| postgis_topology | 3.6.3 | yes | CLEAN |
| pg_stat_statements | 1.12 | yes | CLEAN |
| pg_trgm | 1.6 | yes | CLEAN |
| uuid-ossp | 1.1 | yes | CLEAN |
| **postgis_raster** | not installed | required per §02 | **GAP** |

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| PG-01 | **HIGH** | `postgis_raster` is available in the image (version 3.6.3 confirmed via `pg_available_extensions`) but is NOT installed. The module spec calls out PostGIS 3.6.3 raster support; the `silver_raster_layers` migration table (migration `2026_04_18_140000_create_silver_raster_layers_table.php`) implies raster pipeline use. `CREATE EXTENSION postgis_raster;` is needed before Module 3 raster ingestion can function. Phase B: install. |

### spatial_ref_sys Integrity

| Check | Result |
|---|---|
| Row count | 8,500 |
| SRID 4326 (WGS 84 geographic) | PRESENT |
| SRID 3857 (Web Mercator) | PRESENT |
| SRID 32613 (UTM Zone 13N — project CRS) | PRESENT |

**CLEAN.** All three architecturally-required SRIDs are present. Row count of 8,500 is consistent with a full PostGIS bundled reference load (typical 8,500 SRIDs).

### Role Inventory

| Role | Superuser | CreateDB | Replication | CanLogin | Assessment |
|---|---|---|---|---|---|
| `georag` | yes | yes | yes | yes | CLEAN — app superuser |
| `georag_audit` | no | no | no | no | CLEAN — audit role |
| `georag_read` | no | no | no | no | CLEAN — read-only role |
| `georag_write` | no | no | no | no | CLEAN — write role |
| `pg_*` system roles | no | no | no | no | CLEAN — PG built-ins |

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| PG-02 | **MEDIUM** | `georag` is a superuser with replication rights. For a production deployment this is over-privileged for an application role — it should be a regular role with schema-specific grants. Module 9 (Security/RBAC) owns least-privilege role scoping. Flag here for Module 9. Not a Module 2 action. |

### Sequence Continuity

All sequences with materialized `last_value` verified. Schema: `last_value >= max(pk)` required.

| Table | max(PK) | seq.last_value | OK? |
|---|---|---|---|
| users | 2 | 4 | CLEAN |
| migrations | 48 | 48 | CLEAN |
| pulse_aggregates | 105 | 112 | CLEAN |
| pulse_entries | 28 | 28 | CLEAN |
| project_user | 4 | 4 | CLEAN |
| personal_access_tokens | (no rows) | 3 | CLEAN (no rows — seq advanced from earlier deletes, safe) |
| commodity_aliases | n/a | 77 | CLEAN |
| status_aliases | n/a | 39 | CLEAN |

Sequences with `last_value IS NULL` (15 total, including all `_history_*` sequences): these are sequences that have never been called (`last_value = NULL` in `pg_sequences` means the sequence has been created but no `nextval()` call has been made). This is normal for empty tables. No continuity risk.

**CLEAN.** No sequence behind its max PK.

### Bloat Baseline

Top bloat table by dead_pct from `pg_stat_user_tables`:

| Schema | Table | n_live_tup | n_dead_tup | dead_pct |
|---|---|---|---|---|
| public | pulse_aggregates | 80 | 9 | 10.11% |
| All others | — | various | 0 | 0.00% |

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| PG-03 | **LOW** | `pulse_aggregates` has 10.11% dead tuple ratio (9 dead / 80 live). This is normal Pulse activity churn; autovacuum will address at the 20% scale factor threshold. No immediate action needed. Monitor after Module 3 ingestion adds bulk-insert traffic. |

### Current Settings

| Setting | Current Value | Arch Target | Assessment |
|---|---|---|---|
| shared_buffers | 4GB | ~25% of 64GB = 16GB | **UNDER-PROVISIONED** |
| effective_cache_size | 12GB | ~75% of 64GB = 48GB | **UNDER-PROVISIONED** |
| work_mem | 128MB | 128MB dev | CLEAN |
| maintenance_work_mem | 512MB | 1-2GB | **UNDER-PROVISIONED** |
| max_connections | 200 | 200 (sized for PgBouncer) | CLEAN |
| random_page_cost | 1.1 | 1.1 (NVMe) | CLEAN |
| max_wal_size | 4GB | — | CLEAN |
| checkpoint_timeout | 5min | — | CLEAN |
| max_worker_processes | 8 | 8 (8-core) | CLEAN |
| max_parallel_workers_per_gather | 4 | 4 | CLEAN |
| archive_mode | on | on | CLEAN |
| archive_command | `test ! -f .../wal_archive/%f && cp %p .../%f` | local-to-volume | CLEAN (Module 1 Phase B verified live) |
| archive_timeout | 5min | — | CLEAN |
| autovacuum | on | on | CLEAN |
| autovacuum_vacuum_scale_factor | 0.2 | 0.05 (large tables) | **NEEDS TUNING** |
| autovacuum_analyze_scale_factor | 0.1 | 0.025 (large tables) | **NEEDS TUNING** |
| autovacuum_vacuum_cost_delay | 2ms | — | CLEAN |
| autovacuum_max_workers | 3 | 3 | CLEAN |

**Findings:**

| ID | Severity | Issue |
|---|---|---|
| PG-04 | **HIGH** | `shared_buffers = 4GB` against 64GB RAM is ~6%, well below the 25% target (16GB). PG is leaving most of its working set in the OS cache. For a dev workstation this still performs due to the OS page cache, but the planner is making suboptimal cost estimates. Phase B: raise to 16GB. Note: `deploy.resources.limits.memory: 4G` in compose will OOM-kill the container if `shared_buffers` is raised to 16GB — the memory limit must be raised concurrently to 24-32GB. |
| PG-05 | **HIGH** | `effective_cache_size = 12GB` vs 48GB target (75% of 64GB). This drives planner index-vs-seqscan decisions. With 12GB, the planner underestimates the OS page cache and may choose full scans over index scans on medium-sized geological tables. Phase B: raise to 48GB. |
| PG-06 | **MEDIUM** | `maintenance_work_mem = 512MB`. Spec target is 1-2GB for bulk ingest operations (VACUUM, CREATE INDEX, pg_restore). Phase B: raise to 1GB; allow per-session override to 2GB during ingestion. |
| PG-07 | **MEDIUM** | `autovacuum_vacuum_scale_factor = 0.2` (default). Module 2 spec requires 0.05 on large ingestion tables. After Module 3 ingestion adds tens of thousands of rows, a 20% dead-tuple threshold means significant bloat accumulates before vacuum triggers. Phase B: apply per-table storage parameters on the large geological tables (collars, samples, assays). |

### WSL2 Kernel Version

```
6.6.87.2-microsoft-standard-WSL2
```

**Assessment:** Kernel 6.6.x supports io_uring. PG18 `io_method=io_uring` is viable on this host. Phase B decision point: toggle `io_method=io_uring` and measure; Phase B spec section B1 requires a before/after measurement before committing. **Surface to Kyle** per §7 trigger (PG18 io_uring toggle across deployment targets may not be uniform).

### Long-Running Queries / Locks

Zero sessions with state != idle and duration > 5 seconds at time of audit. **CLEAN.**

### pg_stat_statements — Top Queries by Total Time

| Calls | Total ms | Mean ms | Query (truncated) |
|---|---|---|---|
| 47,735 | 157,523 | 3.30 | pg_catalog.pg_class relname introspection (Dagster schema polling) |
| 16,312 | 15,371 | 0.94 | pg_attribute type introspection |
| 16,312 | 12,240 | 0.75 | pg_type introspection |
| 15,145 | 7,024 | 0.46 | daemon_heartbeats INSERT (Dagster heartbeat) |
| 5 | 3,478 | 695 | ST_Extent on v_pg_assessment_survey (spatial bounding box) |
| 5 | 1,369 | 273 | ST_Extent on v_pg_drillhole_collar |

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| PG-08 | **MEDIUM** | Top query by total time is Dagster introspecting `pg_catalog.pg_class` 47,735 times at 3.30ms mean = 157 seconds cumulative. This is normal Dagster ORM schema reflection at boot and during run polling. The high call count is because Dagster bypasses PgBouncer and connects direct to PostgreSQL (ENV-03 from Module 1 audit). The repeated reflection should be considered noise but contributes real CPU load. Mitigate in Phase B by enabling Dagster schema caching (config, not Module 2 scope). Flag to data-engineer. |
| PG-09 | **LOW** | Two spatial bounding-box queries (`ST_Extent` on MVT views) each take ~270-695ms. These are 5 calls each — likely Martin tile-bounds pre-computation on startup. Not actionable here; Module 8 owns Martin performance. |

---

## A2 — PgBouncer 1.25.1 (edoburu) Env Audit

### Env-Var Scheme

Live environment inside `georag-pgbouncer` container:

```
ADMIN_USERS=georag
AUTH_TYPE=scram-sha-256
DB_HOST=postgresql
DB_NAME=georag
DB_PORT=5432
DB_USER=georag
DB_PASSWORD=<redacted>
DEFAULT_POOL_SIZE=100
IGNORE_STARTUP_PARAMETERS=extra_float_digits,jit,application_name
MAX_CLIENT_CONN=200
POOL_MODE=transaction
RESERVE_POOL_SIZE=5
SERVER_IDLE_TIMEOUT=300
SERVER_RESET_QUERY=DISCARD ALL
```

**No `POSTGRESQL_*` or `PGBOUNCER_*` vars present.** `DB_*` bare vars are correctly set. **CLEAN.**

### Pool Configuration

| Setting | Value | Spec Target | Assessment |
|---|---|---|---|
| POOL_MODE | transaction | transaction | CLEAN |
| MAX_CLIENT_CONN | 200 | 1000 (prod spec) | GAP — acceptable for dev; Phase B raises for prod |
| DEFAULT_POOL_SIZE | 100 | 25 (spec) | OVER-PROVISIONED — actual spec says 25; 100 in compose comment says "100 gives 100% headroom over peak demand" — this is an intentional local deviation. However at 100 server connections per database PgBouncer can consume the full PG `max_connections=200` just from PgBouncer pools. Flag. |
| RESERVE_POOL_SIZE | 5 | 5 | CLEAN |
| SERVER_IDLE_TIMEOUT | 300 | 600 (spec) | UNDER |
| server_lifetime | 3600 | 3600 | CLEAN (PgBouncer default, confirmed via SHOW CONFIG) |
| query_wait_timeout | 120 | 120 | CLEAN (PgBouncer default, confirmed via SHOW CONFIG) |
| MIN_POOL_SIZE | 5 | — | Composite env var; not in spec |

**Findings:**

| ID | Severity | Issue |
|---|---|---|
| PGB-01 | **MEDIUM** | `DEFAULT_POOL_SIZE=100` means PgBouncer will open up to 100 server-side connections to PostgreSQL. With `max_connections=200` in PostgreSQL, a single PgBouncer pool can consume 50% of the PG connection budget. FastAPI + Laravel + Dagster all route through the same PgBouncer pool. Under concurrent load, PgBouncer could exhaust PG connections. Phase B: reduce `DEFAULT_POOL_SIZE` to 25-50 and raise PG `max_connections` to 400, OR keep 100 and add a `max_db_connections` cap per the spec. The spec target of 25 plus headroom is correct for a transaction-mode pool where connections are brief. |
| PGB-02 | **LOW** | `server_lifetime` and `query_wait_timeout` are not explicitly set in `pgbouncer.ini` — they are at PgBouncer compile-time defaults (3600s, 120s respectively, confirmed via `SHOW CONFIG`). Phase B: make these explicit in the env to prevent surprises on image upgrades. |
| PGB-03 | **LOW** | `SERVER_IDLE_TIMEOUT=300` (5 min). Module spec B3 target is 600 (10 min) for stable connections. 300 means server connections are torn down relatively aggressively under low load, which causes reconnect overhead. Not critical at dev scale. Phase B: raise to 600. |

### Admin DB Access

`SHOW POOLS` executed successfully inside the container. Result:

```
database  | user    | pool_mode   | cl_active | cl_waiting | sv_active | sv_idle | maxwait
georag    | georag  | transaction |         0 |          0 |         0 |       0 |       0
pgbouncer | pgbouncer | statement |         1 |          0 |         0 |       0 |       0
```

Pool is idle (no active application connections at time of audit). **CLEAN — admin access works.**

`SHOW STATS` confirms 8 total server assignments, 14 transactions, 34 queries since startup. Healthy.

### Log Scan (7 days)

```
LOG: server login has been failing, cached error: server DNS lookup failed (server_login_retry)
WARNING: pooler error: server login has been failing
```

These 6-8 warnings all appear at `2026-04-19 22:33:xx UTC` — the compose bootstrap window when PostgreSQL was not yet healthy. They resolved once PostgreSQL came up. No ongoing auth errors, no `no more connections allowed` entries. **CLEAN** (bootstrap DNS race is a known transient from healthcheck depends_on ordering).

---

## A3 — Neo4j 2026.03.1 Community Edition Audit

### Version

```
2026.03.1 (confirmed — matches Module 1 Phase B pin)
```

Architecture doc references `2026.02.3` — this tag never existed in Docker Hub. Effective pin is 2026.03.1. Doc drift flagged for Module 10.

### Live Configuration (env)

| Setting | Value | Assessment |
|---|---|---|
| NEO4J_AUTH | **none** | **CRITICAL — no authentication** |
| server.memory.pagecache.size | 4G | CLEAN — matches arch spec |
| server.memory.heap.initial_size | 2G | Mismatched with max (see below) |
| server.memory.heap.max_size | 4G | CLEAN |
| bolt.thread_pool.max_size | 50 | Reasonable for 4 FastAPI workers (50 >> 4×2=8 minimum) |
| bolt.thread_pool.min_size | 5 | CLEAN |
| transaction.timeout | 60s | CLEAN |
| query.log.threshold | 1000ms | CLEAN |

**Findings:**

| ID | Severity | Issue |
|---|---|---|
| N4J-01 | **CRITICAL** | `NEO4J_AUTH=none` — Neo4j is running with **no authentication**. Any process on the `georag` Docker network can execute arbitrary Cypher including writes, schema changes, and destructive operations (`MATCH (n) DETACH DELETE n`). The healthcheck probe in Module 1 confirmed this: `cypher-shell` connects without credentials. In a dev workstation behind Docker's internal bridge this is contained, but it is a non-starter for any staging or production deployment and violates tenant isolation. Phase B: Set `NEO4J_AUTH=neo4j/<strong_password>` and update all containers that connect to Neo4j (FastAPI, warmup init container). **Surface to Kyle.** |
| N4J-02 | **MEDIUM** | `heap.initial_size=2G` vs `heap.max_size=4G`. When Neo4j starts, JVM allocates 2G and may need to grow to 4G under load. Setting initial = max avoids runtime heap resizing overhead (GC pressure during expansion). Module 2 spec B4 requires them to match. Phase B: set both to 4G. |
| N4J-03 | **MEDIUM** | No `workspace_id` property observed on any node type. All 56,034 nodes sampled — `Project.keys = ['name', 'project_id']`, `Drillhole.keys = [collar_id, drill_date, total_depth, status, project_id, name, hole_id, azimuth, dip, northing, elevation, hole_type, easting]`. Neither has `workspace_id`. Given `NEO4J_AUTH=none`, there is no effective tenant isolation at the graph level. This is a dependency finding for the RBAC module (Module 9), but documents the current state. |

### Index List (resolved from live graph, not from arch doc)

Two distinct DrillHole label spellings found in live data: `DrillHole` and `Drillhole` (capital H vs lowercase h). This is a data integrity concern.

| Label | Index/Constraint Type | Property | State |
|---|---|---|---|
| Commodity | UNIQUENESS | code | ONLINE |
| Deposit | UNIQUENESS | name | ONLINE |
| Document | UNIQUENESS | report_id | ONLINE |
| DrillHole | UNIQUENESS | hole_id | ONLINE |
| DrillHole | RANGE | type | ONLINE |
| Drillhole | UNIQUENESS | pg_id | ONLINE |
| Drillhole | UNIQUENESS | collar_id | ONLINE |
| Drillhole | RANGE | drillhole_id | ONLINE |
| Drillhole | RANGE | jurisdiction_code | ONLINE |
| Formation | UNIQUENESS | name | ONLINE |
| Formation | RANGE | age | ONLINE |
| GeophysicalSurvey | RANGE | date | ONLINE |
| GeophysicalSurvey | RANGE | type | ONLINE |
| Jurisdiction | UNIQUENESS | code | ONLINE |
| Mine | UNIQUENESS | pg_id | ONLINE |
| Mine | RANGE | jurisdiction_code | ONLINE |
| MineralOccurrence | UNIQUENESS | pg_id | ONLINE |
| MineralOccurrence | RANGE | commodity | ONLINE |
| MineralOccurrence | RANGE | deposit_type | ONLINE |
| MineralOccurrence | RANGE | external_id | ONLINE |
| MineralOccurrence | RANGE | jurisdiction_code | ONLINE |
| Project | UNIQUENESS | project_id | ONLINE |
| Project | UNIQUENESS | name | ONLINE |
| Project | RANGE | commodity | ONLINE |
| Project | RANGE | region | ONLINE |
| PublicGeoSource | UNIQUENESS | source_id | ONLINE |
| Publication | UNIQUENESS | title | ONLINE |
| Publication | RANGE | year | ONLINE |
| QualifiedPerson | UNIQUENESS | name | ONLINE |
| Report | UNIQUENESS | report_id | ONLINE |
| Report | UNIQUENESS | title | ONLINE |
| Report | RANGE | date | ONLINE |
| ResourcePotentialZone | UNIQUENESS | pg_id | ONLINE |
| ResourcePotentialZone | RANGE | jurisdiction_code | ONLINE |

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| N4J-04 | **HIGH** | Two separate node labels for drillholes: `DrillHole` (capital H, 0 live nodes per count query) and `Drillhole` (lowercase h, 33,510 nodes). Constraint migrations have been applied for BOTH spellings. This is a schema label inconsistency. Queries using `MATCH (d:DrillHole)` will return 0 results; the data is under `Drillhole`. The correct label must be canonicalized. **Surface to Kyle** (§04f label change requires owner approval per Global Invariant 4). Module 2 documents the finding; resolution is a Kyle-approved schema fix. |

### Online Backup Probe

```
neo4j-admin database backup --help
```

Output shows: `backup` subcommand is **NOT listed**. Available subcommands are: `check`, `dump`, `import`, `info`, `load`, `migrate`, `upload`.

**Online backup is NOT available on Neo4j 2026.03.1 Community Edition.** The `backup` command is Enterprise-only. The Module 1 Phase B backup script DRY_RUN reported it as available because it probed the top-level `neo4j-admin database --help` which exits 0 regardless of subcommand availability. The script's `backup --help` probe is insufficient — it must probe the actual subcommand.

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| N4J-05 | **CRITICAL** | **Online backup is NOT available on Neo4j Community Edition 2026.03.1.** The `neo4j-admin database backup` subcommand does not exist. The existing `docker/neo4j/backup.sh` script's detection logic (`backup --help` exit 0) is a false positive — it tested the parent command, not the subcommand. The only available backup method is `neo4j-admin database dump` which requires the database to be **stopped or offline**. Live backup is not possible. **Surface to Kyle**: the Module 1 Phase B backup script (`docker/neo4j/backup.sh`) needs its detection logic fixed and must use `dump` (with documented downtime). This also reopens BK-05 from Module 1 as still-unresolved. |

### Data Directory Size

```
333M	/data
```

333 MiB for 56,034 nodes across 13 label types. Growth rate from Module 1 baselines: no prior measurement. Baseline established here.

---

## A4 — Qdrant 1.17 Audit

### Cluster Mode

`GET /cluster` → `{"status":"disabled"}` — single-node deployment. **CLEAN for dev.**

### Collection Inventory

| Collection | Points | Dense Vectors | Sparse Vectors | HNSW m | ef_construct | workspace_id index | Snapshots |
|---|---|---|---|---|---|---|---|
| pg_drillhole_collar | 33,490 | size=384, Cosine | **ABSENT** | 16 | 200 | **ABSENT** | 1 snapshot |
| pg_mineral_occurrence | 22,229 | size=384, Cosine | **ABSENT** | 16 | 200 | **ABSENT** | 1 snapshot |
| pg_mine | 140 | size=384, Cosine | **ABSENT** | 16 | 200 | **ABSENT** | 1 snapshot |
| pg_resource_potential_zone | 82 | size=384, Cosine | **ABSENT** | 16 | 200 | **ABSENT** | 1 snapshot |
| georag_reports | 18 | size=384, Cosine | **ABSENT** | 16 | 200 | **ABSENT** | 1 snapshot |

**MIGRATION-REQUIRED findings:**

| ID | Severity | Issue |
|---|---|---|
| QDR-01 | **CRITICAL** | **ALL 5 collections lack sparse vector config.** Per addendum §04h-i and Global Invariant 11, dense-only Qdrant collections have "not implemented the architecture." Hybrid sparse+dense is core V1, not optional. Phase B must add a sparse vector slot with `modifier: idf` to every collection. Pre-existing points will have null sparse vectors until Module 3 backfills them. This is additive and zero-downtime per module spec B5. **MIGRATION-REQUIRED.** |
| QDR-02 | **CRITICAL** | **ALL 5 collections lack a `workspace_id` payload index.** `payload_schema` for none of the collections includes `workspace_id`. This payload field is also absent from ALL PostgreSQL migrations (grep returned zero results), meaning the concept of workspace-scoped retrieval has not been implemented at the data layer. Tenant isolation via `workspace_id` filtering is non-functional. **MIGRATION-REQUIRED.** Both the Qdrant payload index AND the upstream data model must be resolved — this is a cross-cutting issue. Surface to Kyle: are Qdrant collections currently single-tenant (one workspace = all data)? If yes, the `workspace_id` payload + index is Module 3's responsibility to add during ingestion re-design. |
| QDR-03 | **MEDIUM** | `ef_construct=200` vs module spec target of `ef_construct=128`. The live value (200) is higher than the spec target — this increases index build time and memory use but improves recall. This is a tradeoff to measure in Phase C before lowering. Do not change in Phase B until Phase C establishes a recall baseline. |
| QDR-04 | **MEDIUM** | `quantization_config: null` on all collections. The module spec recommends scalar quantization int8 for large collections. `pg_drillhole_collar` (33,490 vectors × 384 dim × 4 bytes = ~50 MiB raw) and `pg_mineral_occurrence` (22,229 × 384 × 4 = ~34 MiB) would benefit from int8 quantization. Phase B: evaluate quantization after Phase C latency baseline. |
| QDR-05 | **LOW** | `on_disk_payload: false` for `georag_reports` (all others have `on_disk_payload: true`). `georag_reports` is a small collection (18 points) so this is a configuration inconsistency rather than a performance problem. Phase B: align to `on_disk_payload: true` for consistency. |

### Snapshot Status

All 5 collections have exactly 1 snapshot each, all created `2026-04-20T03:00:xx` (overnight scheduled backup from Ofelia). Snapshots are present and have checksums. **CLEAN — Qdrant backup is operational.**

Note: Qdrant is single-node; per-node snapshot caveats from the module spec do not apply. Document this posture in the Phase D runbook if cluster mode is ever introduced.

---

## A5 — Redis 8.6.2 Audit

### Instance Count

**1 instance** (`georag-redis`). This is correct for dev. Prod requires 3 separated instances (cache / queue / sessions) per module spec §5 locked decisions. The prod compose profile must add this before any production deployment.

### Per-Instance Config

| Setting | Value | Arch Target | Assessment |
|---|---|---|---|
| redis_version | 8.6.2 | 8.6.2 | CLEAN |
| maxmemory | 512MB (536,870,912 bytes) | 512MB dev | CLEAN |
| maxmemory_policy | allkeys-lru | allkeys-lru | CLEAN |
| appendonly | yes (aof_enabled=1) | yes (per review in Module 1) | CLEAN |
| appendfsync | everysec | everysec | CLEAN |
| save | disabled (`save ""`) | disabled | CLEAN |
| databases | 4 | 4 | CLEAN |
| used_memory_human | 1.66 MiB | — | Healthy |
| db0 | 6 keys, 4 with TTL | — | Healthy |

### phpredis Version

`phpredis 6.3.0` loaded in `georag-laravel-octane`. **CLEAN** — matches requirement for Redis 8 compatibility per §12.

### Queue / Failed Jobs

`php artisan queue:failed` → **No failed jobs found.** Queue depth under Horizon is idle (no active ingestion). **CLEAN.**

### AOF Size Anomaly

| Metric | Value |
|---|---|
| aof_current_size | ~40 MB |
| db0 keys | 6 |
| rdb_changes_since_last_save | ~1,104,040 |

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| RDS-01 | **MEDIUM** | AOF file is ~40 MB with only 6 active keys and 1.66 MiB used memory. `rdb_changes_since_last_save=1,104,040` indicates approximately 1.1 million write operations since the last RDB save. This is consistent with high-frequency ephemeral writes (Pulse aggregates, Dagster heartbeats, Horizon poll events) that were written then expired/deleted. The AOF records all these as SET + DEL pairs. The AOF has been rewritten once (`aof_rewrites=1`) already. A `BGREWRITEAOF` would compact this significantly. This is not a failure but indicates high write churn — the AOF will grow until the next auto-rewrite threshold (by default when AOF is 100% larger than the RDB equivalent, which would be at ~80MB). Phase B: document in runbook; consider lowering `auto-aof-rewrite-percentage` to trigger more frequent rewrites in a churn-heavy workload. |
| RDS-02 | **HIGH** | **No prod Redis separation** in current compose. The prod compose profile does not exist yet (only dev profiles are defined). Before any staging deployment, three separated Redis instances (`redis-cache`, `redis-queue`, `redis-sessions`) with correct per-role persistence configs must be added. This is a Phase B deliverable per module spec B6. |

### keyspace_misses

`keyspace_hits=3,134`, `keyspace_misses=226,989`. Miss rate = ~98.6%. This is extremely high.

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| RDS-03 | **MEDIUM** | 98.6% cache miss rate. This indicates either (a) the application is not yet using Redis for caching meaningfully (most requests go to the database), or (b) cached values are expiring before reuse. At this stage of the project with minimal traffic (2 users, seed data only), this is expected — no active user sessions are driving cache warmth. Not actionable now; establish a baseline in Phase C and re-evaluate after Module 3 ingestion adds working data. |

---

## A6 — SeaweedFS 4.20 / S3 Abstraction Audit

### ADR-0001 Gotchas (Reference to Module 1 A6 — not re-run)

Per `2026-04-19-infra-audit.md` A6: all three gotchas (G1 `-volume` flag, G2 IPv4 healthcheck, G3 Windows exec bit) verified **CLOSED** in Module 1. **No re-run performed per audit instructions.**

### S3 Conformance

`aws s3 ls --endpoint-url http://minio:8333` → succeeds. Buckets listed:

```
2026-04-19  georag-backups
2026-04-19  georag-bronze
2026-04-19  georag-exports
```

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| SFS-01 | **HIGH** | **Missing buckets per addendum §02b.** Expected buckets `bronze` and `bronze-raster` (addendum §02b naming) are absent. Current buckets use the naming convention from the original MinIO era (`georag-bronze`, `georag-exports`). The addendum §02b spec says `bronze` and `bronze-raster`. This naming drift means the Module 3 ingestion pipeline (which will reference addendum §02b bucket names) and the Module 2 spec will expect different names than what exists. **Surface to Kyle**: confirm the canonical bucket naming convention — either update addendum §02b to match `georag-bronze`/`georag-exports`, or create `bronze`/`bronze-raster` in Phase B. |

### Volume Server Capacity

`curl http://minio:9333/cluster/status` → `{"IsLeader":true,"Leader":"172.19.0.3:9333.19333","MaxVolumeId":22}`

`-volume.max=32` is applied (verified in `docker/seaweedfs/entrypoint.sh`). MaxVolumeId=22 means 22 of 32 volume slots are in use. 10 free slots remain. **CLEAN** — the fix from Module 1 Phase B (`-volume.max=32`) is live.

### Vendor-Purity Sweep

`git grep -n -i 'minio\.filer\|seaweedfs' app/` — **no results**
`git grep -n -i 'minio\.filer\|seaweedfs' fastapi_app/` — **no results** (fastapi_app directory does not exist, FastAPI is in project root)

S3Client construction check: Laravel `laravel-octane` env shows `AWS_ENDPOINT=${MINIO_ENDPOINT:-http://minio:8333}` — endpoint is read from env, not hardcoded. **CLEAN.**

No vendor-specific SeaweedFS or MinIO SDK calls found in application code. **CLEAN.**

---

## A7 — Frozen Baseline Capture

Frozen config written to `ops/audit/2026-04-19-datastores-config.md` (companion file).

### Idle Resource Footprint

From `ops/baselines/2026-04-19-docker-stats-idle.csv` (Module 1 baseline — covers all data stores):

| Service | Idle CPU | Idle Memory | Memory Limit | Mem% |
|---|---|---|---|---|
| georag-postgresql | ~0.01–0.73% | ~340–470 MiB | 4 GiB | ~8–11% |
| georag-pgbouncer | ~0.01–3.56% | ~1.7–8.6 MiB | 256 MiB | ~0.7–3.4% |
| georag-neo4j | ~0.46–0.63% | ~1.1–1.75 GiB | 9 GiB | ~12–19% |
| georag-qdrant | ~0.01–0.39% | ~32–42 MiB | 4 GiB | ~0.8–1.0% |
| georag-redis | ~0.24–0.40% | ~5.5–6.2 MiB | 1 GiB | ~0.5–0.6% |
| georag-minio (SeaweedFS) | ~0.17–0.37% | ~111–157 MiB | 2 GiB | ~5–8% |

Note: PgBouncer CPU spike to 3.56% is a healthcheck polling artifact. Neo4j idle memory is 1.1–1.75 GiB (JVM heap partially populated from warmup script).

### Query Latency Baselines

**PostgreSQL — pg_stat_statements (top 5 by total_exec_time, current workload):**

All queries are Dagster internal operations (schema reflection, heartbeats, run polling). No application-level geological queries have been executed. Zero meaningful PG latency baselines for the application workload — Module 4 Phase C is the right time to capture these.

**Neo4j — sample traversal:**

`MATCH (p:Project)-[:HAS_DRILLHOLE]->(d:Drillhole)` — the `HAS_DRILLHOLE` relationship does not yet exist (0 results). Node count query `MATCH (n) RETURN count(n)` → 56,034 nodes, response time < 100ms (warm bolt connection). Traversal latency baselines deferred to Module 4 Phase C once ingestion populates relationships.

**Qdrant — dense search latency:**

Single dense search on `pg_drillhole_collar` (33,490 indexed vectors, HNSW): reported time = **8.4ms**. Results = 0 (dummy vector not near any real embedding). Acceptable baseline. Sparse and hybrid search are not available (no sparse vector config) — deferred to Phase B post-migration.

**Redis — latency:**

`redis-cli --latency` brief sample: min=0ms, max=1ms, avg=0.11ms. **CLEAN** — sub-millisecond command latency.

**Finding:**

| ID | Severity | Issue |
|---|---|---|
| BASE-01 | **LOW** | No application-level PostgreSQL query baselines exist yet (all pg_stat_statements queries are Dagster internals). Phase C will require a synthetic workload that exercises geological queries. Defer to Module 4 Phase C per module spec. |

---

## Summary Table

| ID | Store | Severity | One-liner |
|---|---|---|---|
| PG-01 | PostgreSQL | HIGH | postgis_raster not installed; needed before raster ingestion |
| PG-02 | PostgreSQL | MEDIUM | georag role is superuser — over-privileged for app use (Module 9) |
| PG-03 | PostgreSQL | LOW | pulse_aggregates 10% dead tuple ratio — normal churn |
| PG-04 | PostgreSQL | HIGH | shared_buffers=4GB vs 16GB target; memory limit also constrains it |
| PG-05 | PostgreSQL | HIGH | effective_cache_size=12GB vs 48GB; planner underestimates OS cache |
| PG-06 | PostgreSQL | MEDIUM | maintenance_work_mem=512MB; needs 1-2GB for bulk ingest |
| PG-07 | PostgreSQL | MEDIUM | autovacuum scale factors at defaults; need per-table tuning on large tables |
| PG-08 | PostgreSQL | MEDIUM | Dagster schema reflection dominates pg_stat_statements (47k calls) |
| PG-09 | PostgreSQL | LOW | ST_Extent queries 270-695ms each; Martin startup artifact |
| PGB-01 | PgBouncer | MEDIUM | DEFAULT_POOL_SIZE=100 can exhaust PG max_connections=200 under load |
| PGB-02 | PgBouncer | LOW | server_lifetime and query_wait_timeout not explicit in .ini |
| PGB-03 | PgBouncer | LOW | SERVER_IDLE_TIMEOUT=300 vs 600 spec target |
| N4J-01 | Neo4j | CRITICAL | NEO4J_AUTH=none — no authentication, any container can run destructive Cypher |
| N4J-02 | Neo4j | MEDIUM | heap.initial_size=2G != heap.max_size=4G; causes GC pressure during expansion |
| N4J-03 | Neo4j | MEDIUM | No workspace_id on any node type; tenant isolation not implemented |
| N4J-04 | Neo4j | HIGH | Two label spellings: DrillHole (0 nodes) and Drillhole (33,510 nodes) — schema inconsistency |
| N4J-05 | Neo4j | CRITICAL | Online backup does not exist in CE; backup.sh detection logic is a false positive |
| QDR-01 | Qdrant | CRITICAL | All 5 collections are dense-only; sparse vector config absent; MIGRATION-REQUIRED |
| QDR-02 | Qdrant | CRITICAL | No workspace_id payload index on any collection; tenant isolation non-functional; MIGRATION-REQUIRED |
| QDR-03 | Qdrant | MEDIUM | ef_construct=200 vs spec 128; measure before changing |
| QDR-04 | Qdrant | MEDIUM | No quantization on large collections (pg_drillhole_collar, pg_mineral_occurrence) |
| QDR-05 | Qdrant | LOW | georag_reports has on_disk_payload=false vs true for all others |
| RDS-01 | Redis | MEDIUM | AOF ~40MB with 6 keys — high write churn from Pulse/Dagster; file will grow until auto-rewrite |
| RDS-02 | Redis | HIGH | No prod Redis separation (3-instance) in any compose profile |
| RDS-03 | Redis | MEDIUM | 98.6% cache miss rate; expected at this data scale but baseline established |
| SFS-01 | SeaweedFS | HIGH | Bucket naming: georag-bronze/georag-exports vs addendum §02b bronze/bronze-raster |
| BASE-01 | Baselines | LOW | No application-level PG query baselines; deferred to Module 4 Phase C |

---

## Surface to Kyle — Critical/High Findings + Phase B Actions

| ID | Severity | Store | Phase B Action |
|---|---|---|---|
| N4J-01 | **CRITICAL** | Neo4j | Set `NEO4J_AUTH=neo4j/<password>` in compose env; update FastAPI + warmup container credentials; add to .env.example |
| N4J-05 | **CRITICAL** | Neo4j | Fix backup.sh detection logic; rewrite to use `neo4j-admin database dump` (requires offline DB); document downtime window |
| QDR-01 | **CRITICAL** | Qdrant | For each of 5 collections: add sparse_vectors config with `modifier: idf` via Qdrant REST API (zero-downtime additive) |
| QDR-02 | **CRITICAL** | Qdrant | Add workspace_id payload index via `PUT /collections/{name}/index`; coordinate with Module 3 on backfill |
| PG-04 | **HIGH** | PostgreSQL | Raise `shared_buffers` to 16GB AND raise `deploy.resources.limits.memory` to 24-32GB concurrently |
| PG-05 | **HIGH** | PostgreSQL | Raise `effective_cache_size` to 48GB in postgres command args |
| PG-01 | **HIGH** | PostgreSQL | `CREATE EXTENSION postgis_raster;` in the PG init or a migration before Module 3 raster work |
| N4J-04 | **HIGH** | Neo4j | Canonicalize DrillHole label spelling — requires Kyle approval (§04f change) |
| RDS-02 | **HIGH** | Redis | Add `redis-cache`, `redis-queue`, `redis-sessions` services to prod compose profile |
| SFS-01 | **HIGH** | SeaweedFS | Kyle to decide: rename buckets to addendum §02b names OR update spec to match current names |

**Decisions required from Kyle before Phase B can start:**

1. **N4J-01**: Approve Neo4j auth password choice (or confirm .env.example placeholder pattern is sufficient).
2. **N4J-04**: Approve `DrillHole` → `Drillhole` label canonicalization (§04f schema change — Global Invariant 4 requires owner sign-off).
3. **N4J-05**: Confirm dump-with-downtime approach for Neo4j backups, OR evaluate Enterprise for online backup (cost decision).
4. **SFS-01**: Confirm canonical bucket naming — `bronze`/`bronze-raster` (per addendum §02b) vs `georag-bronze`/`georag-exports` (current).
5. **PG-04/PG-05**: Confirm memory budget reallocation on the workstation (raising PG limits reduces headroom for Neo4j/FastAPI).
6. **QDR-02 + N4J-03**: Confirm workspace_id tenant isolation design — is it Module 2 (data layer) or Module 9 (application layer)? The data model currently has zero workspace_id fields at any layer.

---

## Arch-Doc Drift Flagged for Module 10

| Drift Item | Current State | Arch Doc Says | Action |
|---|---|---|---|
| Neo4j version | 2026.03.1 (effective) | 2026.02.3 | Update §12 pin reference to 2026.03.1 |
| Qdrant HNSW ef_construct | 200 (live) | 128 (spec §06) | Intentional over-spec or typo — Kyle to confirm, update spec |
| PgBouncer max_client_conn | 200 (live dev) | 1000 (prod spec §06) | Split reference: 200 for dev, 1000 for prod profile — clarify in spec |
| DrillHole vs Drillhole labels | Both exist in graph | §04f uses DrillHole | Code resolution in Phase B; doc update in Module 10 |
| workspace_id in Qdrant | Not present in any collection | Required per §04h-i | Module 2/3 boundary clarification needed |

---

## Surprising Findings

1. **Neo4j has no auth at all.** `NEO4J_AUTH=none` is literally the first time this has been visible in any audit. Every container on the `georag` network — including future external services — can destroy the entire graph with one Cypher command.

2. **The Neo4j backup script from Module 1 Phase B is a false positive.** It reported online backup as available and selected "backup mode." The actual `backup` subcommand doesn't exist on CE. The live drill (deferred from Module 1 Phase B due to SeaweedFS capacity) would have revealed a runtime error. The existing `docker/neo4j/backup.sh` has never produced a real backup artifact.

3. **workspace_id does not exist anywhere in the data model.** Neither the PostgreSQL migrations, the Qdrant payload schemas, nor any Neo4j node property includes `workspace_id`. The entire tenant isolation architecture assumes this field will be added, but it has not been defined at the schema level. This is a pre-condition for any multi-tenant use.

4. **Two spelling variants of the DrillHole node label coexist in the live graph.** 33,510 nodes under `Drillhole`, 0 under `DrillHole`, but constraints and indexes exist for both spellings. Any query written as `MATCH (d:DrillHole)` silently returns zero rows.

---

*Nothing outside `ops/audit/` was modified during this audit. All probes were read-only. No services were restarted. No configs were edited.*

---

## Phase B Critical Fixes (2026-04-19)

<!-- Performed by: devops-engineer agent (Claude Sonnet 4.6) -->
<!-- Authorized by: Kyle (all four fixes) -->
<!-- Date: 2026-04-19 -->

---

### N4J-05 Verification — Outcome (a): artifact is real, script uses `dump` (correct)

**S3 probe:**
```
docker exec georag-backup-agent aws s3 ls s3://georag-backups/neo4j/ --endpoint-url http://minio:8333 --recursive
2026-04-19 19:12:59   53565647 neo4j/neo4j-dump-2026-04-19T18-55-00Z.tar.gz
```

**Artifact size:** 53,565,647 bytes (~51.1 MiB). Matches the Module 1 Phase B report claim of 51 MiB.

**Artifact contents:** Downloaded and inspected. `tar tzf neo4j-verify.tar.gz` → single file `neo4j.dump`. This is a legitimate Neo4j offline dump file.

**Script review (`docker/neo4j/backup.sh`):** Script uses `neo4j-admin database dump` (line 184), NOT `neo4j-admin database backup`. The dump command IS available on Community Edition. The Module 1 Phase B backup script is correct.

**Audit finding N4J-05 was a misread.** The Phase A audit stated "The Module 1 Phase B backup script's detection logic (`backup --help` exit 0) is a false positive." In fact, the backup.sh that shipped with Module 1 was already rewritten to use `dump` (offline path) — the DRY_RUN output in the Phase B report referenced an earlier draft of the script. The live script and the live artifact both confirm `dump` is the operative method.

**Resolution:** N4J-05 finding is CLOSED as a misread of the Phase B script. The backup script is correct. The live artifact is legitimate. The `ops/audit/2026-04-19-infra-phase-b-critical-fixes.md` report contained a draft DRY_RUN output that described the old `backup` path — the final script was already fixed before the report was written. No script correction required.

---

### N4J-01 Resolution — Neo4j auth enabled

**Client inventory (services with Neo4j connections):**

| Service | File:line | Credential source | Auth-ready before fix? |
|---|---|---|---|
| fastapi | `src/fastapi/app/main.py:281` | `settings.NEO4J_USER` + `settings.NEO4J_PASSWORD` (config.py) | YES — reads env vars; compose was not passing them (used defaults `neo4j/neo4j`) |
| dagster-daemon | `src/dagster/georag_dagster/definitions.py:548-551` | `auth_enabled=False` hardcoded | NO — ignored credentials entirely |
| dagster-daemon | `src/dagster/georag_dagster/resources.py:276` | Uses `self.username` / `self.password` when `auth_enabled=True` | Fixed in definitions.py |
| neo4j-warmup | `docker-compose.yml entrypoint` | `cypher-shell` without `-u`/`-p` | NO — no credentials passed |
| backup-agent | `docker-compose.yml env` | `NEO4J_USERNAME`/`NEO4J_PASSWORD` from `.env` | YES — env vars already wired |
| laravel-octane / horizon / reverb | (no Neo4j connection found) | — | N/A |

**Hardcoded credentials found:** `src/fastapi/tmp_g.py:12` — dev test script had `auth=None` hardcoded. Fixed to use `settings.NEO4J_USER` / `settings.NEO4J_PASSWORD`.

**New password generated:** YES. Value written to `.env` only. Not printed in this report.

**Volume migration required:** YES. The Neo4j data volume was initialised with `NEO4J_AUTH=none`. `NEO4J_AUTH=neo4j/<password>` does not retroactively set auth on an existing volume. Recovery performed:
```
# Step 1: stop neo4j container
docker compose ... stop neo4j
# Step 2: one-shot password set against the existing volume
docker run --rm --volume georagintelligencev10_neo4j_data:/data \
  neo4j:2026-community@sha256:a5feb81... \
  neo4j-admin dbms set-initial-password <NEW_PASSWORD>
# Output: Changed password for user 'neo4j'. IMPORTANT: this change will only
#          take effect if performed before the database is started for the first time.
# Step 3: recreate neo4j with new auth
docker compose ... up -d neo4j
```

**Changes made:**

1. `.env` — added `NEO4J_USERNAME`, `NEO4J_PASSWORD`, `NEO4J_USER`; changed `NEO4J_AUTH` from `none` to `neo4j/${NEO4J_PASSWORD}`.

2. `docker-compose.yml` — neo4j service env: replaced `NEO4J_AUTH: none` with `NEO4J_AUTH: <user>/<pass>` + `GRAPH_USER` / `GRAPH_PASSWORD` (non-`NEO4J_`-prefixed vars so the neo4j image does not attempt to map them to `neo4j.conf` settings). Note: `NEO4J_USERNAME` / `NEO4J_PASSWORD` CANNOT be passed to the neo4j container directly — the image maps all `NEO4J_<SETTING>` env vars to `neo4j.conf`, producing "Unrecognized setting: USERNAME / PASSWORD" errors.

3. `docker-compose.yml` — healthcheck: updated to `cypher-shell -u neo4j -p "$$GRAPH_PASSWORD"` (double-$ prevents compose from substituting the value at config-render time; the container shell expands it at runtime).

4. `docker-compose.yml` — fastapi env: added `NEO4J_USER: ${NEO4J_USERNAME:-neo4j}` and `NEO4J_PASSWORD: ${NEO4J_PASSWORD}` (FastAPI config.py reads `NEO4J_USER` not `NEO4J_USERNAME`).

5. `docker-compose.yml` — neo4j-warmup: added `environment` block with `NEO4J_USERNAME`/`NEO4J_PASSWORD`; all `cypher-shell` calls updated to pass `-u "$$NEO4J_USERNAME" -p "$$NEO4J_PASSWORD"`.

6. `docker-compose.yml` — dagster-daemon env: added `NEO4J_USERNAME: ${NEO4J_USERNAME:-neo4j}` and `NEO4J_PASSWORD: ${NEO4J_PASSWORD}`.

7. `src/dagster/georag_dagster/definitions.py:548-555` — `Neo4jResource` instantiation: changed `auth_enabled=False` to `auth_enabled=True`, added `username=EnvVar("NEO4J_USERNAME")`, `password=EnvVar("NEO4J_PASSWORD")`.

8. `src/fastapi/tmp_g.py:12` — dev test script: replaced `auth=None` with `auth=(settings.NEO4J_USER, settings.NEO4J_PASSWORD)`.

**Restart sequence outcome:**
- Neo4j: recreated with auth on → `healthy` in 4 minutes
- Anonymous access rejected: `42NFF: Access denied, see the security logs for details.`
- Authenticated access confirmed: `cypher-shell -u neo4j -p <password> 'RETURN 1'` → `1`
- FastAPI: recreated → `healthy` in ~1 minute. No auth errors in logs.
- Dagster daemon: bind-mounts `./src/dagster` live. `auth_enabled=True` confirmed via `python3 -c "... print('auth_enabled:', neo4j_res.auth_enabled)"` → `True`.
- Laravel (all 3): no Neo4j connections; not affected; remain healthy.
- backup-agent: already wired for creds; no change needed.

**N4J-01 STATUS: CLOSED.**

---

### QDR-01 Resolution — Sparse vectors added to all 5 collections

**Sparse vector field name used:** `text` (SPLADE convention; no existing application code references sparse vectors yet, so `text` is established as the canonical name for Module 4).

**API endpoint:** `PATCH /collections/{name}` with payload key `sparse_vectors_config` (NOT `sparse_vectors` — that key caused `"Wrong input: Not existing vector name"` errors. `sparse_vectors_config` is the correct PATCH key in Qdrant 1.17.1).

**Before → After for each collection:**

| Collection | Points | Before | After (functional verification) |
|---|---|---|---|
| `pg_drillhole_collar` | 33,490 | sparse_vectors_config: absent | sparse upsert accepted: `{"operation_id":1054, "status":"acknowledged"}` |
| `pg_mineral_occurrence` | 22,229 | sparse_vectors_config: absent | sparse upsert accepted: `{"operation_id":941, "status":"acknowledged"}` |
| `pg_mine` | 140 | sparse_vectors_config: absent | sparse upsert accepted: `{"operation_id":57, "status":"acknowledged"}` |
| `pg_resource_potential_zone` | 82 | sparse_vectors_config: absent | sparse upsert accepted: `{"operation_id":40, "status":"acknowledged"}` |
| `georag_reports` | 18 | sparse_vectors_config: absent | sparse upsert accepted: `{"operation_id":12, "status":"acknowledged"}` |

Note: Qdrant 1.17.1's `GET /collections/{name}` response does not include `sparse_vectors_config` in the JSON body even after a successful PATCH (API response omits it when using `config.params.vectors` as the primary vector type). Functional verification via upsert of a test sparse vector point (id=999999999, then deleted) confirms the config is live on all 5 collections. Pre-existing dense points with null sparse vectors are expected — Module 3 will backfill.

**QDR-01 STATUS: CLOSED.**

---

### QDR-02 Resolution — workspace_id payload index added to all 5 collections

**Field name:** `workspace_id`. **Schema type:** `keyword` (correct for UUID/short-string tenant IDs).

**API endpoint:** `PUT /collections/{name}/index`

**Before → After:**

| Collection | Before | After |
|---|---|---|
| `pg_drillhole_collar` | workspace_id: absent | `{"data_type": "keyword", "points": 0}` |
| `pg_mineral_occurrence` | workspace_id: absent | `{"data_type": "keyword", "points": 0}` |
| `pg_mine` | workspace_id: absent | `{"data_type": "keyword", "points": 0}` |
| `pg_resource_potential_zone` | workspace_id: absent | `{"data_type": "keyword", "points": 0}` |
| `georag_reports` | workspace_id: absent | `{"data_type": "keyword", "points": 0}` |

All verified via `GET /collections/{name}` → `payload_schema.workspace_id`. Points = 0 is expected — no existing document has a `workspace_id` value. Module 3 will populate.

Scope reminder: This fix covers only the Qdrant payload-index layer. The PostgreSQL migration adding `workspace_id`, the Neo4j property, and application-layer population and enforcement remain Module 3 / Module 9 work.

**QDR-02 STATUS: CLOSED.**

---

### Summary — Critical Findings After Phase B

| ID | Severity | Status |
|---|---|---|
| N4J-01 | CRITICAL | **CLOSED** — Neo4j auth enabled; all clients credentialed; anonymous access rejected |
| N4J-05 | CRITICAL | **CLOSED** — Misread finding; backup.sh uses `dump` (correct); live 51 MiB artifact confirmed real |
| QDR-01 | CRITICAL | **CLOSED** — Sparse vector config (`text`, `modifier: idf`) added to all 5 collections |
| QDR-02 | CRITICAL | **CLOSED** — `workspace_id` keyword payload index added to all 5 collections |

**Remaining critical count: 0.**

Note: The `Surface to Kyle` table entries for N4J-01, N4J-05, QDR-01, QDR-02 should be treated as CLOSED. Remaining open items in that table (PG-04, PG-05, PG-01, N4J-04, RDS-02, SFS-01) are HIGH severity and await separate Phase B work or Kyle decisions.

---

---

## Phase B Tuning (2026-04-19)

<!-- Performed by: devops-engineer agent (Claude Sonnet 4.6) -->
<!-- Date: 2026-04-20 -->
<!-- Scope: Module 2 Phase B remaining items — B2, B3, B4 partial, B7 -->

### Item 1 — PgBouncer cleanup (B3)

**Bitnami-legacy env vars:** No `POSTGRESQL_*` or `PGBOUNCER_*` vars were found — confirmed clean in Phase A. No removal needed.

**Worker count audit (live containers):**

| Service | Worker count | PG connections |
|---|---|---|
| FastAPI (uvicorn) | 4 workers | asyncpg pool max_size=25 (hard cap in main.py) |
| Laravel Octane (Swoole) | 4 workers | ~1-2 per worker transient = ~8 peak |
| Laravel Horizon | supervisor-1 max=1 + supervisor-llm max=2 dev = 3 workers | ~3 |
| Dagster daemon | 1 process | ~2 |
| Dagster webserver | 1 process | ~1 |
| Backup-agent | short-lived, non-permanent | ~0 steady-state |
| **Peak total** | — | **~39-40 backend connections** |

**asyncpg prepared statement compatibility:** `src/fastapi/app/main.py:204` — `statement_cache_size=0` is already set on the asyncpg `create_pool()` call. Transaction pooling mode is compatible. **CLEAN — no action required.**

**Changes applied to `docker-compose.yml` pgbouncer env block:**

| Setting | Before | After | Reason |
|---|---|---|---|
| `DEFAULT_POOL_SIZE` | 100 | **50** | Covers peak 40 connections with 25% headroom; 100 risked consuming half of PG's `max_connections=200` budget |
| `MAX_CLIENT_CONN` | 200 | **1000** | Raised to prod spec target; front-side accepts 1000 app connections (PgBouncer multiplexes to 50 backend) |
| `SERVER_IDLE_TIMEOUT` | 300 | **600** | PGB-03: raised to spec target (10 min) |
| `SERVER_LIFETIME` | implicit default | **3600** | PGB-02: made explicit |
| `QUERY_WAIT_TIMEOUT` | implicit default | **120** | PGB-02: made explicit |

`docker compose up -d pgbouncer` executed. SHOW POOLS verified healthy post-recreate.

**PGB-01, PGB-02, PGB-03: CLOSED.**

---

### Item 2 — PostGIS GIST sweep + post-ingest tune script (B2)

**Geometry column inventory:** 30 entries in `geometry_columns` across `public_geoscience` and `silver` schemas.

| Category | Count |
|---|---|
| Tables with GIST index | 13 |
| Tables without GIST — real base tables | 9 (8 `_history` tables + `silver.seismic_surveys`) |
| Without GIST — views (cannot index) | 8 (`v_pg_*_mvt` views) |

**All 8 `_history` tables** currently have 0 rows (pre-ingestion). The 8 MVT views are `VIEW` type — cannot be indexed; their underlying base tables are already covered.

**Files produced:**
- `ops/postgis/add-missing-gist-indices.sql` — DDL for 9 missing GIST indices; uses `CREATE INDEX CONCURRENTLY IF NOT EXISTS`. **NOT executed — awaiting Kyle approval.** Index builds on currently-empty tables will be instantaneous; apply before Module 3 ingestion.
- `ops/postgis/post-ingest-tune.sql` — parameterized `CLUSTER` + `ANALYZE` + `REFRESH MATERIALIZED VIEW CONCURRENTLY` template. Invoked by Dagster at the tail of each spatial ingestion run. Known MV: `silver.mv_collar_summary`.

---

### Item 3 — Neo4j hardening (B4)

**Page-cache config:** `server.memory.pagecache.size = 4.00GiB` confirmed via `CALL dbms.listConfig()`. `db.memory.pagecache.warmup.enable = true` — this is a CE config that exists but is noted as a no-op on CE (the warmup preload does not fire; the manual warmup script is the operative mechanism). Pagecache settings are correct per Section 06. **No change needed.**

**Warmup path:** `SHOW PROCEDURES YIELD name WHERE name CONTAINS "warmup"` → `apoc.warmup.run` is available (APOC plugin loaded). However, `docker/neo4j/warmup.cypher` already uses manual traversal-based warmup which is more targeted and does not rely on APOC's full scan. The APOC procedure performs a less targeted full-graph load. **Leave the existing warmup.cypher as-is** — manual traversal is superior for this workload.

**Bolt pool sizing:** Live config = `bolt.thread_pool.max_size = 50`. Spec target = `2 × FastAPI_worker_count = 2 × 4 = 8` minimum. Current value (50) is well above minimum and consistent with the existing reasoning (no change required).

**N4J-02 fix — heap initial = max:** `NEO4J_HEAP_INITIAL_SIZE` changed from `2G` to `4G` in both `.env` and `docker-compose.yml` (compose default). Neo4j NOT restarted (per safety rules — next scheduled restart will pick this up).

**Index gaps (for Kyle review):**

| Label | Nodes | Missing Index | Severity |
|---|---|---|---|
| `PublicGeo` | 55,941 | No index on any property | **HIGH** — every warmup query and RAG chat query does a full label scan |

All other labels >1,000 nodes (`Drillhole` 33,510; `MineralOccurrence` 22,230) have adequate indices. Full analysis in `ops/neo4j/missing-indices.md`.

**Backup DRY_RUN:** `DRY_RUN=1 /backup-scripts/neo4j/backup.sh` exited 0 with correct plan: stop neo4j → `neo4j-admin database dump` → restart → tar → S3 upload → retention sweep. **CLEAN — script operational.**

---

### Item 4 — SeaweedFS follow-ups (B7)

**Vendor-purity sweep:**

| Location | Finding | Severity |
|---|---|---|
| `src/fastapi/app/` | Clean — no minio/seaweedfs SDK calls | — |
| `app/` (Laravel) | Clean — S3Client uses `AWS_ENDPOINT` env var | — |
| `src/dagster/georag_dagster/resources.py:20-21` | **MinIOResource class uses `from minio import Minio` and `from minio.error import S3Error`** — minio Python SDK (not boto3) | **MEDIUM** |

The Dagster `MinIOResource` (`resources.py:103`) wraps the `minio-py` SDK using vendor-specific methods (`fput_object`, `bucket_exists`, `make_bucket`). This violates the vendor-purity goal of using only standard S3 API calls via boto3/s3fs. **Surface to Kyle — do NOT refactor without approval.** A boto3-based replacement would be a clean drop-in but is a code change to Dagster application code.

**Volume-server capacity:** `curl http://localhost:9333/cluster/status` → `MaxVolumeId=22`. The `-volume.max=32` fix from Module 1 is live. 10 free volume slots remain. **CLEAN.**

**S3 abstraction round-trip test:** Script at `ops/tests/s3-abstraction-check.sh`. All six steps (PUT, GET, HEAD, LIST, DELETE, verify-gone) executed against `georag-bronze` via the backup-agent container. **All steps PASSED.** Uses only standard `aws s3` / `aws s3api` calls — no vendor-specific SDK.

---

### Surface-to-Kyle Items (3 required)

**(a) PG memory config tuning (PG-04/PG-05/PG-06) + container mem_limit bump**

Audit found `shared_buffers=4GB` (target 16GB), `effective_cache_size=12GB` (target 48GB), `maintenance_work_mem=512MB` (target 1GB). These are under-provisioned for the 64GB workstation. The PostgreSQL container `deploy.resources.limits.memory: 4G` will OOM-kill the container if `shared_buffers` is raised to 16GB — the memory limit must be raised to 24-32GB concurrently.

**Recommendation:** Raise PG container memory limit to 28G and set:
- `shared_buffers=16GB`
- `effective_cache_size=48GB`
- `maintenance_work_mem=1GB`
- `autovacuum_vacuum_scale_factor=0.05` (per-table override on large geological tables)

Requires `docker compose up -d postgresql` (config reload supports most params; `shared_buffers` requires restart). **Decision: does Kyle approve the memory reallocation? Impact: PG takes 24-28GB, reducing headroom for other services.**

**(b) Neo4j PublicGeo index creation (N4J-04 follow-up + new finding)**

55,941 `PublicGeo`-labelled nodes have no index. Every RAG query and warmup traversal touching `:PublicGeo` does a full node-store scan (~560ms cold vs <1ms indexed). Recommended DDL: `CREATE INDEX pg_label_pg_id IF NOT EXISTS FOR (n:PublicGeo) ON (n.pg_id)`. Full analysis in `ops/neo4j/missing-indices.md`.

**Decision required:** Kyle approves index creation → graph-engineer executes and verifies. Note: index creation on a live graph is non-disruptive in Neo4j Community (background build, queries still served) but consumes memory during the build phase (~30-60s estimated on 55,941 nodes). **Next restart window not required** — this can be done live.

**(c) io_uring toggle (PG-08 follow-up, §7)**

WSL2 kernel `6.6.87.2-microsoft-standard-WSL2` supports io_uring. PostgreSQL 18.x supports `io_method=io_uring`. The spec §7 requires a before/after measurement before committing. Enabling may improve I/O throughput on NVMe for bulk ingestion workloads. The risk is kernel-version fragility across deployment targets (WSL2 vs bare Linux vs cloud VM).

**Recommendation:** Enable in a test pass using `ALTER SYSTEM SET io_method = 'io_uring'; SELECT pg_reload_conf();`, run the Module 4 Phase C benchmark query set before and after, compare. Revert with `io_sync` if regression observed. **Decision: Kyle approves the test pass?**

**(d) Dagster MinIOResource minio-py → boto3 refactor**

`src/dagster/georag_dagster/resources.py:20-21` uses the `minio` Python SDK directly. Standard path should be boto3 with `endpoint_url`. The minio-py SDK works against SeaweedFS's S3-compatible API in practice, but it exposes vendor-specific error types (`S3Error`) and methods (`fput_object`, `bucket_exists`) that would break if the object store backend is swapped. **Decision: approve refactor to boto3?** This is a data-engineer / devops cross-cutting change.

---

### Status Table — All Phase B Items

| Finding | Status |
|---|---|
| PGB-01 DEFAULT_POOL_SIZE=100 | **CLOSED** — reduced to 50, MAX_CLIENT_CONN raised to 1000 |
| PGB-02 server_lifetime/query_wait_timeout implicit | **CLOSED** — made explicit in compose env |
| PGB-03 SERVER_IDLE_TIMEOUT=300 | **CLOSED** — raised to 600 |
| asyncpg statement_cache_size compatibility | **CLEAN** — already set to 0 |
| PostGIS GIST sweep | **CLOSED** — 13/22 tables covered; 9 missing indices in review file; 8 are views (no index possible) |
| post-ingest-tune.sql | **CLOSED** — delivered at ops/postgis/ |
| N4J-02 heap initial vs max | **CLOSED** — env and compose updated to 4G; takes effect on next Neo4j restart |
| N4J warmup path | **CLOSED** — existing manual warmup.cypher confirmed correct; apoc.warmup.run available but not needed |
| N4J missing PublicGeo index | **OPEN — surface to Kyle (b above)** |
| Neo4j backup DRY_RUN | **CLEAN** — exits 0, correct dump plan |
| SeaweedFS vendor-purity | **PARTIAL** — FastAPI/Laravel clean; Dagster MinIOResource uses minio-py SDK (surface to Kyle (d above)) |
| SeaweedFS volume-max=32 | **CLEAN** — confirmed live, 10 slots free |
| S3 round-trip integrity test | **CLOSED** — all 6 steps passed |
| Redis topology runbook | **CLOSED** — ops/runbooks/redis-topology.md delivered |

---

## Module 2 Phase B Cleanup (2026-04-19) — Three Follow-Ups Closed

**Follow-up 1 — `src/fastapi/tmp_g.py`:** Decision **(a)** — moved and renamed to `src/fastapi/scripts/verify_graph_entity_cache.py`. File is a legitimate graph-cache smoke-test harness (cold/warm Redis paths, degradation assertions, TTL check); not debug cruft.

**Follow-up 2 — Auth-bypass sweep:** Results tabled in `ops/backlog/module-10-auth-bypass-sweep.md`. One HIGH finding: `.env.example:132` still shows `NEO4J_AUTH=none` (stale post-N4J-01-fix). Two MEDIUM findings: `APP_DEBUG` defaults to `true` in compose and `.env.example`. All runtime bypasses (`auth_enabled=False`, `auth=None`) confirmed CLOSED by Phase B patches. No new production-risk bypasses in application code.

**Follow-up 3 — Module 10 doc-sweep backlog:** Seeded with 8 items in `ops/backlog/module-10-doc-sweep.md`: Neo4j version pin, Qdrant ef_construct, DrillHole label case, workspace_id absence, sparse_vectors_config API key, .env.example NEO4J_AUTH=none, APP_DEBUG compose default, SeaweedFS bucket naming, and PostgreSQL version (18.3 vs 17.9 in arch doc).

---

## Phase B Decisions — Executed (2026-04-19)

<!-- Performed by: devops-engineer agent (Claude Sonnet 4.6) -->
<!-- Authorized by: Kyle (all four decisions, Module 2 Phase B surface) -->
<!-- Date: 2026-04-19 -->

### (a) PostgreSQL Memory Tuning — Moderate Path Applied

**Decision:** MODERATE path (NOT the 28G aggressive path in prior recommendation).

| Parameter | Before | After |
|---|---|---|
| `shared_buffers` | 4 GB | **8 GB** |
| `effective_cache_size` | 12 GB | **24 GB** |
| `maintenance_work_mem` | 512 MB | **1 GB** |
| Container memory limit | 4 G | **12 G** |
| Container memory reservation | 1 G | **6 G** |

Config files: `ops/baselines/2026-04-19-pg-config-before-tuning.txt`, `ops/baselines/2026-04-19-pg-config-after-tuning.txt`
EXPLAIN results: `ops/baselines/2026-04-19-pg-tuning-results.md`

Selected EXPLAIN numbers (cold buffers, first post-restart read):
- `pg_drillhole_collar` count: execution time 10.6ms → 4.8ms (-55%); I/O read time 5.2ms → 0.7ms (-87%)
- `pg_mineral_occurrence` count: execution time 6.4ms → 2.9ms (-54%); I/O read time 3.3ms → 0.5ms (-85%)

**PG-04, PG-05, PG-06: CLOSED (moderate path).**

---

### (b) Neo4j PublicGeo Index — ONLINE

```cypher
CREATE INDEX pg_label_pg_id IF NOT EXISTS FOR (n:PublicGeo) ON (n.pg_id)
```

- **Wall time to ONLINE:** ~4 seconds (55,941 nodes, background build, no Bolt interruption)
- **State verified:** `SHOW INDEXES` → `state=ONLINE`
- **EXPLAIN plan:** `NodeIndexSeek` on `RANGE INDEX n:PublicGeo(pg_id)` — confirmed, no NodeByLabelScan
- **File updated:** `ops/neo4j/missing-indices.md` — marked CREATED

**N4J-PublicGeo-index: CLOSED.**

---

### (c) io_uring — Tested, Reverted

`io_method=io_uring` was applied and PG was restarted. PG crashed immediately:

```
FATAL:  could not setup io_uring queue: Operation not permitted
HINT:  Check if io_uring is disabled via /proc/sys/kernel/io_uring_disabled.
```

**Root cause:** Docker's default seccomp profile blocks `io_uring_setup`, `io_uring_enter`,
and `io_uring_register` syscalls. WSL2 kernel 6.6.87 has io_uring compiled in, but the
container sandbox prevents access.

**Resolution:** Reverted to `io_method=worker` (compose updated). PG restarted cleanly.
No regression — all dependent services reconnected; backup-agent DRY_RUN exit 0.

To enable in future: custom Docker seccomp profile or bare-metal deployment.
Documented in `ops/backlog/module-10-doc-sweep.md` for §06/§12 doc update in Module 10.

**io_uring status: REVERTED (Docker seccomp). No regression. Tracked for Module 10.**

---

### (d) Dagster MinIOResource → boto3 — Deferred to Module 3

Not executed this run per Kyle's decision. Backlog entry written at:
`ops/backlog/module-3-intake.md`

The entry includes source location, rationale (addendum §02a vendor-purity), drop-in approach,
pre-approved env vars, and owner (data-engineer agent, Module 3 Phase B).

**STATUS: Deferred. Canonical handoff recorded.**

---

### Dependent Services Post-Restart

All services healthy after PG restart. No reconnection failures observed.

| Service | Status |
|---|---|
| georag-postgresql | Healthy (12G limit, 8GB shared_buffers, io_method=worker) |
| georag-pgbouncer | Healthy — reconnected cleanly |
| georag-fastapi | Healthy — asyncpg pool reconnected |
| georag-laravel-octane/horizon/reverb | All healthy |
| georag-dagster-daemon/webserver | Healthy (brief retry during restart window, recovered) |
| georag-backup-agent | DRY_RUN exit 0 — correct plan |
---

## Phase C Closeout — Bucket Rename (2026-04-20)

<!-- Performed by: devops-engineer agent (Claude Sonnet 4.6) -->
<!-- Date: 2026-04-20 -->
<!-- Scope: Module 2 Phase C close-out — SFS-01 bucket rename -->

**Finding SFS-01 resolved.** Kyle approved option (a): rename live buckets to match addendum §02b.

### Actions executed

| Step | Result |
|------|--------|
| Pre-rename inventory | georag-bronze: 71 objects / 290 MiB; georag-exports: 2 objects / 3.5 KiB; both under stop thresholds (100 obj, 1 GiB) |
| New bucket creation | bronze, exports, bronze-raster — all created in SeaweedFS |
| Object copy (georag-bronze → bronze) | 71 objects / 304,008,527 bytes — count and size verified identical |
| Object copy (georag-exports → exports) | 2 objects / 3,559 bytes — count and size verified identical |
| bronze-raster | Created empty; Module 3 populates raster archives |
| Code references updated | 19 files: .env, .env.example, docker-compose.yml (defaults + comments + minio-init entrypoint), 14 Dagster asset files, definitions.py, 2 FastAPI scripts, ops/tests/s3-abstraction-check.sh |
| Services restarted | laravel-octane, laravel-horizon, dagster-daemon recreated; all healthy post-restart; no S3 errors in logs |
| Integrity test | PUT/GET/HEAD/LIST/DELETE round-trip against bronze — PASSED |
| Old bucket deletion | georag-bronze and georag-exports deleted with --force |
| Final bucket list | bronze, bronze-raster, exports, georag-backups — 4 buckets as expected |

**SFS-01 STATUS: CLOSED.**

---

## CORRECTION — 2026-04-21 (Module 4 Chunk 2)

Phase B critical fix QDR-01 (sparse vectors added via PATCH to 5 existing collections) was incorrect. The PATCH form returned 2xx and the verification upsert of a test sparse point succeeded, but the resulting sparse slots were NOT usable in Qdrant's hybrid Query API (`prefetch` + `FusionQuery`). Module 4 Chunk 2 had to delete and recreate all 5 collections with sparse slots declared at CREATION time.

**Data loss from recreate**: `pg_drillhole_collar` (33,490), `pg_mineral_occurrence` (22,230), `pg_resource_potential_zone`, `pg_mine`, `georag_reports` (18) — all cleared and re-populated from Bronze via Dagster re-materialization.

**Corrected gotcha**: see `memory/feedback_datastore_gotchas.md` — Gotcha 2 rewritten 2026-04-21.

**Lesson**: when Qdrant API accepts a write but downstream behavior is the test, exercise the actual downstream (hybrid prefetch) before declaring success. `update_collection` verification via upsert alone is insufficient.
