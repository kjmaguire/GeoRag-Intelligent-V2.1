# Secret Rotation

**Module 10 Chunk 10.8** — operator playbook for rotating every credential
class in the GeoRAG stack. Pairs with `secret-management.md` (which covers
the SOPS+age tooling). This file is the **how-do-I-rotate-X step-by-step**.

## Rotation cadence (canonical table)

Reproduced from `secret-management.md` for one-stop reference.

| Credential | Default cadence | Trigger for off-cycle rotation |
|------------|-----------------|--------------------------------|
| `APP_KEY` (Laravel) | 1 year | Suspected leak; key in error log; ex-employee |
| `FASTAPI_SERVICE_KEY` (HS256) | 90 days | Suspected leak; HSM/Vault unavailable for >24h |
| `POSTGRES_PASSWORD` | 90 days | Compromise; ex-DBA |
| `REDIS_PASSWORD` | 90 days | Compromise |
| `NEO4J_PASSWORD` | 90 days | Compromise |
| `MINIO_ROOT_PASSWORD` (legacy) / SeaweedFS access keys | 90 days | Compromise; bucket leak |
| `GRAFANA_ADMIN_PASSWORD` | 30 days | Suspected leak |
| `DAGSTER_PG_PASSWORD` | 90 days | Coupled with POSTGRES_PASSWORD |
| Sanctum personal access tokens (per-user) | User-controlled | User logout; suspected XSS |
| Sanctum SPA session cookie | Per-session | Account compromise |
| age private key (CI) | 1 year | Suspected leak; CI host compromise |
| age private keys (operator) | 1 year | Operator offboarding |

## General rotation pattern

```
1. Mint a new value (use openssl rand -base64 48 or service-specific generator).
2. Stage it in the encrypted env file: edit .env.production, sops --encrypt → commit.
3. Push to staging FIRST. Run release-rehearsal.yml. Verify auth works end-to-end.
4. Push to production. Watch authz_audit channel for spikes.
5. Revoke the old value in the underlying service.
6. Document the rotation in CHANGELOG.md with the date + ticket.
```

The order matters: stage → revoke. If you revoke first, dev/staging lose
auth and you can't ship the new value. Always overlap.

## Per-credential procedures

### APP_KEY (Laravel)

```bash
# 1. Generate a new key (Laravel's built-in ensures correct shape).
docker compose exec laravel-octane php artisan key:generate --show
# → base64:abcd1234... — copy this.

# 2. Add to encrypted env, KEEPING the old key as APP_KEY_PREVIOUS so
#    encrypted DB columns can still be decrypted while we re-encrypt.
sops .env.production
#   APP_KEY=base64:NEWKEY
#   APP_KEY_PREVIOUS=base64:OLDKEY      # add this line
sops --encrypt .env.production > .env.production.enc
git add .env.production.enc && git commit -m "chore(secrets): rotate APP_KEY"

# 3. Deploy to staging. Run:
docker compose exec laravel-octane php artisan crypt:rotate
# (custom artisan command — wraps a re-encrypt loop over query_audit_log
#  rows and any other encrypted-cast columns.)

# 4. After re-encrypt completes (verify via row count match), drop
#    APP_KEY_PREVIOUS from the env, re-encrypt, redeploy.
```

**Risk:** if step 3 doesn't complete before APP_KEY_PREVIOUS is dropped,
historical encrypted data becomes unrecoverable. Always verify row counts.

### FASTAPI_SERVICE_KEY (HS256 JWT) — `kid`-based rotation (V1.5-03)

The Laravel side mints JWTs signed with this key and stamps the active key
id in the `kid` JWT header; FastAPI side reads the `kid`, looks the key up
in a `kid → secret` map, and verifies the signature. If both sides agree
on the kid set, rotation is zero-downtime.

```bash
# 1. Generate the new 48-byte key + decide on a new kid (any short string).
NEW_KEY=$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')
NEW_KID="2026-q3"
echo "kid=$NEW_KID secret_first8=${NEW_KEY:0:8}…"

# 2. Edit the encrypted prod env. Add the new key as FASTAPI_SERVICE_KEY_NEW
#    + FASTAPI_SERVICE_KEY_NEW_KID; keep the current as FASTAPI_SERVICE_KEY
#    + FASTAPI_SERVICE_KEY_KID. The PREVIOUS_* slots are for the OUTGOING key.
sops .env.production
#   FASTAPI_SERVICE_KEY=<current secret>
#   FASTAPI_SERVICE_KEY_KID=primary                 # current kid
#   FASTAPI_SERVICE_KEY_PREVIOUS=<keep this empty during overlap setup>
#   FASTAPI_SERVICE_KEY_PREVIOUS_KID=<keep this empty too>
#   # Stage the new key in unused slots while we're not yet ready to cut:
#   FASTAPI_SERVICE_KEY_NEW=<NEW_KEY>               # placeholder for step 3
#   FASTAPI_SERVICE_KEY_NEW_KID=<NEW_KID>
sops --encrypt > .env.production.enc

# 3. CUT-OVER. In a single edit, swap so FastAPI accepts BOTH:
sops .env.production
#   FASTAPI_SERVICE_KEY=<NEW_KEY>                   # new is primary
#   FASTAPI_SERVICE_KEY_KID=<NEW_KID>
#   FASTAPI_SERVICE_KEY_PREVIOUS=<old key>          # old is accepted
#   FASTAPI_SERVICE_KEY_PREVIOUS_KID=primary        # old kid
sops --encrypt > .env.production.enc

# 4. Deploy. FastAPI now accepts JWTs signed with EITHER key; Laravel still
#    mints with the OLD key for 1 deploy cycle so in-flight calls aren't
#    interrupted. Watch authz_audit for unexpected 401s.

# 5. Bump Laravel's mint kid to the NEW kid in a follow-up deploy:
sops .env.production
#   (no change to FASTAPI_SERVICE_KEY — Laravel reads the same env var,
#   which now holds NEW_KEY since step 3.)
#   The FASTAPI_SERVICE_KEY_KID env now controls Laravel's mint kid AND
#   FastAPI's primary verify kid — both already point to NEW_KID.
docker compose restart laravel-octane laravel-horizon laravel-reverb fastapi

# 6. After 1h with no anomalies, drop the PREVIOUS slots:
sops .env.production
#   FASTAPI_SERVICE_KEY_PREVIOUS=
#   FASTAPI_SERVICE_KEY_PREVIOUS_KID=
sops --encrypt > .env.production.enc
docker compose restart fastapi
```

**JWT TTL is 60s** (per Module 9 9.4) so the rotation overlap window can be
short — 5 min is enough to drain in-flight calls. Memory `feedback_datastore_gotchas.md`
documents the per-request `num_ctx` gotcha that's unrelated but lives in
the same auth flow.

**Verification post-rotation:**

```bash
# Laravel-side mint produces the expected kid.
docker compose exec laravel-octane php artisan tinker \
  --execute='$jwt = app(App\Services\FastApiJwtMinter::class)->mint(1, "00000000-0000-0000-0000-000000000001"); $h = json_decode(base64_decode(strtr(explode(".", $jwt)[0], "-_", "+/")), true); echo "minted_kid=" . ($h["kid"] ?? "MISSING") . PHP_EOL;'
# Expect: minted_kid=<NEW_KID>

# FastAPI-side decode accepts BOTH keys during overlap.
docker compose exec fastapi pytest tests/test_jwt_auth.py -k kid -q
# Expect: 4/4 kid tests passing.
```

### POSTGRES_PASSWORD / DAGSTER_PG_PASSWORD

Coupled — Dagster has its own DB user but typically reuses the password.
If they're separate, rotate independently.

```bash
# 1. ALTER ROLE inside Postgres FIRST (so the old creds keep working until
#    the new are confirmed deployed).
NEW_PG=$(openssl rand -base64 48)
docker compose exec -T postgresql psql -U georag -d georag -c "
  ALTER ROLE georag WITH PASSWORD '$NEW_PG';
"

# 2. Update encrypted env.
sops .env.production    # POSTGRES_PASSWORD=$NEW_PG
sops --encrypt > .env.production.enc

# 3. Restart everything that talks to Postgres: laravel-octane, horizon,
#    reverb, fastapi, dagster-daemon, dagster-webserver, postgres_exporter,
#    pgbouncer. PgBouncer in particular caches connection strings — `docker
#    compose restart pgbouncer` is required.

docker compose restart pgbouncer laravel-octane laravel-horizon \
                       laravel-reverb fastapi dagster-daemon \
                       dagster-webserver postgres_exporter

# 4. Watch Pulse exception dashboard for SQLSTATE 28P01 (auth failed) over
#    the next 5 min. None expected.
```

### REDIS_PASSWORD

```bash
# 1. Mint, write to env, restart redis + everything that talks to it.
NEW_REDIS=$(openssl rand -base64 32)
sops .env.production    # REDIS_PASSWORD=$NEW_REDIS
sops --encrypt > .env.production.enc

# 2. ACL update inside Redis BEFORE restart (so existing connections survive).
docker compose exec redis redis-cli -a OLD_REDIS \
    "ACL SETUSER default >$NEW_REDIS"

# 3. Restart consumers.
docker compose restart laravel-octane laravel-horizon laravel-reverb \
                       fastapi redis_exporter
```

### NEO4J_PASSWORD

Neo4j Community needs the user-management API enabled to script the
change; otherwise edit `auth.txt` inside the container, restart.

```bash
# 1. Online change via Cypher (preferred — no restart):
docker compose exec neo4j cypher-shell -u neo4j -p OLD_NEO4J \
    "ALTER USER neo4j SET PASSWORD 'NEW_NEO4J' SET PASSWORD CHANGE NOT REQUIRED;"

# 2. Update env + restart consumers.
sops .env.production    # NEO4J_PASSWORD=NEW_NEO4J
sops --encrypt > .env.production.enc
docker compose restart fastapi dagster-daemon
```

### SeaweedFS access keys

```bash
# SeaweedFS uses S3-compatible IAM. Use the master API:
docker compose exec seaweedfs weed shell <<'EOF'
s3.configure -access_key=NEW_KEY -secret_key=NEW_SECRET -user=georag-app -actions=Read,Write,List
EOF

# Update env, restart consumers.
sops .env.production    # MINIO_ROOT_USER + MINIO_ROOT_PASSWORD (legacy names; SeaweedFS reads the same env)
docker compose restart fastapi dagster-daemon
```

### GRAFANA_ADMIN_PASSWORD

```bash
# Grafana provisions admin from env on first boot. To change:
docker compose exec grafana grafana cli admin reset-admin-password NEW_GRAFANA
sops .env.production    # GRAFANA_ADMIN_PASSWORD=NEW_GRAFANA
docker compose restart grafana
```

### Sanctum personal access tokens

Per-user; rotated by users via the SPA's Account → Tokens page. Operator
emergency revocation:

```bash
docker compose exec laravel-octane php artisan tinker
>>> User::find(42)->tokens()->delete();
>>> exit
```

### age private keys

Operator keys live on individual workstations under `~/.config/sops/age/keys.txt`.
The CI key lives as `SOPS_AGE_PRIVATE_KEY` GitHub Secret.

```bash
# 1. Generate new key.
age-keygen -o ~/.config/sops/age/keys-NEW.txt

# 2. Add the new public recipient to .sops.yaml.
# 3. sops updatekeys .env.production.enc — re-encrypts with the new recipient list.
# 4. Commit + deploy. New CI runs use the new key.
# 5. Drop the old recipient from .sops.yaml + sops updatekeys again.
```

## Audit trail

Every rotation logs to `authz_audit` channel via the metric counter
(Module 10 Chunk 10.4 + 10.6). Grep Loki:

```
{channel="authz_audit"} |= "secret_rotation"
```

The actual write happens via a small `Log::channel('authz_audit')->info(...)`
call from the Laravel rotation artisan commands. If you rotate manually
(without using the artisan commands) — log the event by hand:

```bash
docker compose exec laravel-octane php artisan tinker
>>> Log::channel('authz_audit')->info('secret_rotation', ['credential' => 'POSTGRES_PASSWORD', 'actor' => 'kyle@example.com', 'reason' => 'scheduled-90d']);
>>> exit
```

## Cross-references

- `ops/runbooks/secret-management.md` — SOPS+age tooling.
- `ops/runbooks/log-retention.md` — where the rotation audit lives + how long.
- `.github/workflows/cd.yml` — deploy stages that consume the rotated env.
- `app/Console/Commands/CryptRotate.php` (TODO if not yet authored — V1.5 polish).
