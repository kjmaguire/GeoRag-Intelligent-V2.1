# Redis 3-Instance Topology — Staging/Prod Rollout

**Status:** Implementation runbook (companion to `redis-topology.md`).
**Owner:** devops-engineer.
**Activates on:** before staging deployment (Phase D).
**Reverts to:** single-instance dev (the current state).

This document is the executable counterpart to `redis-topology.md`. The topology
runbook explains *why* three instances; this one is the actual *how*: compose
services, env, Laravel config, Prometheus, rollout, smoke tests, rollback.

The single-instance dev posture is **intentional and stays as-is** until the
team is ready to stand up staging. Apply this rollout only against the
`staging` and `prod` compose profiles.

---

## 1. Architecture overview

| Instance | Memory | Eviction | Persistence | Backs |
|---|---|---|---|---|
| `redis-cache` | 2 GiB | `allkeys-lru` | None (AOF off, RDB off) | Application cache (Laravel `Cache::`, FastAPI agent cache) |
| `redis-queue` | 1 GiB | **`noeviction`** | AOF `everysec`, aggressive rewrite | Horizon job queues, scheduler state |
| `redis-sessions` | 512 MiB | `volatile-lru` | AOF `everysec` | Sanctum sessions, Reverb scaling pub/sub |

Total prod budget: **3.5 GiB** (matches §06b memory plan).

Each instance runs its own `redis_exporter` sidecar so Prometheus can label
metrics per role (cache vs queue vs sessions).

---

## 2. Pre-flight checks

Before applying:

```bash
# Confirm current state is single-instance dev (the only valid starting point).
docker ps --filter name=georag-redis --format '{{.Names}} {{.Status}}'
# Expected:
#   georag-redis  Up X minutes (healthy)

# Confirm no staging redis containers exist yet.
docker ps -a --filter 'name=georag-redis-' --format '{{.Names}}'
# Expected: empty output.

# Confirm Prometheus alert rules already cover Redis (they do — see
# docker/prometheus/rules/redis-alerts.yml). The same 5 rules apply per
# instance once the scrape config lists all three.
grep -c "redis_up\|redis_memory_used_bytes" docker/prometheus/rules/redis-alerts.yml
# Expected: at least 5
```

---

## 3. Compose service definitions

**Implementation:** the YAML for all six new services (3 Redis + 3 exporters)
plus the two new volumes ships as a Compose overlay at
[`docker/compose.redis-staging.yml`](../../docker/compose.redis-staging.yml).
The overlay is applied alongside the canonical compose with
`-f compose.yml -f docker/compose.redis-staging.yml --profile staging`. All
six services are gated by `profiles: [staging, prod]` so they are inert
under any `dev-*` profile — the dev `redis` instance is unaffected.

The sections below describe each service's role and configuration intent.
Refer to the overlay file for the exact YAML.

### 3.1 `redis-cache`

```yaml
  redis-cache:
    profiles:
      - staging
      - prod
    command:
      - redis-server
      - --requirepass
      - ${REDIS_CACHE_PASSWORD:?REDIS_CACHE_PASSWORD must be set}
      - --maxmemory
      - 2gb
      - --maxmemory-policy
      - allkeys-lru
      - --appendonly
      - "no"
      - --save
      - ""
      - --databases
      - "2"
      - --tcp-backlog
      - "511"
      - --timeout
      - "600"
      - --tcp-keepalive
      - "300"
      - --slowlog-log-slower-than
      - "1000"
      - --slowlog-max-len
      - "256"
      - --lazyfree-lazy-eviction
      - "yes"
      - --lazyfree-lazy-expire
      - "yes"
      - --lazyfree-lazy-server-del
      - "yes"
      - --lazyfree-lazy-user-del
      - "yes"
      - --lazyfree-lazy-user-flush
      - "yes"
      - --loglevel
      - notice
    container_name: georag-redis-cache
    deploy:
      resources:
        limits:
          cpus: 1
          memory: "2684354560"  # 2560 MiB — 2 GiB maxmemory + 512 MiB headroom
        reservations:
          memory: "268435456"   # 256 MiB
    healthcheck:
      test:
        - CMD-SHELL
        - redis-cli -a "$$REDIS_CACHE_PASSWORD" ping | grep PONG
      timeout: 5s
      interval: 10s
      retries: 5
      start_period: 10s
    image: redis:8.6.2-alpine
    networks:
      georag: null
    restart: unless-stopped
    # No volume — cache is intentionally ephemeral. Data loss on restart is
    # by design; consumers must tolerate cold-cache misses on startup.
```

### 3.2 `redis-queue`

```yaml
  redis-queue:
    profiles:
      - staging
      - prod
    command:
      - redis-server
      - --requirepass
      - ${REDIS_QUEUE_PASSWORD:?REDIS_QUEUE_PASSWORD must be set}
      - --maxmemory
      - 1gb
      - --maxmemory-policy
      - noeviction
      - --appendonly
      - "yes"
      - --appendfsync
      - everysec
      - --auto-aof-rewrite-percentage
      - "50"
      - --auto-aof-rewrite-min-size
      - 32mb
      - --save
      - ""
      - --databases
      - "2"
      - --tcp-backlog
      - "511"
      - --timeout
      - "0"
      - --tcp-keepalive
      - "300"
      - --slowlog-log-slower-than
      - "1000"
      - --slowlog-max-len
      - "256"
      - --lazyfree-lazy-eviction
      - "no"
      - --lazyfree-lazy-expire
      - "no"
      - --loglevel
      - notice
    container_name: georag-redis-queue
    deploy:
      resources:
        limits:
          cpus: 1
          memory: "1342177280"  # 1280 MiB — 1 GiB maxmemory + 256 MiB headroom
        reservations:
          memory: "268435456"   # 256 MiB
    healthcheck:
      test:
        - CMD-SHELL
        - redis-cli -a "$$REDIS_QUEUE_PASSWORD" ping | grep PONG
      timeout: 5s
      interval: 10s
      retries: 5
      start_period: 10s
    image: redis:8.6.2-alpine
    networks:
      georag: null
    restart: unless-stopped
    volumes:
      - type: volume
        source: redis_queue_data
        target: /data
        volume: {}
```

### 3.3 `redis-sessions`

```yaml
  redis-sessions:
    profiles:
      - staging
      - prod
    command:
      - redis-server
      - --requirepass
      - ${REDIS_SESSION_PASSWORD:?REDIS_SESSION_PASSWORD must be set}
      - --maxmemory
      - 512mb
      - --maxmemory-policy
      - volatile-lru
      - --appendonly
      - "yes"
      - --appendfsync
      - everysec
      - --save
      - ""
      - --databases
      - "2"
      - --tcp-backlog
      - "511"
      - --timeout
      - "1800"
      - --tcp-keepalive
      - "300"
      - --slowlog-log-slower-than
      - "1000"
      - --slowlog-max-len
      - "256"
      - --loglevel
      - notice
    container_name: georag-redis-sessions
    deploy:
      resources:
        limits:
          cpus: 1
          memory: "671088640"  # 640 MiB — 512 MiB maxmemory + 128 MiB headroom
        reservations:
          memory: "134217728"  # 128 MiB
    healthcheck:
      test:
        - CMD-SHELL
        - redis-cli -a "$$REDIS_SESSION_PASSWORD" ping | grep PONG
      timeout: 5s
      interval: 10s
      retries: 5
      start_period: 10s
    image: redis:8.6.2-alpine
    networks:
      georag: null
    restart: unless-stopped
    volumes:
      - type: volume
        source: redis_sessions_data
        target: /data
        volume: {}
```

### 3.4 Three `redis_exporter` sidecars

Prometheus already expects redis at `redis_exporter:9121`. For the 3-instance
topology, run one exporter per instance on distinct ports so Prometheus can
label per-role.

```yaml
  redis_exporter_cache:
    profiles:
      - staging
      - prod
    image: oliver006/redis_exporter:v1.74.0-alpine
    container_name: georag-redis-exporter-cache
    command:
      - --redis.addr=redis://redis-cache:6379
      - --redis.password=${REDIS_CACHE_PASSWORD}
      - --web.listen-address=:9121
    depends_on:
      redis-cache:
        condition: service_healthy
    networks:
      georag: null
    restart: unless-stopped

  redis_exporter_queue:
    profiles:
      - staging
      - prod
    image: oliver006/redis_exporter:v1.74.0-alpine
    container_name: georag-redis-exporter-queue
    command:
      - --redis.addr=redis://redis-queue:6379
      - --redis.password=${REDIS_QUEUE_PASSWORD}
      - --web.listen-address=:9122
    depends_on:
      redis-queue:
        condition: service_healthy
    networks:
      georag: null
    restart: unless-stopped

  redis_exporter_sessions:
    profiles:
      - staging
      - prod
    image: oliver006/redis_exporter:v1.74.0-alpine
    container_name: georag-redis-exporter-sessions
    command:
      - --redis.addr=redis://redis-sessions:6379
      - --redis.password=${REDIS_SESSION_PASSWORD}
      - --web.listen-address=:9123
    depends_on:
      redis-sessions:
        condition: service_healthy
    networks:
      georag: null
    restart: unless-stopped
```

### 3.5 Volumes

Add to top-level `volumes:` block:

```yaml
volumes:
  redis_data:                      # existing — stays for dev profile
    name: georagintelligencev10_redis_data
    driver: local

  redis_queue_data:                # NEW
    name: georagintelligencev10_redis_queue_data
    driver: local
    # AOF lives here. Back up to SeaweedFS via the backup-restore.md procedure.

  redis_sessions_data:             # NEW
    name: georagintelligencev10_redis_sessions_data
    driver: local
    # AOF lives here. Less critical than queue but session loss = forced re-login.
```

`redis-cache` is volume-less by design (ephemeral).

---

## 4. Environment variables

### 4.1 `.env.staging` template (NEW file)

```dotenv
# =============================================================================
# REDIS — STAGING/PROD 3-INSTANCE TOPOLOGY
# Used by:    redis-cache, redis-queue, redis-sessions
# Profile:    staging, prod
# Authority:  ops/runbooks/redis-topology.md
# =============================================================================

# --- redis-cache (allkeys-lru, ephemeral, 2 GiB) -----------------------------
REDIS_CACHE_HOST=redis-cache
REDIS_CACHE_PORT=6379
REDIS_CACHE_PASSWORD=__CHANGEME_generate_with_openssl_rand_hex_32__
REDIS_CACHE_DB=0

# --- redis-queue (noeviction, AOF, 1 GiB) -----------------------------------
# CRITICAL: noeviction means the queue never silently drops jobs under
# memory pressure. Laravel will receive a write error instead.
REDIS_QUEUE_HOST=redis-queue
REDIS_QUEUE_PORT=6379
REDIS_QUEUE_PASSWORD=__CHANGEME_generate_with_openssl_rand_hex_32__
REDIS_QUEUE_DB=0

# --- redis-sessions (volatile-lru, AOF, 512 MiB) ----------------------------
REDIS_SESSION_HOST=redis-sessions
REDIS_SESSION_PORT=6379
REDIS_SESSION_PASSWORD=__CHANGEME_generate_with_openssl_rand_hex_32__
REDIS_SESSION_DB=0

# --- Laravel routing — point each subsystem at its dedicated instance -------
CACHE_STORE=redis
SESSION_DRIVER=redis
SESSION_CONNECTION=sessions
QUEUE_CONNECTION=redis
REDIS_QUEUE_CONNECTION=queue
REDIS_CACHE_CONNECTION=cache

# --- Reverb scaling — uses the cache instance's pub/sub by design -----------
# The cache instance is fine for Reverb's ephemeral pub/sub channel state;
# pub/sub keys do not consume `maxmemory` so allkeys-lru is irrelevant here.
REVERB_SCALING_ENABLED=true
REVERB_SCALING_CHANNEL=reverb
# Reverb scaling reads REDIS_HOST/REDIS_PORT/REDIS_PASSWORD by default. Point
# these at the cache instance for the staging profile.
REDIS_HOST=redis-cache
REDIS_PORT=6379
REDIS_PASSWORD=${REDIS_CACHE_PASSWORD}

# Generate each password with: openssl rand -hex 32
# Each must be DIFFERENT — sharing them defeats the per-instance ACL boundary.
```

### 4.2 `.env.example` additions (for documentation)

Add to the existing Redis block in `.env.example`:

```dotenv
# ── Prod 3-instance topology (active when staging/prod profile is up) ──
# Leave these unset for dev; Laravel falls back to REDIS_HOST/REDIS_PASSWORD.
# When set, they take precedence and route cache/queue/sessions to dedicated
# instances. See ops/runbooks/redis-topology.md and redis-3-instance-rollout.md.
# REDIS_CACHE_HOST=redis-cache
# REDIS_CACHE_PASSWORD=
# REDIS_QUEUE_HOST=redis-queue
# REDIS_QUEUE_PASSWORD=
# REDIS_SESSION_HOST=redis-sessions
# REDIS_SESSION_PASSWORD=
# SESSION_CONNECTION=sessions
# REDIS_QUEUE_CONNECTION=queue
# REDIS_CACHE_CONNECTION=cache
```

---

## 5. Laravel configuration changes

### 5.1 `config/database.php` — replace the existing `redis` block

```php
'redis' => [
    'client' => env('REDIS_CLIENT', 'phpredis'),

    'options' => [
        'cluster' => env('REDIS_CLUSTER', 'redis'),
        'prefix'  => env('REDIS_PREFIX', Str::slug((string) env('APP_NAME', 'laravel')) . '-database-'),
        'persistent' => env('REDIS_PERSISTENT', false),
    ],

    // Default — single instance in dev. Falls through to per-role envs in
    // staging/prod.
    'default' => [
        'url'      => env('REDIS_URL'),
        'host'     => env('REDIS_HOST', '127.0.0.1'),
        'username' => env('REDIS_USERNAME'),
        'password' => env('REDIS_PASSWORD'),
        'port'     => env('REDIS_PORT', '6379'),
        'database' => env('REDIS_DB', '0'),
        'max_retries'       => env('REDIS_MAX_RETRIES', 3),
        'backoff_algorithm' => env('REDIS_BACKOFF_ALGORITHM', 'decorrelated_jitter'),
        'backoff_base'      => env('REDIS_BACKOFF_BASE', 100),
        'backoff_cap'       => env('REDIS_BACKOFF_CAP', 1000),
    ],

    // Cache — falls back to `default` when REDIS_CACHE_HOST is unset (dev).
    'cache' => [
        'url'      => env('REDIS_URL'),
        'host'     => env('REDIS_CACHE_HOST', env('REDIS_HOST', '127.0.0.1')),
        'username' => env('REDIS_CACHE_USERNAME', env('REDIS_USERNAME')),
        'password' => env('REDIS_CACHE_PASSWORD', env('REDIS_PASSWORD')),
        'port'     => env('REDIS_CACHE_PORT', env('REDIS_PORT', '6379')),
        'database' => env('REDIS_CACHE_DB', '1'),
        'max_retries'       => env('REDIS_MAX_RETRIES', 3),
        'backoff_algorithm' => env('REDIS_BACKOFF_ALGORITHM', 'decorrelated_jitter'),
        'backoff_base'      => env('REDIS_BACKOFF_BASE', 100),
        'backoff_cap'       => env('REDIS_BACKOFF_CAP', 1000),
    ],

    // Queue — NEW connection. Routes Horizon jobs to redis-queue in prod,
    // falls back to default Redis in dev.
    'queue' => [
        'host'     => env('REDIS_QUEUE_HOST', env('REDIS_HOST', '127.0.0.1')),
        'username' => env('REDIS_QUEUE_USERNAME', env('REDIS_USERNAME')),
        'password' => env('REDIS_QUEUE_PASSWORD', env('REDIS_PASSWORD')),
        'port'     => env('REDIS_QUEUE_PORT', env('REDIS_PORT', '6379')),
        'database' => env('REDIS_QUEUE_DB', '0'),
        'max_retries'       => env('REDIS_MAX_RETRIES', 3),
        'backoff_algorithm' => env('REDIS_BACKOFF_ALGORITHM', 'decorrelated_jitter'),
        'backoff_base'      => env('REDIS_BACKOFF_BASE', 100),
        'backoff_cap'       => env('REDIS_BACKOFF_CAP', 1000),
    ],

    // Sessions — NEW connection. Routes Sanctum sessions to redis-sessions
    // in prod, falls back to default Redis in dev.
    'sessions' => [
        'host'     => env('REDIS_SESSION_HOST', env('REDIS_HOST', '127.0.0.1')),
        'username' => env('REDIS_SESSION_USERNAME', env('REDIS_USERNAME')),
        'password' => env('REDIS_SESSION_PASSWORD', env('REDIS_PASSWORD')),
        'port'     => env('REDIS_SESSION_PORT', env('REDIS_PORT', '6379')),
        'database' => env('REDIS_SESSION_DB', '0'),
        'max_retries'       => env('REDIS_MAX_RETRIES', 3),
        'backoff_algorithm' => env('REDIS_BACKOFF_ALGORITHM', 'decorrelated_jitter'),
        'backoff_base'      => env('REDIS_BACKOFF_BASE', 100),
        'backoff_cap'       => env('REDIS_BACKOFF_CAP', 1000),
    ],
],
```

### 5.2 `config/queue.php` — already env-overridable

The `redis` queue connection block already reads
`REDIS_QUEUE_CONNECTION` (default `'default'`). No code change needed —
just set `REDIS_QUEUE_CONNECTION=queue` in `.env.staging` (already in §4.1
above).

### 5.3 `config/cache.php` — already env-overridable

The `redis` cache store reads `REDIS_CACHE_CONNECTION` (default `'cache'`).
No code change. Set `REDIS_CACHE_CONNECTION=cache` (or leave default) in
`.env.staging`.

### 5.4 `config/session.php` — already env-overridable

The session connection reads `SESSION_CONNECTION`. No code change. Set
`SESSION_CONNECTION=sessions` in `.env.staging`.

### 5.5 `config/horizon.php` — point environments at the queue connection

Horizon's supervisor blocks currently say `'connection' => 'redis'` which
resolves to Laravel's default Redis connection. In the staging/prod profile
that needs to be `'connection' => env('HORIZON_REDIS_CONNECTION', 'redis')`,
with `HORIZON_REDIS_CONNECTION=queue` set in `.env.staging`.

```php
// config/horizon.php — for both supervisor-1 and supervisor-llm blocks
'supervisor-1' => [
-   'connection' => 'redis',
+   'connection' => env('HORIZON_REDIS_CONNECTION', 'redis'),
    'queue' => ['default'],
    // ...
],
'supervisor-llm' => [
-   'connection' => 'redis',
+   'connection' => env('HORIZON_REDIS_CONNECTION', 'redis'),
    'queue' => ['llm'],
    // ...
],
```

Then in `.env.staging`:

```dotenv
HORIZON_REDIS_CONNECTION=queue
```

In dev, the env is unset and Horizon falls back to the default `redis`
connection — no behaviour change.

---

## 6. Prometheus scrape configuration

Replace the existing single `redis` job in `docker/prometheus/prometheus.yml`
with three jobs (one per exporter):

```yaml
  - job_name: "redis_cache"
    scrape_interval: 15s
    static_configs:
      - targets: ["redis_exporter_cache:9121"]
        labels:
          service: "redis"
          role: "cache"

  - job_name: "redis_queue"
    scrape_interval: 15s
    static_configs:
      - targets: ["redis_exporter_queue:9122"]
        labels:
          service: "redis"
          role: "queue"

  - job_name: "redis_sessions"
    scrape_interval: 15s
    static_configs:
      - targets: ["redis_exporter_sessions:9123"]
        labels:
          service: "redis"
          role: "sessions"
```

The five existing alert rules in `docker/prometheus/rules/redis-alerts.yml`
fire per-instance automatically because Prometheus labels by `service` +
`role`. Alerts will identify which Redis is the offender via the `role`
label in the alert payload.

**Recommended addition** — a queue-specific alert that's NOT in the current
ruleset:

```yaml
- alert: RedisQueueEvictionAttempt
  expr: increase(redis_evicted_keys_total{role="queue"}[5m]) > 0
  for: 1m
  labels:
    severity: critical
    service: redis
    role: queue
  annotations:
    summary: "Eviction attempted on noeviction queue Redis — Laravel writes failing"
    description: "redis-queue is configured noeviction, so any redis_evicted_keys_total increase means a misconfiguration drift. Investigate maxmemory-policy."
```

---

## 7. Rollout procedure

Execute against the `staging` profile first. Do not skip steps.

```bash
# 0. Snapshot current dev state (so rollback is trivial).
cp .env .env.dev.backup
docker exec georag-redis redis-cli -a "$REDIS_PASSWORD" BGREWRITEAOF
docker cp georag-redis:/data/appendonlydir /tmp/redis-aof-snapshot-$(date +%Y%m%d)

# 1. Generate the three passwords. Store in your secret manager.
openssl rand -hex 32   # → REDIS_CACHE_PASSWORD
openssl rand -hex 32   # → REDIS_QUEUE_PASSWORD
openssl rand -hex 32   # → REDIS_SESSION_PASSWORD

# 2. Create .env.staging from the §4.1 template and substitute the three
#    passwords. Verify all three are different (sharing them defeats per-
#    instance ACL).
diff <(grep PASSWORD .env.staging | sort) <(grep PASSWORD .env.staging | sort -u)
# Expected: empty diff (each password is unique).

# 3. Apply the config/database.php and config/horizon.php edits from §5.

# 4. Apply the compose service definitions from §3.

# 5. Bring up the staging profile WITHOUT stopping dev redis yet.
#    This lets you smoke-test the new instances in parallel.
#    Note the explicit `-f` overlay — the redis-staging services live in
#    docker/compose.redis-staging.yml, not the canonical compose.
docker compose \
    -f compose.yml \
    -f docker/compose.redis-staging.yml \
    --profile staging \
    --env-file .env.staging \
    up -d \
    redis-cache redis-queue redis-sessions \
    redis_exporter_cache redis_exporter_queue redis_exporter_sessions

# 6. Verify all three are healthy.
docker compose ps redis-cache redis-queue redis-sessions
# Expected: all three "Up X seconds (healthy)"

# 7. Verify each instance accepts its own password and rejects others.
docker exec georag-redis-cache redis-cli -a "$REDIS_CACHE_PASSWORD" PING
docker exec georag-redis-queue redis-cli -a "$REDIS_QUEUE_PASSWORD" PING
docker exec georag-redis-sessions redis-cli -a "$REDIS_SESSION_PASSWORD" PING
# All three: PONG

# 8. Verify maxmemory-policy is correct on each.
for r in redis-cache:CACHE redis-queue:QUEUE redis-sessions:SESSION; do
  name="${r%%:*}"; pwvar="REDIS_${r##*:}_PASSWORD"
  echo "--- $name ---"
  docker exec "georag-$name" redis-cli -a "${!pwvar}" CONFIG GET maxmemory-policy
done
# Expected:
#   redis-cache    → allkeys-lru
#   redis-queue    → noeviction
#   redis-sessions → volatile-lru

# 9. Verify Prometheus is scraping all three.
curl -s 'http://localhost:9090/api/v1/targets?state=active' \
  | jq -r '.data.activeTargets[] | select(.labels.service == "redis") | "\(.labels.role) \(.health)"'
# Expected:
#   cache up
#   queue up
#   sessions up

# 10. Restart Laravel services so they pick up the new env / config.
docker compose \
    -f compose.yml \
    -f docker/compose.redis-staging.yml \
    --profile staging \
    --env-file .env.staging \
    restart laravel-octane laravel-horizon laravel-reverb

# 11. SMOKE TESTS — see §8.

# 12. Once smoke tests pass, stop the dev redis instance.
docker compose stop redis
# Note: do NOT `docker compose rm redis` — the dev volume stays so you can
# revert if needed.
```

---

## 8. Smoke tests post-cutover

```bash
# 8.1 Cache write/read goes to redis-cache (DB 0 there)
docker exec georag-laravel-octane php artisan tinker --execute='
    Cache::put("smoketest:cache", "hello", 60);
    echo Cache::get("smoketest:cache") . "\n";
'
docker exec georag-redis-cache redis-cli -a "$REDIS_CACHE_PASSWORD" -n 0 KEYS "*smoketest*"
# Expected: matching key visible in redis-cache, NOT in redis-queue or redis-sessions.

# 8.2 Horizon queue goes to redis-queue
docker exec georag-laravel-octane php artisan tinker --execute='
    dispatch((new App\Jobs\GenerateExportJob(1))->onQueue("default"));
'
docker exec georag-redis-queue redis-cli -a "$REDIS_QUEUE_PASSWORD" -n 0 KEYS "queues:*"
# Expected: queues:default key visible in redis-queue.

# 8.3 Session write goes to redis-sessions
# (Trigger via authenticated request)
curl -c /tmp/cookies.txt -X POST -H "Content-Type: application/json" \
    -d '{"email":"<test-user>","password":"<test-pw>"}' \
    https://georag.staging.local/api/v1/auth/login
docker exec georag-redis-sessions redis-cli -a "$REDIS_SESSION_PASSWORD" -n 0 KEYS "*session*"
# Expected: at least one session key visible in redis-sessions.

# 8.4 Reverb broadcast still works (uses cache instance for pub/sub)
# Open a test page that subscribes to a private channel via Echo, then:
docker exec georag-laravel-octane php artisan tinker --execute='
    broadcast(new \App\Events\Dashboard\ActivityEventBroadcast([
        "type" => "smoketest", "ts" => time(),
    ]));
'
# Expected: subscriber receives the event.

# 8.5 Octane health endpoint still passes
curl -s https://georag.staging.local/api/v1/health | jq '.overall'
# Expected: "green" (or "warn" — but NOT "critical")

# 8.6 No eviction is happening anywhere yet
for role in cache queue sessions; do
  port=$(case $role in cache) echo 9121;; queue) echo 9122;; sessions) echo 9123;; esac)
  curl -s "http://localhost:$port/metrics" | grep -E "^redis_evicted_keys_total" | head -1
done
# Expected: all three return 0
```

---

## 9. Rollback procedure

If smoke tests fail or production traffic uncovers a regression:

```bash
# 1. Stop the new instances and exporters.
docker compose --profile staging stop \
    redis-cache redis-queue redis-sessions \
    redis_exporter_cache redis_exporter_queue redis_exporter_sessions

# 2. Restore .env from the dev backup.
mv .env .env.staging.failed-$(date +%Y%m%d)
cp .env.dev.backup .env

# 3. Bring the dev redis back up.
docker compose start redis

# 4. Restart Laravel services.
docker compose restart laravel-octane laravel-horizon laravel-reverb

# 5. Verify service health.
curl -s https://georag.staging.local/api/v1/health | jq '.overall'

# 6. Optional — keep the failed staging containers/volumes for forensic
#    analysis. Drop them only after the regression is understood:
docker compose --profile staging rm -f \
    redis-cache redis-queue redis-sessions \
    redis_exporter_cache redis_exporter_queue redis_exporter_sessions
docker volume rm \
    georagintelligencev10_redis_queue_data \
    georagintelligencev10_redis_sessions_data
```

The rollback is fast because:
- Dev redis volume was never deleted.
- `.env.dev.backup` preserved the original routing.
- The `default` Redis connection in `config/database.php` falls back
  cleanly when the per-role env vars are unset.

---

## 10. Cleanup (when prod has been stable for 30+ days)

```bash
# 1. Remove the dev `redis:` service from compose. The single-instance
#    pattern is now superseded for all environments above dev-light.
#    Keep `dev-light` profile mapping to the new instances OR keep a
#    single redis under `dev-light` profile for low-resource workstations.

# 2. Drop the dev-only env vars (REDIS_HOST, REDIS_PASSWORD) from
#    .env.staging and .env.prod. Keep REDIS_HOST default in .env.example
#    for fresh-clone dev workstations.

# 3. Remove the redis_data volume:
docker volume rm georagintelligencev10_redis_data
```

---

## 11. References

- `ops/runbooks/redis-topology.md` — design rationale (the why)
- `ops/runbooks/backup-restore.md` — AOF backup procedure for redis-queue
   and redis-sessions
- `ops/audit/2026-04-19-resolved-compose-all-profiles.yml` — compose
   conventions to mirror
- `docker/prometheus/rules/redis-alerts.yml` — alert rules (apply per-instance
   automatically once the scrape config from §6 lands)
- `georag-architecture.html` §06 — "Separate instances strongly recommended"

## 12. Open questions

- Should the `dev-light` profile run all three instances at half-size to
  catch instance-routing bugs early in dev? Trade-off: ~3.5 GiB extra
  RAM on dev workstations vs catching staging-only bugs in dev.
- Should `redis-cache` add `--protected-mode no` since it's not on a public
  network and skipping the protection check shaves a connection-time
  microsecond? Default `yes` is the safer choice; suggest leaving it.
