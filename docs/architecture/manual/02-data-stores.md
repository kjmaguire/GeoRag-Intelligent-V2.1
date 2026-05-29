# Chapter 02 — Data Stores

Five durable stores plus a tile generator. This chapter covers what each is
*for*, how it is configured, and the wire path FastAPI / Laravel / Hatchet
use to reach it. Schema-level detail (every table, every index) lives in
[Ch 03 — Schemas](03-schemas.md).

---

## 1. PostgreSQL 18 + PostGIS 3.6 — the central source of truth

### Image build

The `georag/postgres:18-ext` image
([docker/postgresql/Dockerfile](../../../docker/postgresql/Dockerfile)) starts
from `postgis/postgis:18-3.6-alpine` and layers on the rest of the extension
matrix. The Alpine base is intentional — it’s the only 17/18-3.6 published
variant at the time of bump.

### Extensions installed

Compiled into the image and registered via
[`docker/postgresql/init/10-phase0-extensions-and-schemas.sql`](../../../docker/postgresql/init/10-phase0-extensions-and-schemas.sql)
plus [`init-postgis.sql`](../../../docker/postgresql/init/init-postgis.sql):

| Extension | Schema | Purpose |
|---|---|---|
| `postgis` | `public` | Vector/Raster geospatial (geom, geography columns) |
| `postgis_raster` | `public` | Raster types — required by `h3_postgis` ([10-phase0-extensions-and-schemas.sql:36-41](../../../docker/postgresql/init/10-phase0-extensions-and-schemas.sql)) |
| `pgcrypto` | `public` | HMAC + symmetric encryption (`AUDIT_ENCRYPTION_KEY`) |
| `pg_trgm` | `public` | Trigram fuzzy match for hole-ID lookups, deposit name resolution |
| `pg_stat_statements` | `public` | Query stats — preloaded |
| `auto_explain` | core | Log slow plan trees — preloaded; logs > 2 s in JSON ([docker-compose.yml:250-254](../../../docker-compose.yml)) |
| `h3` + `h3_postgis` | `public` | Hex spatial indexing — drives `gold.h3_density_mineral` |
| `hypopg` | `public` | Hypothetical-index evaluation — Index Health Agent |
| `pg_stat_kcache` | `public` | Kernel-level CPU/IO per query |
| `pg_partman` | `partman` | Declarative monthly partitions on `audit.audit_ledger`, `workflow.workflow_runs` |
| `pg_repack` | `public` | Online table reorg without exclusive locks |
| `pg_ivm` | `public` | Incrementally-maintained materialised views (ready, not yet used) |
| `vector` | `public` | Optional — only present for embedding-comparison sandboxes; production embeddings live in Qdrant |

Verification block at the end of
[10-phase0-extensions-and-schemas.sql:104-120](../../../docker/postgresql/init/10-phase0-extensions-and-schemas.sql)
logs `Phase 0 init: X / 10 expected extensions, Y / 8 expected namespaces`
on first init.

### Connection paths

There are deliberately **three** ways services reach Postgres, picked per
caller’s transactional needs:

| Path | Used by | Why |
|------|---------|-----|
| `pgbouncer:6432` (transaction pooling, role `georag_app`) | laravel-octane runtime, laravel-horizon, laravel-reverb, fastapi runtime queries | High-volume short-lived queries; safe under asyncpg + transaction pooling |
| `postgresql:5432` (direct, role `georag` owner) | `MIGRATE_DB_CONNECTION=pgsql_migrations` from Laravel ([docker-compose.yml:538-547](../../../docker-compose.yml)) | DDL needs session state; phase0-owned tables need owner privileges without `GRANT georag TO georag_app` (would make runtime app superuser) |
| `postgresql:5432` (direct, role `georag` or `georag_app`) | hatchet-worker-* (`POSTGRES_DIRECT_HOST`), Dagster, FastAPI per-flow JWT loader, Martin | Transactions, `SET LOCAL`-style GUCs (workspace tenancy), or persistent connection model |

Connection counts: `max_connections=200`
([docker-compose.yml:234](../../../docker-compose.yml)). PgBouncer
(`edoburu/pgbouncer:1.25.1-p0`) is the gatekeeper for everything except
those listed above.

### Roles ([docker/postgresql/init/init-roles.sql](../../../docker/postgresql/init/init-roles.sql))

| Role | Login? | What it can do |
|------|--------|----------------|
| `georag` | Yes | **`SUPERUSER` + `rolbypassrls=true`** — verified at [database/raw/phase1/10-georag-app-role.sql:5](../../../database/raw/phase1/10-georag-app-role.sql). DDL/migrations only — never runtime traffic. ⚠️ See [security item §1.1](#11-known-security-issue--georag-role-is-superuser) below. |
| `georag_app` | Yes | Runtime application role for Laravel + FastAPI. `NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE`. RLS policies actually apply. Verified by the boot-time exception block at [database/raw/phase1/10-georag-app-role.sql:104-107](../../../database/raw/phase1/10-georag-app-role.sql) — startup refuses if either flag flips. |
| `georag_read` | No (group) | SELECT on `silver.*`, `bronze.*`, `public.*`, `audit.*` |
| `georag_write` | No (group, inherits `georag_read`) | + INSERT/UPDATE on `silver.*`, `public.*`, audit append |
| `georag_audit` | No | INSERT only on `audit.*` tables — the audit-trail writer pattern |
| `hatchet` | Yes | Owner of the dedicated `hatchet` logical DB ([docker/postgresql/init/20-hatchet-database.sql](../../../docker/postgresql/init/20-hatchet-database.sql)) |
| `kestra` | Yes | Owner of the dedicated `kestra` logical DB |
| `georag` (Dagster context) | Yes | Also owns `georag_dagster` logical DB |
| `martin_ro` | No (planned) | Read-only role for Martin. Currently the compose file still wires `georag_app` ([docker-compose.yml:812](../../../docker-compose.yml)). |
| `pgcrypto` key | (env) | `AUDIT_ENCRYPTION_KEY` — used by pgcrypto for per-sender HMAC registry and per-flow JWT key registry |

⚠️ Gotcha from memory ([project_init_roles_gap](../notes/INDEX.md#project_init_roles_gap)):
`init-roles.sql` is **not** inside the auto-init directory `/docker-entrypoint-initdb.d/`
on this build, so on a fresh cluster `georag_read/_write/_audit` must be
applied manually. Patch on the to-do list.

#### 1.1 Known security issue — `georag` role is `SUPERUSER`

**Severity: tracked, mitigated, not closed.**

The `georag` role is `LOGIN SUPERUSER` with `rolbypassrls=true`. Postgres
superusers always bypass RLS regardless of `FORCE ROW LEVEL SECURITY`.
Mitigation is operational, not structural:

1. Runtime traffic goes through `georag_app` (non-superuser, `NOBYPASSRLS`).
   Self-check at [phase1/10-georag-app-role.sql:104-107](../../../database/raw/phase1/10-georag-app-role.sql)
   refuses startup if `georag_app` ever acquires SUPERUSER/BYPASSRLS.
2. The only path that connects as `georag` is the dedicated
   `pgsql_migrations` Laravel connection ([docker-compose.yml:538-547](../../../docker-compose.yml)).
   Use it only for migrations.
3. Direct psql access to `georag` requires `POSTGRES_PASSWORD` from `.env`
   and is gated by network isolation (5432 is not exposed to the host).

Recommended production hardening (not yet implemented):
- Split `georag` into:
  - `georag_owner` — `NOSUPERUSER` but owns the schemas/tables (table
    ownership ≠ superuser; RLS still applies to non-owner roles).
  - `georag_migrator` — `NOSUPERUSER` with explicit DDL grants on schemas
    needed during migration. Never used at runtime.
- Bootstrap-only superuser (`postgres`) for one-time extension installs.
- Track this in the appendix C threat model (LLM-egress and tool-abuse
  surfaces care about ownership, not just login).

Until then: treat any process that has the `POSTGRES_PASSWORD` env as a
trust-boundary peer of the cluster.

#### 1.2 Martin DB role — `martin_ro` is **planned**, currently a security gap

The Martin tile server connects to Postgres as `georag_app`
([docker-compose.yml:812](../../../docker-compose.yml)). The intended
posture is a separate `martin_readonly` role (created at
[2026_04_22_130000_create_silver_mvt_functions.php:88](../../../database/migrations/2026_04_22_130000_create_silver_mvt_functions.php)
with `NOLOGIN NOINHERIT NOSUPERUSER`) with `SELECT` + `EXECUTE` on only
the MVT function/view surface.

**Status:** open security issue (not just a planning note). Tracked in
appendix C.

To close it:
1. Give `martin_readonly` a password + `LOGIN`.
2. Switch `docker-compose.yml:812` `DATABASE_URL` to use it.
3. Verify `\dn+` shows it has USAGE on only `silver`, `gold`, `public_geo`,
   `public` schemas and `EXECUTE` only on the MVT functions enumerated in
   [Ch 09 §2](09-martin-and-maplibre.md).
4. Add a startup self-check mirroring the `georag_app` block.

### Logical databases (same server)

| DB | Owner | Used by |
|----|-------|---------|
| `georag` | `georag` | The application. All `silver.*`, `gold.*`, `bronze.*`, `public.*`, `audit.*`, `usage.*`, `outbox.*`, `workflow.*`, `workspace.*`, `public_geo`/`public_geoscience`, `interpretation.*` namespaces. |
| `hatchet` | `hatchet` | Hatchet engine state — workflow runs, message queue (`SERVER_MSGQUEUE_KIND=postgres`) |
| `kestra` | `kestra` | Kestra repository + queue + flow storage |
| `georag_dagster` | `georag` | Dagster run history, event log |

### WAL archiving

`archive_mode=on`, `archive_timeout=300`, `archive_command` copies each
finished segment to `/var/lib/postgresql/wal_archive` on the
`pg_wal_archive` named volume
([docker-compose.yml:267-271](../../../docker-compose.yml)). The
`backup-agent` container ([docker-compose.yml:2831](../../../docker-compose.yml))
mounts that volume read-only and uploads to SeaweedFS every 5 minutes,
giving a ~10‑minute worst-case data-loss window.

---

## 2. Neo4j Community 2026.03 — geological knowledge graph

### What lives here

Per Hard Rule #9, **Community Edition only.** Used for:
- Entity nodes: `Project`, `DrillHole`, `Formation`, `RockUnit`, `MineralOccurrence`, `Citation`, `Document`, `Anomaly`, `Hypothesis`.
- Relationships: `(:DrillHole)-[:INTERSECTS]->(:Formation)`, `(:Document)-[:CITES]->(:Citation)`, `(:DrillHole)-[:NEAR]->(:DrillHole)`, etc.
- Used for graph traversal tools inside the agentic LangGraph (Ch 06) and
  by the `index_neo4j.py` Dagster asset
  ([src/dagster/georag_dagster/assets/index_neo4j.py](../../../src/dagster/georag_dagster/assets/index_neo4j.py)).

### Schema init + warmup

- One-time schema constraints + indices applied by `neo4j-warmup` from
  [docker/neo4j/init-schema.cypher](../../../docker/neo4j/init-schema.cypher).
- Page cache pre-warmed by [docker/neo4j/warmup.cypher](../../../docker/neo4j/warmup.cypher)
  (the workaround for missing Enterprise `db.memory.pagecache.warmup.enable`).
- Custom Neo4j config in [docker/neo4j/conf/neo4j.conf](../../../docker/neo4j/conf/neo4j.conf).

### Backup

[docker/neo4j/backup.sh](../../../docker/neo4j/backup.sh) is invoked by
Ofelia → uploaded by backup-agent.

### Driver pool sizing

Server accepts 5–50 Bolt threads
([docker-compose.yml:1157-1158](../../../docker-compose.yml)).
FastAPI’s async driver pool is sized 25 (matches asyncpg ceiling).

---

## 3. Qdrant 1.17 — vector index

### Collections

Created on FastAPI / hatchet-worker-ai startup via two asset paths:

| Collection | Created in | Vector dim | Distance | Quantisation |
|---|---|---|---|---|
| `public_geoscience` | [src/dagster/georag_dagster/assets/index_public_geoscience.py](../../../src/dagster/georag_dagster/assets/index_public_geoscience.py) (`_ensure_collection`) | bge-small dim (384) | Cosine | Scalar (set at create-time) |
| `reports` (silver.document_passages) | [src/dagster/georag_dagster/assets/index_reports.py](../../../src/dagster/georag_dagster/assets/index_reports.py) (`_ensure_collection`) | 384 | Cosine | Scalar |
| `splade_*` (sparse) | Embedder workflow | sparse | DOT | n/a |

> Quantisation is set per-collection, not at the cluster level
> ([docker-compose.yml:1281-1288](../../../docker-compose.yml)) — cluster-level
> defaults were removed because `client.create_collection(...)` without
> `quantization_config` was silently ignoring them.

### HNSW tuning (cluster default)

`m=32`, `ef_construct=256`, `ef=200`, `max_indexing_threads=4`
([docker-compose.yml:1268-1280](../../../docker-compose.yml)). WAL cap 256 MiB
per collection.

### Payload indexes

Created at collection-creation time on filter fields: `project_id`,
`workspace_id`, `document_type`, `source_chunk_id`. Lets the agentic
retriever do `workspace_id = ?` pre-filtering before HNSW search.

### Auth

Off in dev. **Required in production.** The empty-string trap is
documented inline at [docker-compose.yml:1295-1306](../../../docker-compose.yml):
setting `QDRANT__SERVICE__API_KEY=""` *enables* auth with empty-key
expectation and breaks every client that doesn’t pass `api-key:`.

**Deployment posture matrix:**

| Profile | API key | Network |
|---|---|---|
| `dev-light` / `dev-data` | unset (auth disabled) | Docker network isolation |
| `prod` / on-prem | **required** (`QDRANT_API_KEY` `:?` form) | Same network isolation **plus** TLS + auth |

`docker-compose.yml` should grow a profile-gated override that sets
`QDRANT__SERVICE__API_KEY=${QDRANT_API_KEY:?QDRANT_API_KEY must be set}`
under a `prod` profile. Until that ships, prod operators must apply it via
an override file or env. Tracked in appendix C.

---

## 4. Redis 8.6 — shared infrastructure

Single instance, four logical DBs
([docker-compose.yml:449-462](../../../docker-compose.yml)):

| DB | Used for |
|---|---|
| 0 | Laravel session store, queue jobs, Horizon supervisor state, cache |
| 1 | Future split — currently unused |
| 2 | Spare |
| 3 | Spare |

Async clients:
- Laravel: `redis.asyncio`-style is N/A — PHP uses the standard predis/phpredis driver.
- FastAPI: `redis.asyncio` (aioredis) — Hard Rule #2.
- Hatchet worker (`ai` pool): Used for per-sender external_notification token bucket ([docker-compose.yml:2209-2212](../../../docker-compose.yml)).

Persistence: AOF on (`appendonly=yes`, `appendfsync=everysec`), `save ""`
disabled. Stop grace 15 s.

---

## 5. SeaweedFS 4.20 — object storage (S3-compatible)

Replaces MinIO per [ADR-0001](../../adr/). Mounted in compose as the
`minio` service for backward compatibility of every consumer that hard-codes
the DNS alias `minio`.

### Endpoints

| Port | Protocol | Purpose |
|------|----------|---------|
| 8333 | S3 API | All `aioboto3` / Laravel filesystem / `mc` clients hit here |
| 8888 | HTTP filer | Admin/inspection |
| 9333 | Master | Cluster-internal — used by the `/cluster/status` healthcheck only |

### Buckets ([docker-compose.yml:1416-1430](../../../docker-compose.yml))

Created idempotently by the `minio-init` container at every `up`:

| Bucket | Purpose |
|---|---|
| `bronze` | Raw ingest archive (PDFs, LAS, SEG-Y, GeoTIFFs, GPKGs, XLSX, CSV) |
| `exports` | Generated reports/exports for download |
| `bronze-raster` | Raster archive — separate from `bronze` for lifecycle policies |
| `georag-backups` | Postgres dumps, Neo4j dumps, Qdrant snapshots, PG WAL segments |
| `tier-hot` / `tier-warm` / `tier-cold` | Storage Tiering Agent target buckets (Phase 0) |

### Object key conventions

| Object kind | Key pattern |
|---|---|
| Raw upload | `bronze/<workspace_id>/<project_id>/<sha256-prefix>/<original-filename>` |
| Bronze artifact | `bronze/<workspace_id>/<project_id>/<run_id>/<original-filename>` |
| OCR rendered page | `bronze-raster/<workspace_id>/<document_sha256>/page-<NNNN>.png` |
| Export | `exports/<workspace_id>/<export_id>/<filename>` |
| PG WAL | `georag-backups/postgres/wal/<segment-name>` |
| PG base backup | `georag-backups/postgres/base/<timestamp>.tar.zst` |

> Note: code references for these patterns live in
> [src/fastapi/app/services/bronze_store.py](../../../src/fastapi/app/services/bronze_store.py)
> (Bronze) and the various `backup_*` Hatchet workflows under
> [src/fastapi/app/hatchet_workflows/](../../../src/fastapi/app/hatchet_workflows/).

### Env aliasing

`S3_*` is canonical; `MINIO_*` and `AWS_*` are kept as aliases
([docker-compose.yml:939-954](../../../docker-compose.yml)) so the same
container can be used by Python aioboto3 (`AWS_*`), legacy MinIO clients
(`MINIO_*`), and ADR-0001-aware code (`S3_*`).

---

## 6. ClickHouse + (separate) Langfuse

Only started when `docker/compose.langfuse.yml` is applied
([docker/compose.langfuse.yml](../../../docker/compose.langfuse.yml)). Backs
the self-hosted Langfuse:

- `langfuse-web` on 3000 — UI, ingest, project keys.
- `langfuse-worker` — ingest worker (Redis + ClickHouse).
- `clickhouse` — 24.10-alpine. Holds traces/spans for LLM observability.

Wired into FastAPI/Laravel/Hatchet via `LANGFUSE_HOST`, `LANGFUSE_PUBLIC_KEY`,
`LANGFUSE_SECRET_KEY` (empty key disables the SDK at import time, see
[docker-compose.yml:602-608](../../../docker-compose.yml)).

---

## 7. Martin 1.7 — vector tile generator

Not a store, but it talks to Postgres directly. Covered in detail in
[Ch 09 — Martin + MapLibre](09-martin-and-maplibre.md). The key facts:

- `DATABASE_URL` ([docker-compose.yml:812](../../../docker-compose.yml)) goes
  straight to `postgresql:5432` (PgBouncer cannot keep Martin's persistent
  connections happy).
- Tile cache 512 MiB total → 256 MiB tiles + 64 MiB sprites + 64 MiB fonts
  ([docker/martin/martin.yaml:15-30](../../../docker/martin/martin.yaml)).
- Function sources (`schema: silver`) and table sources (`schema: public_geo`).

---

## 8. Hatchet engine state (Postgres)

The Hatchet Lite engine ([docker-compose.yml:1861](../../../docker-compose.yml))
keeps its own state in the `hatchet` logical DB. Provisioned by
[docker/postgresql/init/20-hatchet-database.sql](../../../docker/postgresql/init/20-hatchet-database.sql).
Laravel exposes a read-only view of that DB via the `pgsql_hatchet`
connection ([docker-compose.yml:548-556](../../../docker-compose.yml)) — used
by `HatchetWorkersController` to render the in-app Worker Dashboard at
`/admin/integrations/hatchet`.

---

## 9. Backup matrix

| Source | Tool | Schedule | Destination |
|--------|------|----------|-------------|
| Postgres base | `docker/postgresql/backup.sh` via Ofelia | daily | SeaweedFS `georag-backups/postgres/base/` |
| Postgres WAL | `docker/postgresql/wal-upload.sh` via `backup-agent` | every 5 min | SeaweedFS `georag-backups/postgres/wal/` |
| Neo4j | `docker/neo4j/backup.sh` via Ofelia | daily | SeaweedFS `georag-backups/neo4j/` |
| Qdrant | `docker/qdrant/backup.sh` via Ofelia | daily | SeaweedFS `georag-backups/qdrant/` |
| Redis | `app/hatchet_workflows/backup_redis.py` | daily | SeaweedFS `georag-backups/redis/` |
| SeaweedFS | `app/hatchet_workflows/backup_seaweedfs.py` | daily | (cross-region, not yet on dev) |

RTO/RPO: 10-min WAL replay window, full restore documented in
[docs/RUNBOOK.md](../../RUNBOOK.md).
