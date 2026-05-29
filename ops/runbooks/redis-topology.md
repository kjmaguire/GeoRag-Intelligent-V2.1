# GeoRAG Redis Topology — Dev vs Prod Plan
<!-- Produced by: devops-engineer agent (Claude Sonnet 4.6) -->
<!-- Date: 2026-04-19 (Module 2 Phase B, Item B6) -->
<!-- Authority: 02-data-stores-hardening.md §B6 -->

## Current State (dev — single instance)

One Redis instance (`georag-redis`) handles all three traffic types:

| Database | Use | Key patterns |
|---|---|---|
| db0 | Cache + Sessions + Horizon queues | `laravel_*`, `horizon:*`, `pulse:*`, `georag:*` |

**Config:**
- `maxmemory: 512mb`
- `maxmemory-policy: allkeys-lru`
- `appendonly: yes` / `appendfsync: everysec`
- `save: ""` (RDB disabled — AOF only)

This is intentional for dev: a single instance keeps the memory footprint low on the
developer workstation (idle: ~6 MiB / 1 GiB limit).

**Known issue (RDS-01):** AOF has grown to ~40 MiB with only 6 active keys due to high
write churn from Pulse aggregates and Dagster heartbeats (~1.1M write ops since last save).
This is normal. The AOF rewrite threshold is 100% growth over the base RDB size, so the
next auto-rewrite will fire at ~80 MiB. To compact manually:

```bash
docker exec georag-redis redis-cli -a "$REDIS_PASSWORD" BGREWRITEAOF
```

---

## Prod Design — 3-Instance Separation (Phase D, before staging deployment)

The prod compose profile must add three separated Redis instances. Do NOT implement in
dev — this section is a design artifact for when the prod profile is built.

### Instance 1 — redis-cache

**Role:** Application cache (RAG results, geocoding, tile metadata)

```yaml
redis-cache:
  image: redis:8.6.2-alpine@<digest>
  container_name: georag-redis-cache
  command: >
    redis-server
    --requirepass ${REDIS_CACHE_PASSWORD}
    --maxmemory 2gb
    --maxmemory-policy allkeys-lru
    --appendonly no
    --save ""
    --databases 2
    --tcp-keepalive 300
    --timeout 600
    --lazyfree-lazy-eviction yes
    --lazyfree-lazy-expire yes
  profiles: ["prod", "staging"]
  deploy:
    resources:
      limits:
        memory: 2560M
```

**Persistence:** NONE (cache is ephemeral). `appendonly no`, `save ""`.
**Eviction:** `allkeys-lru` — oldest cache entries evicted when full. Correct for a cache.
**Laravel config:** `CACHE_STORE=redis` pointing to this instance.

---

### Instance 2 — redis-queue

**Role:** Laravel Horizon job queues

```yaml
redis-queue:
  image: redis:8.6.2-alpine@<digest>
  container_name: georag-redis-queue
  command: >
    redis-server
    --requirepass ${REDIS_QUEUE_PASSWORD}
    --maxmemory 1gb
    --maxmemory-policy noeviction
    --appendonly yes
    --appendfsync everysec
    --auto-aof-rewrite-percentage 50
    --auto-aof-rewrite-min-size 32mb
    --save ""
    --databases 2
    --tcp-keepalive 300
    --timeout 0
    --lazyfree-lazy-eviction no
    --lazyfree-lazy-expire no
  profiles: ["prod", "staging"]
  deploy:
    resources:
      limits:
        memory: 1280M
```

**Persistence:** AOF `everysec` — job data must survive a restart. `save ""` disables RDB
(AOF is sufficient; RDB and AOF together create unnecessary I/O on NVMe).
**Eviction:** `noeviction` — Horizon jobs MUST NOT be silently dropped. If the queue fills,
Laravel receives a write error rather than a silent eviction.
**Auto-rewrite:** Tuned aggressive (50% growth, 32 MiB min) to prevent AOF bloat on
high-churn queue workloads (same pattern seen in dev with single instance).
**Laravel config:** `QUEUE_CONNECTION=redis` pointing to this instance.

---

### Instance 3 — redis-sessions

**Role:** Laravel session storage

```yaml
redis-sessions:
  image: redis:8.6.2-alpine@<digest>
  container_name: georag-redis-sessions
  command: >
    redis-server
    --requirepass ${REDIS_SESSION_PASSWORD}
    --maxmemory 512mb
    --maxmemory-policy volatile-lru
    --appendonly yes
    --appendfsync everysec
    --save ""
    --databases 2
    --tcp-keepalive 300
    --timeout 1800
  profiles: ["prod", "staging"]
  deploy:
    resources:
      limits:
        memory: 640M
```

**Persistence:** AOF `everysec` — session loss on restart is a bad user experience.
**Eviction:** `volatile-lru` — only evict keys WITH an expiry (sessions have TTLs).
**Timeout:** 1800s idle timeout aligns with typical session lifetime.
**Laravel config:** `SESSION_DRIVER=redis` pointing to this instance.

---

## Environment Variables Required for Prod

Add to `.env.prod` (do NOT add to `.env` which is dev-only):

```dotenv
# Redis — prod 3-instance topology
REDIS_CACHE_HOST=redis-cache
REDIS_CACHE_PORT=6379
REDIS_CACHE_PASSWORD=<generate-strong-password>

REDIS_QUEUE_HOST=redis-queue
REDIS_QUEUE_PORT=6379
REDIS_QUEUE_PASSWORD=<generate-strong-password>

REDIS_SESSION_HOST=redis-sessions
REDIS_SESSION_PORT=6379
REDIS_SESSION_PASSWORD=<generate-strong-password>
```

## Laravel config/database.php Changes (for prod profile)

The `redis` connection block in `config/database.php` must be updated to reference
the three separate instances when `APP_ENV=production`. The `default` connection
(single instance) remains for dev. Use environment variable switching:

```php
'redis' => [
    'client' => env('REDIS_CLIENT', 'phpredis'),
    'default' => [
        'host'     => env('REDIS_HOST', '127.0.0.1'),
        'password' => env('REDIS_PASSWORD', null),
        'port'     => env('REDIS_PORT', 6379),
        'database' => env('REDIS_DB', 0),
    ],
    'cache' => [
        'host'     => env('REDIS_CACHE_HOST', env('REDIS_HOST', '127.0.0.1')),
        'password' => env('REDIS_CACHE_PASSWORD', env('REDIS_PASSWORD', null)),
        'port'     => env('REDIS_CACHE_PORT', env('REDIS_PORT', 6379)),
        'database' => env('REDIS_CACHE_DB', 1),
    ],
    'queue' => [
        'host'     => env('REDIS_QUEUE_HOST', env('REDIS_HOST', '127.0.0.1')),
        'password' => env('REDIS_QUEUE_PASSWORD', env('REDIS_PASSWORD', null)),
        'port'     => env('REDIS_QUEUE_PORT', env('REDIS_PORT', 6379)),
        'database' => env('REDIS_QUEUE_DB', 0),
    ],
    'sessions' => [
        'host'     => env('REDIS_SESSION_HOST', env('REDIS_HOST', '127.0.0.1')),
        'password' => env('REDIS_SESSION_PASSWORD', env('REDIS_PASSWORD', null)),
        'port'     => env('REDIS_SESSION_PORT', env('REDIS_PORT', 6379)),
        'database' => env('REDIS_SESSION_DB', 0),
    ],
],
```

When `REDIS_CACHE_HOST`, `REDIS_QUEUE_HOST`, and `REDIS_SESSION_HOST` are unset (dev),
all three fall back to the single `REDIS_HOST` instance. No dev code changes needed.

## Total Prod Memory Budget

| Instance | Limit | Idle est. |
|---|---|---|
| redis-cache | 2 GiB | ~50 MiB |
| redis-queue | 1 GiB | ~10 MiB |
| redis-sessions | 512 MiB | ~5 MiB |
| **Total** | **3.5 GiB** | **~65 MiB idle** |

This fits the 64 GiB workstation budget alongside Neo4j (9 GiB), PostgreSQL (24-32 GiB
after B1 memory upgrade), FastAPI, and Laravel services.

---

## Implementation companion

The executable rollout — compose YAML, env templates, Laravel config diffs,
Prometheus scrape jobs, smoke tests, rollback — lives at
**`ops/runbooks/redis-3-instance-rollout.md`** (drafted 2026-05-07). Apply
that document when the team is ready to stand up staging. Do not partially
apply it; the procedure assumes all-or-nothing cutover within a single
maintenance window.

---

*Dev single-instance posture is intentional and correct. Do not spin up a second Redis
in dev without Kyle approval — workstation memory headroom is managed per Section 11.*
