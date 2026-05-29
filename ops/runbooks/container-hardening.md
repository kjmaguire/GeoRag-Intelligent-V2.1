# Container hardening — UIDs, APP_DEBUG, trusted proxies

**Last updated:** 2026-04-22 (Module 9 Chunk 9.7)
**Audit reference:** `ops/audit/2026-04-22-security-rbac-audit.md` finding A8-02 (MEDIUM).

This runbook documents how GeoRAG containers run as non-root and how to add new
services without regressing that posture.

## TL;DR

| Service | Image USER | Compose `user:` | UID:GID |
|---------|------------|------------------|---------|
| `fastapi` | `www-data` (Dockerfile) | `"33:33"` (explicit) | 33:33 |
| `dagster-daemon` | `nobody` (Dockerfile) | `"65534:65534"` (explicit) | 65534:65534 |
| `dagster-webserver` | `nobody` (Dockerfile) | `"65534:65534"` (explicit) | 65534:65534 |

Three previously had `user: root` in compose, overriding the Dockerfile USER and
running as UID 0. Module 9 Chunk 9.7 flipped them to explicit non-root UIDs.

## Why explicit UIDs and not just dropping the override?

Dockerfile USER directives are correct in the image, but the Docker daemon
applies named-volume ownership at first-mount based on the running container's
UID. Pinning the compose `user:` to the same UID the Dockerfile declares makes
the contract explicit and survives an image-rebuild that might forget the USER
line.

## Migration steps for existing dev environments

Existing dev stacks have named volumes (`fastapi_hf_cache`, `dagster_home`)
created with **root** ownership because the previous compose ran as root. After
pulling Module 9 Chunk 9.7, the new non-root user can't write to those volumes
until the volume is recreated.

### Option A — Recreate the volumes (preserves no data, idempotent for dev)

```bash
docker compose stop fastapi dagster-daemon dagster-webserver
docker volume rm \
    georag_fastapi_hf_cache \
    georag_dagster_home
docker compose up -d fastapi dagster-daemon dagster-webserver
```

Dagster will re-run pending sensors on first boot; FastAPI re-downloads the HF
embedding models on first query (one-time ~3 min). Acceptable for dev.

### Option B — chown in place (preserves data)

```bash
# 1. Stop the affected services (keep volumes mounted on a privileged helper).
docker compose stop fastapi dagster-daemon dagster-webserver

# 2. chown via a one-shot privileged container that mounts each volume.
docker run --rm -v georag_fastapi_hf_cache:/data alpine \
    chown -R 33:33 /data

docker run --rm -v georag_dagster_home:/data alpine \
    chown -R 65534:65534 /data

# 3. Restart.
docker compose up -d fastapi dagster-daemon dagster-webserver
```

Use Option B when you have populated state worth keeping (Dagster run history
in particular).

## Adding a new service

1. **Set USER in the Dockerfile.** Don't run as root in the image.
2. **Pin the same UID in `docker-compose.yml`** with `user: "<uid>:<gid>"`.
3. **Pre-stage volume permissions** in the Dockerfile if the service writes to a
   bind mount or named volume:
   ```dockerfile
   RUN mkdir -p /var/lib/myservice \
       && chown -R myuser:mygroup /var/lib/myservice
   ```
4. **Document any deviation** inline in `docker-compose.yml` if you must keep
   `user: root` (rare — open a backlog item to migrate later).

## APP_DEBUG default

`docker-compose.yml` defaults all three Laravel services (Octane, Horizon,
Reverb) to `APP_DEBUG=false`. This means stack traces are NOT surfaced in HTTP
500 responses by default.

**Local dev override:** create or edit `.env` at the project root with
`APP_DEBUG=true`. Compose reads `.env` first, then `docker-compose.yml` defaults
take effect only when the env var is unset.

**Verification:**

```bash
docker compose exec laravel-octane env | grep APP_DEBUG
# expected: APP_DEBUG=false
```

Trigger a 500 (e.g. point at a route that does `throw new RuntimeException(...)`)
and confirm the response body is the generic Laravel error page, not a
stack-trace page.

## Trusted proxies (`TRUSTED_PROXIES`)

The Laravel middleware (registered in `bootstrap/app.php` via Module 9 Chunk
9.5) reads `TRUSTED_PROXIES` to decide whose `X-Forwarded-*` headers to honour.

**Dev:** `TRUSTED_PROXIES=*` (any caller can claim to be a proxy).
**Production:** set to your nginx/Traefik CIDR — e.g. `TRUSTED_PROXIES=10.0.0.0/8`.

Without trust, `request()->ip()` returns the proxy IP for every request, which
collapses the auth-login rate limiter (which keys on email + client IP) into a
single bucket per origin — defeating per-user throttling.

## Verification checklist

```bash
# 1. No service runs as root
docker compose ps --format "{{.Name}}" | xargs -I {} sh -c 'echo "=== {} ==="; docker compose exec -T {} id 2>/dev/null'
# Expect uid=0 ONLY on services where root is justified inline.

# 2. APP_DEBUG=false in compose default
docker compose config | grep APP_DEBUG
# Expect APP_DEBUG: false on all three Laravel services.

# 3. CSP + security headers present
curl -I http://localhost:8888/
# Expect: X-Frame-Options, X-Content-Type-Options, Referrer-Policy,
# Permissions-Policy, Content-Security-Policy.

# 4. CORS preflight allowed methods are explicit (no '*')
curl -i -X OPTIONS \
     -H 'Origin: http://localhost:5173' \
     -H 'Access-Control-Request-Method: GET' \
     http://localhost:8888/api/v1/auth/spa-login \
     | grep -i 'allow-methods'
# Expect a comma-separated list, not '*'.
```

## Carry-forward backlog

If a future Dagster upgrade or new sidecar requires CAP_SYS_ADMIN or similar
privileges, document the reason inline in `docker-compose.yml` and open a
backlog item under `ops/backlog/` referencing this runbook.
