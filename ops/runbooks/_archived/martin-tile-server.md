# Martin tile server runbook

Architecture reference: §04d-tile, §07c-tile in `georag-architecture.html`.

Martin is a PostGIS-to-MVT tile server. It connects directly to PostgreSQL
(NOT PgBouncer) using persistent connections from its own pool
(`pool_size: 20` in `docker/martin/martin.yaml`). Configuration lives
entirely in `docker/martin/martin.yaml` — Martin has no database-backed
config.

---

## Changing Martin config (martin.yaml or alert rules)

### Why `docker compose restart martin` is WRONG on WSL2

Docker Desktop on WSL2 caches bind-mount inode references at container
start time. When you edit a bind-mounted file and run `docker compose restart
martin`, the container process continues reading the OLD inode content from
the kernel's page cache — the config change is silently ignored. You will see
the old cache_size_mb, worker_processes, or function list in the logs.

### Correct procedure for martin.yaml changes

After editing `docker/martin/martin.yaml`:

```bash
# 1. Hard-remove the running container (flushes the inode cache reference).
docker rm -f georag-martin

# 2. Re-create from compose (picks up the fresh file).
docker compose up -d martin

# 3. Wait ~6 seconds for the healthcheck, then verify the catalog loaded.
docker compose exec martin wget -qO- http://localhost:3000/catalog | head -c 2000
```

Do NOT use any of these — they will not apply the config change:
- `docker compose restart martin`
- `docker compose stop martin && docker compose start martin`
- `docker compose up -d martin` WITHOUT the `docker rm -f` step first

### Verify cache settings took effect

Martin logs its cache allocation on every startup. Confirm the tile cache
size is what you expect:

```bash
docker compose logs martin | grep -i cache
```

Expected output with `cache_size_mb: 512` in martin.yaml (the tile cache is
always `cache_size_mb / 2`):

```
Initializing PMTiles directory cache with maximum size 128 MB
Initializing tile cache with maximum size 256 MB
Initializing sprite cache with maximum size 64 MB
Initializing font cache with maximum size 64 MB
```

#### cache_size_mb division math

Martin 1.x splits `cache_size_mb` across four internal caches:

| Cache | Formula | At cache_size_mb=512 |
|---|---|---|
| PMTiles directory | cache_size_mb / 4 | 128 MB |
| Tile (MVT results) | cache_size_mb / 2 | 256 MB |
| Sprite | cache_size_mb / 8 | 64 MB |
| Font | cache_size_mb / 8 | 64 MB |

The MART-02 audit item pins `cache_size_mb: 512` to deliver 256 MB of tile
cache. Container memory limit is 512 MB. Total cache use is 512 MB.
The Martin process + PostgreSQL connection pool (~40 MB for pool_size=20)
fits in the remaining headroom because not all 512 MB is simultaneously
resident in memory (LRU eviction keeps the working set below the hard limit).

### Correct procedure for Prometheus alert rule changes

Alert rules live in `docker/prometheus/rules/martin-alerts.yml`. They are
hot-reloadable without restarting Prometheus:

```bash
docker compose exec prometheus wget -q --post-data='' -O- http://localhost:9090/-/reload
```

Note: Prometheus runs under the `dev-monitor` profile. If it is not currently
running (`docker compose ps` shows no prometheus container), the updated rule
file will be read automatically on next startup — no action needed.

### Known gotcha: Martin 1.5.0 has no /metrics endpoint

`GET /metrics` on Martin 1.5.0 returns HTTP 404. The `MartinLowCacheHitRate`
alert rules in `martin-alerts.yml` reference metric names
(`martin_tile_cache_hit_total`, `martin_tile_cache_miss_total`) that do not
yet exist. The rules are DORMANT and will never fire. They document the
intended thresholds for when Martin adds Prometheus support.

To verify whether a new Martin version added metrics support:

```bash
curl -v http://localhost:3002/metrics 2>&1 | grep "< HTTP"
# HTTP/1.1 404 = still no metrics
# HTTP/1.1 200 = metrics endpoint added — update alert rules accordingly
```

---

## martin_readonly role

Martin connects to PostgreSQL as the `georag` app user (via the
`DATABASE_URL` env var). The `martin_readonly` role exists for use by any
process that should have read-only tile access — it is NOT the active
connection role for Martin itself. Its purpose is:

1. A grant target so any future separate Martin user/connection can be
   `SET ROLE martin_readonly` or `GRANT martin_readonly TO <user>`.
2. A documentation artifact showing which tables and functions the tile
   functions need access to.

### What martin_readonly can access

`martin_readonly` has:
- EXECUTE on all 15 MVT functions (7 silver + 8 public_geoscience)
- SELECT on all silver and public_geoscience source tables (granted by
  migration `2026_04_22_150000_grant_martin_readonly_select.php`)
- USAGE on `silver` and `public_geoscience` schemas
- ALTER DEFAULT PRIVILEGES on both schemas (future tables auto-granted)

To audit the current grant inventory:

```sql
-- Table SELECT grants
SELECT table_schema, table_name
FROM information_schema.table_privileges
WHERE grantee = 'martin_readonly'
ORDER BY table_schema, table_name;

-- Schema USAGE
SELECT nspname, has_schema_privilege('martin_readonly', nspname, 'USAGE')
FROM pg_namespace
WHERE nspname IN ('silver','public_geoscience');
```

### Smoke-testing functions as martin_readonly

```sql
-- Connect to the georag DB, then:
SET ROLE martin_readonly;

-- Should return an integer (bytes), not a permission error:
SELECT octet_length(mvt)
FROM silver.pg_collars_by_project(
    1, 0, 0,
    json_build_object('project_id','019d74a1-fba8-7165-9ae6-a5bf93eef97d'::text)
);

-- Empty table = 0 bytes; permission error = ERROR message. 0 bytes is correct.
SELECT octet_length(mvt)
FROM silver.pg_boundaries_by_project(
    1, 0, 0,
    json_build_object('project_id','019d74a1-fba8-7165-9ae6-a5bf93eef97d'::text)
);

RESET ROLE;
```

---

## Diagnosing tile errors in Martin logs

Martin logs tile errors at ERROR level:

```
ERROR martin::srv::server: Unable to get tile 2/0/0 with ... params from <source>: db error
```

Common causes:

| Symptom | Likely cause | Fix |
|---|---|---|
| `db error` on ALL silver function sources | `martin_readonly` missing SCHEMA USAGE | `GRANT USAGE ON SCHEMA silver TO martin_readonly;` |
| `db error` on specific function | Missing SELECT on a source table | Run grant migration or add GRANT inline |
| `db error` on ALL PGEO function sources | `martin_readonly` missing SELECT on `public_geoscience.jurisdictions` | Run `2026_04_22_150000_grant_martin_readonly_select.php` |
| `RAISE EXCEPTION` text in db error | Function stub not yet replaced | Chunk 8.2b migration hasn't run; check `php artisan migrate:status` |
| Martin starts but shows wrong sources | Stale bind-mount cache | Use `docker rm -f georag-martin && docker compose up -d martin` |
