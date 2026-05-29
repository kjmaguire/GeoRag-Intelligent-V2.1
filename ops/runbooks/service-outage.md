# Service Outage Runbook

Common failure modes with triage steps. One failure mode per section. Jump directly to the section matching your symptom.

---

## 1. PgBouncer won't come up

**Symptom:** `georag-pgbouncer` stays `starting` or repeatedly fails its healthcheck. `docker compose ps` shows it unhealthy. Services that depend on it (Laravel, FastAPI, Dagster) never start.

**Triage commands:**

```bash
docker logs georag-pgbouncer --tail 50
docker exec georag-pgbouncer psql -d pgbouncer -U pgbouncer -c 'SHOW POOLS' 2>&1
docker inspect georag-pgbouncer | jq '.[0].Config.Env | map(select(startswith("ADMIN_USERS")))'
```

**Likely cause:** The `ADMIN_USERS` environment variable is not set or does not match the user that the healthcheck uses to run `SHOW POOLS`. PgBouncer's `SHOW POOLS` admin command requires the connecting user to be in `ADMIN_USERS`. This was identified as finding HC-02 in Phase A.

**Fix:**

```bash
# 1. Verify ADMIN_USERS is set in .env:
grep ADMIN_USERS .env

# 2. If missing, add it:
echo 'ADMIN_USERS=pgbouncer' >> .env

# 3. Restart PgBouncer:
docker compose up -d --force-recreate pgbouncer

# 4. Confirm the healthcheck passes:
docker compose ps pgbouncer
```

Also check that `POSTGRES_PASSWORD` in `.env` matches what PostgreSQL was initialized with. PgBouncer cannot establish backend connections if the password is wrong — `docker logs georag-pgbouncer` will show `password authentication failed`.

**Escalate to Kyle if:** PgBouncer healthcheck passes but Laravel still cannot reach the database (suggests a PgBouncer pool configuration problem, not a startup problem).

---

## 2. PostgreSQL won't accept connections from backup-agent

**Symptom:** `docker exec georag-backup-agent /backup-scripts/postgresql/backup.sh` fails with `pg_basebackup: error: connection to server failed: FATAL: no pg_hba.conf entry for replication connection from host "172.19.x.x"`.

**Triage commands:**

```bash
# Check which pg_hba.conf file PostgreSQL is using:
docker exec georag-postgresql psql -U georag -c "SHOW hba_file;"

# Inspect the bind-mounted hba file:
cat docker/postgresql/pg_hba.conf | grep replication

# Check the backup-agent's IP on the georag network:
docker network inspect georag | jq '.[0].Containers | to_entries[] | select(.value.Name | contains("backup")) | .value.IPv4Address'
```

**Likely cause:** Either (a) the `hba_file` is pointing to `$PGDATA/pg_hba.conf` instead of the bind-mounted `/etc/postgresql/pg_hba.conf` — meaning the `-c hba_file=` flag was lost in a compose change, or (b) the replication ACL CIDR in `docker/postgresql/pg_hba.conf` does not cover the backup-agent's IP (the `georag` bridge network uses `172.19.0.0/16`).

**Fix:**

```bash
# Case A — hba_file pointing wrong:
# Verify the compose command for postgresql includes: -c hba_file=/etc/postgresql/pg_hba.conf
grep "hba_file" docker-compose.yml

# If missing, add it to the postgresql service command and recreate:
docker compose up -d --force-recreate postgresql

# Case B — CIDR mismatch:
# Confirm backup-agent IP is within 172.19.0.0/16:
docker network inspect georag | jq '.[0].Containers'

# If the georag network uses a different subnet, update docker/postgresql/pg_hba.conf:
# Change: host replication all 172.19.0.0/16 scram-sha-256
# To the correct CIDR, then reload:
docker exec georag-postgresql psql -U georag -c "SELECT pg_reload_conf();"
```

**Escalate to Kyle if:** The CIDR matches, the hba_file is correct, and `psql` still fails with an auth error — this may indicate the `POSTGRES_PASSWORD` in `.env` does not match the initialized database password (requires a full data volume recreation to fix).

---

## 3. Neo4j store-lock / "failed to open" on startup

**Symptom:** `georag-neo4j` exits immediately or crash-loops. Logs contain `Store lock is held by another process` or `Failed to open store`.

**Triage commands:**

```bash
docker logs georag-neo4j | head -50
docker exec georag-neo4j ls /data/databases/neo4j/
# Look for files named: *.lock or store_lock
```

**Likely cause:** The previous Neo4j container was killed (exit 137 / SIGKILL) before completing its checkpoint flush, leaving a lock file in the data directory. This happens when the container is stopped with less than the configured `stop_grace_period: 60s` or when the host machine crashes.

**Fix:**

```bash
# 1. Stop the crash-looping container:
docker compose stop neo4j

# 2. Remove the lock file via a one-shot container (mounts the data volume):
NEO4J_DATA_VOLUME="GeoRag_Intelligence_V1.0_neo4j_data"
docker run --rm \
  --volume "${NEO4J_DATA_VOLUME}:/data" \
  alpine:3.20 \
  sh -c "find /data -name '*.lock' -o -name 'store_lock' | xargs -r rm -v"

# 3. Restart Neo4j:
docker compose up -d neo4j

# 4. Watch logs for clean startup:
docker logs georag-neo4j --follow --tail 20
```

> Do not use `neo4j-admin store info --recover` unless you have confirmed data corruption. Forcing a store open without proper recovery can cause data loss. If the lock removal alone does not fix the issue, stop here and escalate.

**Escalate to Kyle if:** After lock removal, Neo4j still fails to open the store, or logs contain `StoreId mismatch` or corruption-related messages.

---

## 4. Neo4j OOM / memory validation error

**Symptom:** `georag-neo4j` exits immediately (exit code 1, not 137). Logs contain `Invalid memory configuration - exceeds physical memory` or `Check the configured values for server.memory.pagecache.size and server.memory.heap.max_size`.

**Triage commands:**

```bash
docker logs georag-neo4j | grep -i "memory\|heap\|pagecache"
docker inspect georag-neo4j | jq '.[0].HostConfig.Memory'
# Returns the container memory limit in bytes. 9663676416 = 9 GiB (correct).
# 6442450944 = 6 GiB (too low — caused the crash in Phase B Action 3).
```

**Likely cause:** Neo4j validates that `pagecache.size + heap.max_size` does not exceed the container's cgroup memory limit. If the compose memory limit is lower than the sum of the configured values (pagecache=4G + heap=4G = 8G), Neo4j refuses to start. The correct container limit is 9G.

**Fix:**

```bash
# 1. Check current limit in docker-compose.yml:
grep -A5 "neo4j:" docker-compose.yml | grep -E "memory|limit"
# Expected: deploy.resources.limits.memory: 9G

# 2. If the limit is wrong, edit docker-compose.yml to set it to 9G,
#    then recreate the container:
docker compose up -d --force-recreate neo4j

# 3. Alternatively, reduce Neo4j heap in .env (acceptable for dev, below spec):
# Set NEO4J_HEAP_MAX_SIZE=2G  (pagecache 4G + heap 2G = 6G, fits in 6G limit)
# This reduces Cypher query execution memory — not recommended for prod.
```

**Escalate to Kyle if:** The container limit is already 9G and Neo4j still reports a memory validation error — this suggests the host has less than 9G free and Docker cannot honor the reservation.

---

## 5. Qdrant snapshot restore fails

**Symptom:** The restore API call in `backup-restore.md §C` returns an error. Common errors: 401 (auth), 409 (collection already exists with incompatible config), 400 (bad snapshot format).

**Triage commands:**

```bash
# Check if QDRANT_API_KEY is set (dev default is empty = no auth required):
docker exec georag-backup-agent env | grep QDRANT_API_KEY

# List existing collections:
curl -sf http://localhost:6333/collections | jq '.result.collections[].name'

# Check Qdrant logs for the error detail:
docker logs georag-qdrant --tail 30
```

**Likely cause:**

- **401:** `QDRANT_API_KEY` is set on the Qdrant server but not passed in the restore curl command. Add `-H "api-key: ${QDRANT_API_KEY}"` to the curl call.
- **409 / collection conflict:** The collection exists with a different vector dimension or distance metric. Delete the collection first, then re-upload.
- **400 / bad format:** The snapshot file was corrupted during download or the S3 object is from a different Qdrant version. Re-download from S3 and retry.

**Fix for 409:**

```bash
COLLECTION=georag_reports
curl -sf -X DELETE "http://localhost:6333/collections/${COLLECTION}" | jq .
# Then repeat the snapshot upload from backup-restore.md §C.
```

**Fix for 401:**

```bash
# Add the API key header to all Qdrant curl calls:
QDRANT_API_KEY=$(docker exec georag-qdrant env | grep QDRANT_API_KEY | cut -d= -f2)
curl -sf -H "api-key: ${QDRANT_API_KEY}" -X POST \
  "http://localhost:6333/collections/${COLLECTION}/snapshots/upload?priority=snapshot" \
  ...
```

**Escalate to Kyle if:** The snapshot uploads successfully but the vector count after restore does not match the expected count from the backup log — this may indicate a snapshot was taken during an in-progress indexing operation.

---

## 6. SeaweedFS volume at capacity

**Symptom:** Any S3 write (backup upload, file ingest, export) fails with: `No writable volumes and no free volumes left for collection:georag-backups` or similar. Visible in `docker exec georag-backup-agent` output or in `docker logs georag-minio`.

**Triage commands:**

```bash
# Check current volume server flags:
docker exec georag-minio ps aux | grep weed
# Look for: -volume.max=32

# Check SeaweedFS master status (how many volumes are allocated):
curl -sf http://localhost:9333/cluster/status | jq .
curl -sf http://localhost:9333/dir/status | jq '.Topology.DataNodes[].Volumes'
```

**Likely cause:** The SeaweedFS volume server's `-volume.max` cap is too low. The default was 8 (used before Phase B fix). Phase B raised it to 32 (`docker/seaweedfs/entrypoint.sh`). If a fresh container was started without the current `entrypoint.sh`, it may have reverted to the old default.

**Fix:**

```bash
# Verify entrypoint.sh contains -volume.max=32:
grep "volume.max" docker/seaweedfs/entrypoint.sh

# If missing, edit docker/seaweedfs/entrypoint.sh to add -volume.max=32 to the
# `exec weed server` command, then force-recreate:
docker compose up -d --force-recreate minio
```

To raise the cap further (e.g., to 64) if 32 volumes fill up:

```bash
# Edit docker/seaweedfs/entrypoint.sh:
# Change: -volume.max=32
# To: -volume.max=64
# Then: docker compose up -d --force-recreate minio
```

**Escalate to Kyle if:** The volume cap is already at 32 and the SeaweedFS data directory (`minio_data` volume) is consuming more than 80% of the host disk — this requires a storage capacity decision, not a configuration change.

---

## 7. Ofelia jobs failing silently

**Symptom:** Backups are not landing in S3 at the expected times. `docker logs georag-ofelia` shows jobs were triggered but no error is visible. Or: Ofelia shows `unable to start a empty scheduler` (zero jobs registered).

**Triage commands:**

```bash
# Check Ofelia registered jobs:
docker logs georag-ofelia 2>&1 | grep -iE "registered|loaded|job|error|fail"

# Check backup-agent for execution errors:
docker logs georag-backup-agent --tail 100 2>&1 | grep -iE "error|fail|credential|s3"

# Check if backup-agent has AWS credentials:
docker exec georag-backup-agent env | grep -E "AWS_|S3_ENDPOINT"

# Check Ofelia can exec into the backup-agent:
docker exec georag-backup-agent echo "exec works"
# If this hangs or fails, the Docker socket mount is the issue.
```

**Likely causes and fixes:**

**A — Zero jobs registered:** The Ofelia job labels on `georag-backup-agent` are disabled or missing. Check:

```bash
docker inspect georag-backup-agent | jq '.[0].Config.Labels'
# Should contain: ofelia.job-exec.pg-backup.schedule, etc.
```

If missing, the compose service definition may have lost the labels. Reapply from `docker-compose.yml` and force-recreate:

```bash
docker compose up -d --force-recreate backup-agent ofelia
```

**B — AWS credentials missing:** `.env` does not contain `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, or `S3_ENDPOINT_URL`. Add them and recreate the backup-agent:

```bash
docker compose up -d --force-recreate backup-agent
```

**C — Docker socket permission error:** The backup-agent mounts `/var/run/docker.sock:ro` for the Neo4j offline dump script. If the socket is not accessible, the Neo4j backup will fail. The backup user (UID 1001) is added to the `docker` group in the Dockerfile. If Docker Desktop changed the socket permissions, the fix is to rebuild the image:

```bash
docker compose build backup-agent
docker compose up -d --force-recreate backup-agent
```

**D — Distroless image confusion:** If someone tries to `docker exec georag-qdrant bash` to debug a Qdrant issue, it will fail cryptically because the Qdrant image is distroless (no shell). This is expected. All Qdrant interaction must go through `georag-backup-agent` or an alpine sidecar.

**Escalate to Kyle if:** Ofelia shows jobs registering and triggering, backup-agent logs show the script starting, but S3 writes still fail — this indicates a SeaweedFS connectivity or capacity issue (see §6).

---

## 8. Octane / Swoole graceful-drain failures

**Symptom:** After `docker compose stop laravel-octane` (or a deploy restart), the container takes exactly 30 seconds to exit and shows exit code 137 (SIGKILL, not 0). Logs show requests being dropped mid-flight. In-flight HTTP requests are terminated abruptly.

**Triage commands:**

```bash
# Check the actual exit code of the last stop:
docker inspect georag-laravel-octane | jq '.[0].State.ExitCode'
# 0 = clean SIGTERM. 137 = SIGKILL.

# Check the compose command for the exec prefix:
docker inspect georag-laravel-octane | jq '.[0].Config.Cmd'
# Should NOT be: ["sh", "-c", "php artisan octane:start ..."]
# Should be:     ["sh", "-c", "exec php artisan octane:start ..."]
#                 ^^^^
#                 exec is required — without it, sh holds PID 1 and
#                 SIGTERM from Docker never reaches the Swoole process.
```

**Likely cause:** The `exec` prefix is missing from the `sh -c` wrapper in the compose command for `laravel-octane`. Without `exec`, the shell holds PID 1 and does not propagate Docker's SIGTERM to the Swoole master process. Docker times out and sends SIGKILL. This was found and fixed in Phase C (C5-01).

**Fix:**

```bash
# Verify the command in docker-compose.yml contains 'exec php artisan':
grep -A3 "laravel-octane" docker-compose.yml | grep "command"
# Expected: command: sh -c "exec php artisan octane:start ..."

# If the exec prefix is missing, edit docker-compose.yml and recreate:
docker compose up -d --force-recreate laravel-octane
```

After the fix, `docker stop georag-laravel-octane` should exit in approximately 1 second with exit code 0.

**Escalate to Kyle if:** The `exec` prefix is present and exit code is still 137 — this may indicate the Swoole process has a stuck worker handling a long-running request that exceeds the 30-second grace period. Investigate via `docker exec georag-laravel-octane php artisan octane:status`.

---

## 9. vLLM OOM on prod GPU

**Symptom:** `georag-vllm` exits with OOM (exit code 137 or CUDA out-of-memory error in logs). Model does not load.

**Note:** vLLM is in the `gpu-llm-prod` profile only and is not active in the current Module 1 development environment. This section is a placeholder for when GPU inference is enabled.

```bash
nvidia-smi
docker logs georag-vllm --tail 50 | grep -iE "oom|cuda|memory|error"
```

Per Hard Rule in CLAUDE.md: golden-query tests must pass on the prod GPU before any traffic is routed to it. If OOM occurs on the first load attempt, the model is too large for available VRAM. Consult Section 11 of `georag-architecture.html` for model sizing guidance. Do not reduce quantization below int4 without SME approval.

**Escalate to Kyle immediately** — prod GPU sizing decisions are outside the V1 scope of this runbook.

---

## 10. Nothing works — stack boot-looped

**Symptom:** Multiple containers are in a crash loop. `docker compose ps` shows several services as `restarting` or `unhealthy`. The UI is unreachable. You are not sure what started the cascade.

**Triage sequence:**

```bash
# Step 1 — Get an overview of all service states:
docker compose --profile dev-light --profile dev-data ps

# Step 2 — Find the first unhealthy service (the cascade root):
docker compose --profile dev-light --profile dev-data ps --format json \
  | jq -r '.[] | select(.Health != "healthy" and .Health != "") | [.Name, .Health, .Status] | @tsv'

# Step 3 — Check logs for that service:
docker compose logs <failing-service> --tail 100

# Step 4 — Check if the root cause is a stateful store:
docker compose ps postgresql pgbouncer redis neo4j qdrant minio
# A stateful store failure cascades: PG down → PgBouncer can't proxy → Laravel never starts → FastAPI never starts.

# Step 5 — Check for disk full (a silent killer):
df -h
docker system df
```

**Common cascade patterns:**

| Root failure | Cascade |
|---|---|
| `postgresql` unhealthy | `pgbouncer` healthcheck fails → Laravel Octane/Horizon/Reverb never start → FastAPI depends_on never satisfied |
| `neo4j` crash-looping | `fastapi` never becomes healthy (depends_on neo4j service_healthy) → all RAG features broken |
| `minio` unhealthy | `fastapi` never starts (depends_on minio) → backup-agent cannot upload |
| `redis` unhealthy | `laravel-octane` and `laravel-horizon` won't start (depends_on redis) |

**Recovery steps for a full cascade:**

```bash
# 1. Stop everything to get a clean slate:
docker compose --profile dev-light --profile dev-data stop

# 2. Start the stateful stores first and confirm they are healthy:
docker compose up -d postgresql redis
docker compose ps   # wait for healthy

# 3. Start the connection layer:
docker compose up -d pgbouncer minio neo4j qdrant
docker compose ps   # wait for healthy

# 4. Start the application layer:
docker compose --profile dev-light --profile dev-data up -d

# 5. Confirm all services healthy:
docker compose --profile dev-light --profile dev-data ps
```

If a stateful store (PostgreSQL, Neo4j, Qdrant) itself is failing to start, consult the relevant section in this runbook (§3 for Neo4j lock, §4 for Neo4j memory, §2 for PG access issues) before attempting a volume recovery.

**Escalate to Kyle if:** A stateful store is failing to open its data volume after lock removal and memory checks pass — this may indicate data corruption requiring backup restoration from `ops/runbooks/backup-restore.md`.

---

_Written 2026-04-19 during Module 1 Phase D. Update this file whenever the underlying procedure changes._

---

## 11. Redis won't come up / rejecting connections

**Symptom:** `redis_up=0` in Prometheus; Laravel Pulse + Horizon + retrieval cache all failing.

**Triage:**
```bash
docker logs georag-redis --tail 30
docker exec georag-redis redis-cli -a "$REDIS_PASSWORD" PING
docker exec georag-redis redis-cli -a "$REDIS_PASSWORD" INFO clients | grep -E "connected_clients|maxclients|rejected_connections"
docker exec georag-redis redis-cli -a "$REDIS_PASSWORD" INFO memory | grep -E "used_memory_human|maxmemory_human"
```

**Likely causes + fixes:**
- Auth failure (rejected_connections > 0, password drift) → re-sync per `secret-rotation.md` § REDIS_PASSWORD.
- maxclients exhausted (Laravel queue worker leak) → `docker compose restart laravel-horizon`. Inspect `pulse_exception_total` for orphaned-job patterns.
- OOM eviction storm (`used_memory > maxmemory`) → bump `maxmemory` OR tighten cache TTLs.

---

## 12. Reverb (Laravel WebSocket) silent

**Symptom:** SSE chat completes but SPA shows stale state — connection never re-establishes after deploy.

**Triage:**
```bash
docker logs georag-laravel-reverb --tail 30
curl -fsS http://localhost:6001/health
docker exec georag-laravel-reverb php artisan reverb:status
```

**Likely causes + fixes:**
- Stale Pusher protocol clients post-schema-bump → `php artisan view:clear && php artisan cache:clear`.
- Reverb worker exited silently → `docker compose restart laravel-reverb`.
- Front-end pointing at wrong host → verify `VITE_REVERB_HOST`/`REVERB_HOST` in `.env.production`.

---

## 13. Martin tile server slow / 500s

**Symptom:** Map dashboard tiles missing; browser 500/502s on `/tiles/...`.

**Triage:**
```bash
docker logs georag-martin --tail 30
curl -fsS http://localhost:3000/health
curl -fsS http://localhost:3000/catalog | jq '.tiles | keys | length'
```

**Likely causes + fixes:**
- Stale config — see Memory `feedback_martin_tile_gotchas.md` Gotcha 2. Use `docker rm -f georag-martin && docker compose up -d martin` (NOT `restart`).
- PG connection refused — Martin connects DIRECTLY to PostgreSQL (bypasses PgBouncer). Verify `martin_readonly` role per Module 8 Chunk 8.3.
- Function broken post-migration — Gotcha 1 (PL/pgSQL DECLARE shadowing fails on CALL not CREATE). Run the smoke-test SQL block in that gotcha's verification section.
- `/metrics` 404 in Prometheus — expected (Gotcha 3, Martin 1.5.0 has no /metrics). Ignore until V1.5 upgrade.

---

## 14. Horizon dashboard queues stuck

**Symptom:** `horizon_queue_depth > 1000` sustained; jobs queue but don't process.

**Triage:**
```bash
docker exec georag-laravel-horizon php artisan horizon:status
docker exec georag-laravel-horizon php artisan queue:failed
docker logs georag-laravel-horizon --tail 50
```

**Likely causes + fixes:**
- Worker crash loop (Pulse exception spike correlated) → check exception dashboard.
- Redis connection limit interaction (see §11).
- Long-running job blocking → `docker exec georag-laravel-horizon php artisan queue:retry all`.
- maxProcesses too low for current load → bump `config/horizon.php` + restart.

---

## 15. FastAPI restart loops / crash on boot

**Symptom:** `up{job="fastapi"}=0`, restart count climbing.

**Triage:**
```bash
docker logs georag-fastapi --tail 80
docker exec georag-fastapi env | grep -E "MULTI_TENANT|FASTAPI_SERVICE_KEY|TIMEOUT_"
```

**Likely causes + fixes:**
- Pydantic Settings validator failure (Module 9 9.4 enforcement) — env has `MULTI_TENANT_ENFORCEMENT_ENABLED=False` AND `SINGLE_TENANT_MODE=False`. Fix per `secret-management.md`.
- Redis or Postgres unreachable at boot — lifespan hook gives up after retry budget. Fix dependency first.
- HF model cache corrupted (after disk full) — wipe per `volume-migration.md` § Disaster recovery.

---

## 16. Dagster daemon stopped processing schedules

**Symptom:** Scheduled ingestion not firing; sensors silent.

**Triage:**
```bash
docker logs georag-dagster-daemon --tail 50
docker exec georag-dagster-webserver curl -fsS http://localhost:3001/health
```

Open `http://<host>:3001` → Daemons tab. Red Scheduler → restart:
```bash
docker compose restart dagster-daemon
```

If daemon won't start due to volume permission (Module 9 9.7 nobody:nobody UID), follow `volume-migration.md` § Non-root UID migration → Option B.

---

_Annexes 11-16 added 2026-04-22 during Module 10 Chunk 10.8._
