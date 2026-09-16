# Secret rotation — production (AWS since 2026-09-08)

> **⚠️ 2026-09-08 — production moved to AWS
> ([ADR-0022](../../docs/adr/0022-aws-replaces-azure-as-the-production-cloud.md)).**
> Every `az containerapp secret set` procedure below is HISTORY. Secrets
> now live in AWS Secrets Manager and are injected into ECS tasks by ARN by
> the execution role; a rotation is a `put-secret-value` plus a
> `force-new-deployment`, not a per-app secret update.
>
> Three credentials in this document NO LONGER EXIST, and that is the
> single biggest change:
>
> - the **Foundry API key** — Bedrock authenticates with the task role;
> - the **storage account key** — S3 does too, and presigned URLs are
>   native to it, which is why Azure's `allowSharedKeyAccess` (enabled
>   only because `temporaryUrl()` had no alternative) has no successor;
> - the **Azure Files account key** — Qdrant's storage was mounted with
>   it, so rotating it broke the mount on the next restart. EFS is
>   IAM-authorised and has no such hazard.
>
> What still rotates: `APP_KEY`, `FASTAPI_SERVICE_KEY` (with
> previous-key acceptance on both sides, which is what makes it
> zero-downtime), `QDRANT_API_KEY`, the Hatchet client token, the Redis
> password, and the `martin_readonly` database password. Their
> APPLICATION-side contracts — the order of operations, what accepts the
> previous value, what fails closed — are unchanged and are the reason
> this file is kept rather than deleted.

**Scope.** Every credential the production deployment holds, where it
lives, which apps read it, and the exact sequence that rotates it without
leaving a consumer on the old value. Written 2026-09-06 for the Azure
Container Apps posture; the compose-era version of this runbook is in
`_archived/` and none of its commands apply here.

The two procedures that touch encrypted data — `APP_KEY` and
`FASTAPI_SERVICE_KEY` — are owned by `docs/RUNBOOK.md`. This runbook says
how to *execute* them on Container Apps and what to roll afterwards; it
does not restate their internals.

```bash
az account set --subscription d314ab40-b5b7-4e3e-8308-86023fb7638a
RG=georag
```

---

## 0. How secrets are held here, and the three traps

**There is no Key Vault and no Bicep.** Each Container App carries its own
`secrets:` list, and an env var reads one as `secretRef:`. The values were
set by hand at the Azure lift and drift freely from
`.env.production.example`, which is a *template*, not the deployed state.
`.env.production.enc` (SOPS + age) is the operator's encrypted record of
what was chosen; **CD does not read it** — `cd.yml` authenticates with
OIDC, pushes images and runs `laravel-migrate-job`, nothing else. Keep the
record in sync after every rotation or it stops being one.

Discover, never assume. Before rotating anything, find out which apps
actually reference it:

```bash
for app in laravel-octane-cc laravel-horizon-cc laravel-reverb-cc fastapi-cc hatchet-cc hatchet-worker-cc qdrant-cc redis-cc martin-cc; do
  echo "== $app"
  az containerapp show -g $RG -n "$app" \
    --query "properties.template.containers[0].env[?secretRef!=null].{env:name,secret:secretRef}" -o table
done
az containerapp job show -g $RG -n laravel-migrate-job \
  --query "properties.template.containers[0].env[?secretRef!=null].{env:name,secret:secretRef}" -o table
```

The three traps, each of which has already bitten this deployment:

1. **A secret change does not restart anything.** `az containerapp secret
   set` updates the store; running replicas keep the old value until a new
   revision starts. Always follow a secret change with
   `az containerapp update --revision-suffix <unique>`. Do **not** use
   `revision restart`: with more than one revision marked active it
   restarts the wrong one, and a revision in `ActivationFailed` does not
   come back (measured 2026-08-25 on Container Apps; the script that measured it went with `deploy/azure/`).
2. **Never `--yaml` an app with a `secrets:` block in the file.** Sending
   `redis.yaml` verbatim sets the live `redis-password` to the literal
   `REPLACE_AT_DEPLOY_TIME` and every client fails auth. Use
   `apply-redis.sh`, which strips the block.
3. **Never put a secret value on a command line.** `--secrets name=value`
   is unavoidable for `az`, so build the value in a shell variable, run
   with `set +x`, and clear it afterwards. Never `echo` it, never paste it
   into a chat, never `psql --set`. `rotate-martin-credential.sh` is the
   reference implementation of doing this right.

The generic cycle, used by every section below:

```bash
set +x
NEW="$(openssl rand -base64 48 | tr -d '\n')"          # or the service's own generator
az containerapp secret set -g $RG -n <app> --secrets <secret-name>="$NEW" --output none
az containerapp update -g $RG -n <app> --revision-suffix "rot$(date -u +%y%m%d%H%M%S)" --output none
unset NEW
LATEST=$(az containerapp show -g $RG -n <app> --query properties.latestRevisionName -o tsv)
az containerapp revision show -g $RG -n <app> --revision "$LATEST" --query "{health:properties.healthState,running:properties.runningState}" -o table
```

`Healthy` is the only acceptable end state. `ActivationFailed` means the
new revision could not start on the new value — read
`az containerapp logs show -g $RG -n <app> --tail 50` and roll back the
secret before anything else.

**Maintenance window.** Postgres is stopped roughly 06:00–14:00 UTC by the
nightly saver (`aws-oncall.md` §0). Nothing in this runbook that touches
a database role works while it is `Stopped`, and a revision rolled during
the window boots against no database and looks broken. Rotate outside it.

---

## 1. Inventory

| Credential | Holder of truth | Read by | Zero-downtime? | Section |
| --- | --- | --- | --- | --- |
| `APP_KEY` | the `APP_KEY` key of the `georag/app` secret | laravel-octane, laravel-horizon, laravel-reverb (and the migrate + rotation tasks) | No — `laravel-octane` and `laravel-horizon` are scaled to **zero** while `query_audit_log` is re-encrypted; scripted (`deploy/aws/rotation/rotate-app-key.sh`) | §2 |
| `FASTAPI_SERVICE_KEY` (+ `_KID`, `_PREVIOUS`, `_PREVIOUS_KID`) | fastapi-cc secret | fastapi-cc (verify), laravel-octane-cc / laravel-horizon-cc (mint), hatchet-worker-cc (X-Service-Key to Laravel) | Yes, both directions (since 2026-09-06) — see §3 | §3 |
| Postgres admin (`georag_admin`) | Flexible Server | operators, `rotate-martin-credential.sh` | Yes | §4 |
| Postgres app roles (`georag_app`, `georag`) | Flexible Server role | laravel-*, laravel-migrate-job, fastapi-cc, hatchet-worker-cc, hatchet-cc (its own `hatchet` database) | Brief 28P01 on each consumer until rolled | §4 |
| `martin_readonly` | Flexible Server role | martin-cc | Yes (scripted) | §4 |
| `REDIS_PASSWORD` | redis-cc `redis-password` secret | laravel-*, fastapi-cc, hatchet-worker-cc | Yes, via live ACL | §5 |
| `QDRANT_API_KEY` | qdrant-cc `QDRANT__SERVICE__API_KEY` | fastapi-cc, hatchet-worker-cc | No — Qdrant holds one key | §6 |
| `AZURE_FOUNDRY_API_KEY` | `georag-foundry-cc` key1 / key2 | fastapi-cc, hatchet-worker-cc | Yes, two keys | §7 |
| Storage account key / `AZURE_STORAGE_CONNECTION_STRING` | `georagblobcc` key1 / key2 | laravel-* (SAS signing); fastapi-cc / hatchet-worker-cc only if not on managed identity | Yes, two keys | §8 |
| `HATCHET_CLIENT_TOKEN` | minted by hatchet-cc | fastapi-cc, hatchet-worker-cc | Yes — old token stays valid until revoked | §9 |
| `REVERB_APP_KEY` / `REVERB_APP_SECRET` | laravel-reverb-cc | laravel-octane-cc, laravel-horizon-cc, laravel-reverb-cc, **the Vite bundle** | Key: needs an image rebuild | §10 |
| `AUDIT_ENCRYPTION_KEY` | fastapi-cc / hatchet-worker-cc | per-flow JWT keys in `workflow.flow_jwt_keys` | No — re-issue every per-flow key | §11 |
| `EXTERNAL_NOTIFICATION_HMAC_SECRET` | hatchet-worker-cc | external senders | Coordinated re-issue | §11 |
| `ANTHROPIC_API_KEY` | fastapi-cc | fastapi-cc | Yes | §12 |
| Sanctum tokens / sessions | Postgres / Redis | users | per-user | §13 |
| GitHub: `AZURE_CLIENT_ID` etc. | Entra federated credential | cd.yml (OIDC) | n/a — identifiers, not secrets | §14 |
| GitHub: `SOPS_AGE_PRIVATE_KEY`, operator age key | age keys | the `.env.production.enc` record | Yes | §14 |
| `FLOW_JWT_SECRET` | fastapi-cc, hatchet-worker | `services/flow_jwt.py` (no caller reaches it today) | Yes | §15 |

Cadence (Appendix C §9, unchanged): `APP_KEY` annual; `FASTAPI_SERVICE_KEY`,
Foundry key quarterly; Postgres, Redis, Qdrant, storage keys annual;
everything else on compromise. Whatever the calendar says, rotate on
suspected exposure immediately.

---

## 2. `APP_KEY` (Laravel)

`APP_KEY` encrypts `query_audit_log` PII columns and keys
`query_text_hash`; rotating it without the data step makes every
encrypted row unreadable. The procedure and its recovery paths are in
`docs/RUNBOOK.md` § "APP_KEY rotation checklist".

### The procedure

```bash
cd deploy/aws/terraform
export ROTATE_SUBNETS="$(terraform output -raw private_subnet_ids)"
export ROTATE_SECURITY_GROUP="$(terraform output -raw task_security_group_id)"
cd -

bash deploy/aws/rotation/rotate-app-key.sh            # preflight + plan, mutates nothing
bash deploy/aws/rotation/rotate-app-key.sh --apply    # do it
```

Run it **inside the maintenance window**, after `startup-sweep.sh` has
brought RDS up — the script refuses to start against a stopped database,
because it rewrites every audit row. Do not run it across the nightly
boundary: the Hatchet `retention_sweep` deletes aged `query_audit_log`
rows, and a row deleted between the dump and the restore comes back only
as a "missing in DB" warning.

**The platform is down for the duration.** That is a deliberate change
from the Azure procedure and the reason is in the next section.

If it stops after the re-encryption but before every service has the new
key, finish it with `--finish`. That needs nothing from the machine that
started the rotation — not the key, not the task counts — because both are
in Secrets Manager until the run completes.

### What changed in the port to AWS, and why

The Azure script ran the dump and the restore **inside** the single live
`laravel-octane-cc` replica, over `az containerapp exec`, because the
plaintext dump has to sit on a disk between the two steps and the serving
container's was the only disk available. Three of the eight findings below
were consequences of that one constraint. They are gone, and the
constraint is gone with them: the re-encryption now runs in a one-off ECS
task (`deploy/aws/terraform/rotation.tf`), the same shape CD already uses
for migrations. No serving container ever holds plaintext PII.

**Maintenance mode was replaced by scaling to zero, not by an equivalent.**
`config/app.php:121` leaves `APP_MAINTENANCE_DRIVER` at `file`, so
maintenance state lives on one container's filesystem. Azure had exactly
one Octane replica and the rotation ran *in* it, so `php artisan down`
there covered every writer. `laravel-octane` runs **two** tasks here
(`main.tf:52`) and the rotation runs in neither, so there is no container
in which `down` would mean anything. Scaling to zero is also the only form
of "stopped writing" that can be *verified* from outside, by
`runningCount`, rather than inferred.

**The new key never reaches a terminal at all.** It is minted locally,
written straight into the `APP_KEY_NEXT` key of the `georag/app` secret,
and injected into the rotation task from there. Azure minted it inside the
replica and read it back over stdout, which is why finding 6 below has the
script write it 0600 to the operator's laptop — a shell variable was
otherwise its only copy. Secrets Manager is that copy now, with a 30-day
recovery window. Note that stdout would be *worse* here than it was on
Azure: an ECS task's stdout is CloudWatch Logs, which persists and is
readable by anyone with `logs:FilterLogEvents`.

**The recovery asset is an RDS snapshot, taken before anything changes.**
The script refuses to proceed without one. Azure's recovery asset was the
dump surviving on the replica's disk after a failed restore; a Fargate
task's disk does not survive, and a half-re-encrypted table **cannot be
re-dumped** — `audit:dump-pii` reads through the `encrypted` cast, so
`DumpAuditPii.php:186` fails the whole dump on the first row it cannot
decrypt. There is no key that reads a half-rotated ledger. Restoring a
snapshot is heavier than re-running a restore by hand; unlike it, it
always works.

### ⚠️ Finding 5 below was WRONG, on Azure as well

> **Horizon does not need pausing. Only the Octane query controller writes
> `query_audit_log`; Horizon only reads.**

It does write. `app/Jobs/StreamQueryFromFastApi.php` — a Horizon job —
writes `response_text`, an `encrypted` column, on three paths: completion
(`:418`), error (`:489-493`) and failed (`:538`). An in-flight stream job
finalising a row under the **old** key after the dump had already read it
leaves that row unreadable once the new key is promoted, with nothing to
notice it: no exception, no log line, one corrupted audit row per
unlucky query.

This was never a property of the cloud, so it was equally wrong on Azure
for the two days that script existed. `laravel-horizon` is now scaled to
zero alongside `laravel-octane`, and
`deploy/aws/rotation/tests/run.sh::outer_quiesces_both_writers_before_running_the_task`
fails if either is dropped.

`laravel-reverb` is rolled but **not** quiesced: it neither reads nor
writes `query_audit_log`, and stopping it would drop every open WebSocket
for no benefit.

### The rehearsal harness

`deploy/aws/rotation/tests/run.sh` — 32 cases against a fake `aws` and a
fake `php artisan`, no credentials, no network, no PHP. It runs in CI in
the `Nightly scheduler jobs` job.

**It was rehearsed against fakes, never against a real cloud.** There was
no staging environment on Azure and there is none now; the only AWS
account is production. Treat the first real run as the live rehearsal: do
it right after the maintenance window opens the database and before users
arrive.

The single most important case is
`inside_restore_runs_under_the_new_key`. A rotation that gets the key
order backwards re-encrypts every row to the key it already had, exits 0,
and is indistinguishable from success.

### The findings that shaped it

Each would break a by-hand run of the RUNBOOK's sequence. Findings 1, 6, 7
and 8 are about Laravel and the audit ledger and still bind exactly as
written; 2, 3 and 4 are recorded as history, because the constraint that
produced each is named above; 5 is corrected above.

1. **`php artisan audit:rotate-key` cannot work here.** Its step 3 is
   `key:generate --force`, which writes the new key into `.env`, and the
   image ships no `.env` (`.dockerignore` excludes it; `APP_KEY` arrives
   as an env var). It fails after the dump and before the restore. The
   in-task script instead runs the restore as
   `APP_KEY="$APP_KEY_NEXT" php artisan audit:restore-pii` — the same
   in-process rebind the orchestrator does, without the file write.
2. *(Azure history.)* **`php artisan down` would get the replica killed
   mid-rotation**, because the default 503 failed the `/up` liveness probe
   with the plaintext dump on its disk. Hence `down --status=200`. No
   longer applicable: nothing serves traffic during the re-encryption.
3. *(Azure history.)* **The dump and the restore had to happen in one exec
   session**, because a revision roll between them lost the dump. They are
   now two steps of one task.
4. *(Azure history.)* **`az containerapp exec` needed a TTY and its exit
   code meant nothing.** `ecs describe-tasks` reports the container's real
   exit code, so the exit code is the contract — and it is granular
   (10 = nothing changed, 20 = possibly half re-encrypted, 30 = rotated but
   not shredded), because the orchestrator has to tell those apart without
   reading logs.
5. **Corrected above — Horizon DOES write encrypted columns.**
6. **The key is durable before any secret is promoted.** Once the restore
   has run, the new key is the only thing that can read the audit rows.
   Azure wrote it 0600 to the operator's laptop because a dropped SSH
   session would otherwise have lost the data; here it is in Secrets
   Manager as `APP_KEY_NEXT` from before the re-encryption starts, which
   is the same guarantee without a copy on anyone's disk.
7. **A failed dump is reversed; a failed restore is not.** After a dump
   failure (exit 10) the script restores the task counts and removes the
   staging keys itself — nothing changed, so nothing is left for a human
   to remember. After a restore failure (exit 20) it leaves the services
   at zero, leaves `APP_KEY_NEXT` in place, and says so: a half-rotated
   ledger must not serve traffic, and the old key is no more able to read
   it than the new one.
8. **Never put the old key back after the rotation task exits 0.** Every
   row has been under the new key since that moment. The previous secret
   version is `AWSPREVIOUS` in `georag/app` and the old key is in it; it
   now reads nothing. The recovery for a bad roll is `--finish`, not a
   revert.

---

## 3. `FASTAPI_SERVICE_KEY` — the shared Laravel ↔ FastAPI key

One value does three jobs (`docs/RUNBOOK.md` § "Key separation note"):
the `X-Service-Key` header, the HS256 signing key for the 60-second JWTs
Laravel mints, and the HMAC for `log_safe.query_hash`.

**Zero-downtime in both directions since 2026-09-06.** Every internal
call carries two credentials and both now overlap. The JWT path keeps a
`kid → secret` map; the `X-Service-Key` path accepts
`FASTAPI_SERVICE_KEY_PREVIOUS` as well as the primary on *both* sides —
`app/services/auth.py::service_key_matches` for calls into FastAPI, and
`app/Http/Middleware/VerifyServiceKey.php` (`services.fastapi.service_key_previous`)
for Hatchet's `/internal/v1/*` bridge calls and FastAPI's callbacks into
Laravel. Before that day the header was compared against the primary alone
on each side, and this rotation was a 401 storm from the first restart to
the last. The rule that makes it clean now: **every verifier learns the
new key with the old one kept as `PREVIOUS`, before any caller starts
sending the new key.** fastapi-cc and the Laravel apps are both verifiers
and both callers, so they get the `PREVIOUS` treatment; hatchet-worker-cc
only calls, so it just gets the new value. The same env var name carries
the previous key on every app. Laravel never mints with it.

Generate (≥ 32 bytes is enforced on both sides at startup):

```bash
set +x
NEW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
KID="$(date -u +%Y-q%q 2>/dev/null || date -u +%Y-%m)"   # any short label distinct from the current kid
```

Discover the current kid and which secret names hold the key (§0 loop),
then rotate **verifier first, callers immediately after**:

```bash
# 1. fastapi-cc: new key primary, old key kept as PREVIOUS (JWT kid map and
#    the X-Service-Key header both honour it).
OLD="$(az containerapp secret show -g $RG -n fastapi-cc --secret-name fastapi-service-key --query value -o tsv)"
az containerapp secret set -g $RG -n fastapi-cc --secrets fastapi-service-key="$NEW" fastapi-service-key-previous="$OLD" --output none
az containerapp update -g $RG -n fastapi-cc \
  --set-env-vars "FASTAPI_SERVICE_KEY_KID=$KID" "FASTAPI_SERVICE_KEY_PREVIOUS=secretref:fastapi-service-key-previous" "FASTAPI_SERVICE_KEY_PREVIOUS_KID=primary" \
  --revision-suffix "rot$(date -u +%y%m%d%H%M%S)" --output none

# 2. The Laravel apps: new key primary, old key as PREVIOUS so the calls
#    hatchet-worker-cc and fastapi-cc still make with the old value keep
#    authenticating until step 3.
for app in laravel-octane-cc laravel-horizon-cc laravel-reverb-cc; do
  az containerapp secret set -g $RG -n "$app" --secrets fastapi-service-key="$NEW" fastapi-service-key-previous="$OLD" --output none
  az containerapp update -g $RG -n "$app" \
    --set-env-vars "FASTAPI_SERVICE_KEY_KID=$KID" "FASTAPI_SERVICE_KEY_PREVIOUS=secretref:fastapi-service-key-previous" \
    --revision-suffix "rot$(date -u +%y%m%d%H%M%S)" --output none
done

# 3. The pure caller.
az containerapp secret set -g $RG -n hatchet-worker-cc --secrets fastapi-service-key="$NEW" --output none
az containerapp update -g $RG -n hatchet-worker-cc --set-env-vars "FASTAPI_SERVICE_KEY_KID=$KID" \
  --revision-suffix "rot$(date -u +%y%m%d%H%M%S)" --output none
unset NEW OLD
```

Include laravel-reverb-cc only if its env carries the key; the §0 loop
tells you.

Replace `primary` in step 1 with whatever kid the *old* key was minted
under if it was not the default. Rolling laravel-horizon-cc restarts the
supervisors, which is what you want: a job mid-flight with a JWT under
the old key is re-queued rather than failing on a 401. fastapi-cc and each
Laravel app log one warning the first time a request authenticates with
the previous key; seeing it a day later means a consumer was missed.

Verify from inside the cluster — the same probe CD runs after a deploy:

```bash
az containerapp exec -g $RG -n fastapi-cc --command 'python3 /app/scripts/ops/post_deploy_smoke.py'
```

`laravel-bridge` and `fastapi-self` must both pass. Then watch for stragglers:

```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(30m)
| where ContainerAppName_s in ("fastapi-cc", "laravel-octane-cc", "laravel-horizon-cc", "hatchet-worker-cc")
| where Log_s has "401" and (Log_s has "X-Service-Key" or Log_s has "Unknown JWT kid" or Log_s has "service.key")
| summarize n = count() by ContainerAppName_s, bin(TimeGenerated, 5m)
```

A steady stream after all four revisions are `Healthy` means one consumer
still has the old value — re-run the discovery loop. After an hour clean,
drop the overlap:

```bash
az containerapp update -g $RG -n fastapi-cc --remove-env-vars FASTAPI_SERVICE_KEY_PREVIOUS FASTAPI_SERVICE_KEY_PREVIOUS_KID \
  --revision-suffix "rot$(date -u +%y%m%d%H%M%S)" --output none
az containerapp secret remove -g $RG -n fastapi-cc --secret-names fastapi-service-key-previous --output none
for app in laravel-octane-cc laravel-horizon-cc laravel-reverb-cc; do
  az containerapp update -g $RG -n "$app" --remove-env-vars FASTAPI_SERVICE_KEY_PREVIOUS \
    --revision-suffix "rot$(date -u +%y%m%d%H%M%S)" --output none
  az containerapp secret remove -g $RG -n "$app" --secret-names fastapi-service-key-previous --output none
done
```

`PROD_SMOKE_PROJECT_ID` on fastapi-cc makes the smoke probe also run a
real query; it is unset today, so the answer path is verified by a user,
not the probe.

---

## 4. Postgres — admin, application roles, `martin_readonly`

`georag-pg-cc` is an Azure Flexible Server; there is no PgBouncer. Roles
and who uses them:

| Role | Used by |
| --- | --- |
| `georag_admin` | operators, `rotate-martin-credential.sh`, anything needing `ALTER ROLE` |
| `georag_app` (RLS user) | fastapi-cc, hatchet-worker-cc, laravel-migrate-job — and, if its env says so, the Laravel apps |
| `georag` | whichever apps carry `DB_USERNAME=georag` / `POSTGRES_USER=georag` — check the env |
| `hatchet` | hatchet-cc, for its own `hatchet` database |
| `martin_readonly` | martin-cc only |

**Admin password** — server-side, no consumer to roll:

```bash
az postgres flexible-server update -g $RG -n georag-pg-cc --admin-password "$(openssl rand -base64 32 | tr -d '=+/')"
```

Put the value in your password manager first; `az` prints nothing back.

**Application role** — set the role password first (old sessions keep
working; new connections need the new password), then roll every
consumer. Which apps consume which role comes from the §0 discovery loop,
not from this table:

```bash
set +x
NEW="$(openssl rand -base64 32 | tr -d '=+/' | cut -c1-32)"
case "$NEW" in *[!A-Za-z0-9]*) echo "not alphanumeric, refusing"; exit 1;; esac
printf "ALTER ROLE georag_app WITH PASSWORD '%s';\n" "$NEW" \
  | psql "host=georag-pg-cc.postgres.database.azure.com dbname=georag user=georag_admin sslmode=require" --set=ON_ERROR_STOP=1 --quiet --file -
for app in fastapi-cc hatchet-worker-cc; do        # add the Laravel apps if they use this role
  az containerapp secret set -g $RG -n "$app" --secrets pg-app-password="$NEW" --output none
  az containerapp update -g $RG -n "$app" --revision-suffix "rot$(date -u +%y%m%d%H%M%S)" --output none
done
az containerapp job secret set -g $RG -n laravel-migrate-job --secrets pg-app-password="$NEW" --output none
unset NEW
```

The SQL goes on stdin, not `--command` and not `--set`: `--command` does
not expand `:'var'`, and `--set` puts the password in `ps` output for every
user on the host (both measured 2026-08-25). The alphanumeric check is what
makes the `printf` interpolation safe; keep them together.

Verify: each new revision `Healthy`, and no `28P01` in the logs:

```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(15m) and Log_s has "28P01"
| summarize n = count() by ContainerAppName_s
```

**`martin_readonly`** — one command, generates and stores the credential
without a human ever seeing it:

```bash
# NOT YET PORTED — the script went with deploy/azure/ on 2026-09-08.
# By hand: ALTER ROLE martin_readonly PASSWORD, put-secret-value on
# MARTIN_DATABASE_URL, then force-new-deployment on the martin service.
# The ORDER matters: Martin holds persistent connections, so the old
# password keeps working until the task is replaced.
```

---

## 5. `REDIS_PASSWORD`

redis-cc runs `--requirepass` from its `redis-password` secret. **Rolling
redis-cc loses everything in it** — Horizon's queues, sessions, the
dedupe windows — so do not roll it to change the password. Redis ACLs let
one user hold several passwords, which gives a zero-downtime path:

```bash
set +x
OLD="$(az containerapp secret show -g $RG -n redis-cc --secret-name redis-password --query value -o tsv)"
NEW="$(openssl rand -base64 32 | tr -d '=+/' | cut -c1-32)"

# 1. Add the new password to the running instance (both now work).
az containerapp exec -g $RG -n redis-cc --command "redis-cli -a '$OLD' --no-auth-warning ACL SETUSER default '>$NEW'"

# 2. Move every client to the new one.
for app in laravel-octane-cc laravel-horizon-cc laravel-reverb-cc fastapi-cc hatchet-worker-cc; do
  az containerapp secret set -g $RG -n "$app" --secrets redis-password="$NEW" --output none
  az containerapp update -g $RG -n "$app" --revision-suffix "rot$(date -u +%y%m%d%H%M%S)" --output none
done

# 3. Make it survive the next redis-cc restart, and retire the old one.
az containerapp secret set -g $RG -n redis-cc --secrets redis-password="$NEW" --output none
az containerapp exec -g $RG -n redis-cc --command "redis-cli -a '$NEW' --no-auth-warning ACL SETUSER default '<$OLD'"
unset OLD NEW
```

Step 3's `secret set` does not restart redis-cc (that is the point). The
ACL change is in memory only; the secret is what the next restart reads.
Do both. Verify with `redis-cli -a "$NEW" PING` through `exec`, and check
Horizon is consuming: the `laravel-horizon-cc` logs should show
supervisors starting on the new revision.

---

## 6. `QDRANT_API_KEY`

qdrant-cc reads `QDRANT__SERVICE__API_KEY`; fastapi-cc and hatchet-worker-cc
send it. Qdrant holds exactly one read-write key, so there is no overlap:
the clients fail from the moment qdrant-cc restarts on the new key until
they restart too. Every query refuses while that lasts (retrieval returns
nothing, the guards do their job). Do it in the quiet hour before the
maintenance window, server first, clients immediately after. Data is on
the SMB share and survives the roll.

```bash
set +x
NEW="$(openssl rand -base64 32 | tr -d '=+/')"
az containerapp secret set -g $RG -n qdrant-cc --secrets qdrant-api-key="$NEW" --output none
az containerapp update -g $RG -n qdrant-cc --revision-suffix "rot$(date -u +%y%m%d%H%M%S)" --output none
for app in fastapi-cc hatchet-worker-cc; do
  az containerapp secret set -g $RG -n "$app" --secrets qdrant-api-key="$NEW" --output none
  az containerapp update -g $RG -n "$app" --revision-suffix "rot$(date -u +%y%m%d%H%M%S)" --output none
done
unset NEW
az containerapp exec -g $RG -n fastapi-cc --command 'python3 /app/scripts/ops/post_deploy_smoke.py'   # `qdrant` must pass
```

If `georag_qdrant`-style errors persist after all three are `Healthy`, the
`QDRANT_PARTIAL_LOSS` marker in the hatchet-worker-cc logs tells you
whether the collection itself is missing points (that is a different
incident — `refusal-rate-spike.md` §3).

---

## 7. `AZURE_FOUNDRY_API_KEY`

`georag-foundry-cc` has two keys. Rotate by switching consumers to the
other one, then regenerating the one they left:

```bash
set +x
az cognitiveservices account keys list -g $RG -n georag-foundry-cc --query "{k1:key1,k2:key2}" -o json >/dev/null   # confirm access; do not print
NEW="$(az cognitiveservices account keys list -g $RG -n georag-foundry-cc --query key2 -o tsv)"
for app in fastapi-cc hatchet-worker-cc; do
  az containerapp secret set -g $RG -n "$app" --secrets azure-foundry-api-key="$NEW" --output none
  az containerapp update -g $RG -n "$app" --revision-suffix "rot$(date -u +%y%m%d%H%M%S)" --output none
done
unset NEW
az cognitiveservices account keys regenerate -g $RG -n georag-foundry-cc --key-name key1 --output none
```

Next time, swap the roles of key1 and key2. This key fronts the LLM,
Embed v4, Rerank v4 **and** Cohere Parse v5, so a wrong value shows up as
Foundry `ClientErrors` on every path at once (`aws-oncall.md` §4) and as
`ocr_method='tesseract'` on newly ingested scanned pages.

---

## 8. Storage account keys and `AZURE_STORAGE_CONNECTION_STRING`

`georagblobcc` keeps shared-key access on because Laravel's
`temporaryUrl()` signs export and figure download URLs with the account
key (historical; see ADR-0022). FastAPI and the Hatchet worker use the
key only if their env carries `AZURE_STORAGE_CONNECTION_STRING` rather than
`AZURE_STORAGE_ACCOUNT_URL` (managed identity via `DefaultAzureCredential`,
`src/georag_object_storage/.../azure_config.py`) — check with the §0 loop.
Two keys, same dance as Foundry:

```bash
set +x
CONN="$(az storage account show-connection-string -g $RG -n georagblobcc --key secondary --query connectionString -o tsv)"
for app in laravel-octane-cc laravel-horizon-cc; do              # plus fastapi-cc / hatchet-worker-cc if they hold the string
  az containerapp secret set -g $RG -n "$app" --secrets azure-storage-connection-string="$CONN" --output none
  az containerapp update -g $RG -n "$app" --revision-suffix "rot$(date -u +%y%m%d%H%M%S)" --output none
done
unset CONN
az storage account keys renew -g $RG -n georagblobcc --key primary --output none
```

**The qdrant-cc SMB share mounts with the account key too** (it is an
environment-level storage mount). Renewing the key that mount was created
with breaks the mount on the next qdrant-cc restart. Before renewing,
check which key the environment storage uses and update it:

```bash
az containerapp env storage list -g $RG -n georag-env-cc -o table
az containerapp env storage set -g $RG -n georag-env-cc --storage-name <name> --azure-file-account-name georagblobcc \
  --azure-file-account-key "$(az storage account keys list -g $RG -n georagblobcc --query "[?keyName=='key2'].value" -o tsv)" \
  --azure-file-share-name qdrant-storage --access-mode ReadWrite --output none
```

Then roll qdrant-cc so it remounts, and only then renew the old key.
Skipping this is how a key rotation turns into a vector-store outage.

---

## 9. `HATCHET_CLIENT_TOKEN`

A JWT issued by hatchet-cc for the default tenant, read by fastapi-cc and
hatchet-worker-cc. Old tokens stay valid until they expire or are revoked,
so this one is genuinely zero-downtime: mint, roll consumers, revoke.

```bash
TENANT="$(psql "host=georag-pg-cc.postgres.database.azure.com dbname=hatchet user=georag_admin sslmode=require" -tA -c "SELECT id FROM \"Tenant\" WHERE slug='default'")"
set +x
NEW="$(az containerapp exec -g $RG -n hatchet-cc --command "/hatchet-admin --config /config token create --name georag-worker-$(date -u +%Y%m%d) --tenant-id $TENANT" 2>/dev/null | tr -d '\r' | grep -o 'ey[A-Za-z0-9_.-]*' | tail -1)"
[ -n "$NEW" ] || { echo "no token minted"; exit 1; }
for app in fastapi-cc hatchet-worker-cc; do
  az containerapp secret set -g $RG -n "$app" --secrets hatchet-client-token="$NEW" --output none
  az containerapp update -g $RG -n "$app" --revision-suffix "rot$(date -u +%y%m%d%H%M%S)" --output none
done
unset NEW
```

The `hatchet` database name and the `/hatchet-admin --config /config`
path are the compose-era values (`docker-compose.yml` § hatchet-worker,
Appendix K); confirm them against the running hatchet-cc image the first
time. Revoke the old token from the Hatchet dashboard (Settings → API
tokens) once the worker logs show `finished step run:` lines again
(`aws-oncall.md` §3 query). A worker on a bad token does not crash — it
sits Running and consumes nothing, which is exactly that section's symptom.

Hatchet's own server secrets (cookie and encryption keys in hatchet-cc)
invalidate every client token when rotated; that is a bigger operation
and not covered here.

---

## 10. Reverb keys

`REVERB_APP_SECRET` is server-side (laravel-octane-cc, laravel-horizon-cc,
laravel-reverb-cc): generic cycle on all three. `REVERB_APP_KEY` is not a
secret in the usual sense — the browser sends it — but rotating it means
**rebuilding the Laravel image**: `cd.yml` bakes `VITE_REVERB_APP_KEY` into
the Vite bundle as a build-arg literal (line ~263). Change it there, set
the same value on the three apps, and let CD ship the rebuilt image. Until
the new image is live the frontend will connect with the old key and every
channel silently drops (§07 "Env trap").

---

## 11. `AUDIT_ENCRYPTION_KEY` and `EXTERNAL_NOTIFICATION_HMAC_SECRET`

`AUDIT_ENCRYPTION_KEY` is the pgcrypto key for the per-flow JWT keys in
`workflow.flow_jwt_keys` (`services/flow_jwt.py` sets it as the
`app.audit_encryption_key` GUC per transaction). There is no re-encrypt
tool. Rotating it makes every stored per-flow key unreadable, and
`FlowKeyLookupError` is deliberately **not** swallowed — integrations
authenticating on `/internal/v1/integrations/*` get 401s until each flow's
key is re-issued under the new value. So: set the new secret on fastapi-cc
and hatchet-worker-cc and roll, then rotate every flow at
`/admin/integrations/jwt-keys/rotate`, then let `flow_jwt_key_reaper`
(nightly) delete the unreadable ones. On compromise only (Appendix C §9).

`EXTERNAL_NOTIFICATION_HMAC_SECRET` signs outbound notifications from
hatchet-worker-cc; every external receiver verifies with the same value,
so rotation is a coordinated re-issue with them, not a solo change.

---

## 12. `ANTHROPIC_API_KEY`

Optional fallback LLM, fastapi-cc only. Issue a new key in the Anthropic
console, generic cycle on fastapi-cc, delete the old key in the console.
`LLM_BACKEND=azure` is the default, so a wrong value is invisible until
the fallback is exercised — check `az containerapp show -n fastapi-cc
--query "properties.template.containers[0].env[?name=='LLM_BACKEND']"`
before assuming it matters.

---

## 13. Users: Sanctum tokens and sessions

Per-user, revoked rather than rotated:

```bash
az containerapp exec -g $RG -n laravel-octane-cc --command "php artisan tinker --execute 'App\\Models\\User::find(42)->tokens()->delete();'"
```

Sessions live in Redis; a compromised session ends with the user's logout
or with §5 (a Redis roll, which ends everyone's).

---

## 14. GitHub and operator-side credentials

- `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` are OIDC
  identifiers, not secrets. CD's credential is the federated credential on
  the Entra app trusting this repo's `main`; rotate it there if the trust
  is wrong, not in GitHub.
- `SOPS_AGE_PRIVATE_KEY` (CI) and the operator key under
  `~/.config/georag/` guard the `.env.production.enc` record.
  `age-keygen` a new pair, add the recipient to `.sops.yaml`,
  `sops updatekeys .env.production.enc`, push the CI key with
  `scripts/operator/set-github-secrets.sh`, then drop the old recipient and
  `updatekeys` again. `scripts/operator/bootstrap-secrets.sh` is idempotent
  and does the first half.
- ACR pulls use the apps' managed identities; nothing to rotate.

---

## 15. Dead and pending

- `FLOW_JWT_SECRET` (renamed from `KESTRA_FLOW_JWT_SECRET` on 2026-09-14,
  ADR-0022) has a reader but no caller. `services/flow_jwt.py` signs and
  verifies with it; nothing invokes the bridge, because Kestra — the
  integration edge it was built for — was removed 2026-07-28.
  **Corrected:** the previous entry here said it "has no consumer", which
  was wrong in a way that mattered — a consumer with no caller is not a
  dead variable, and deleting it would have broken the bridge the moment
  anything called it.
  Two things follow. Pydantic runs with `extra="forbid"`, so the OLD name
  left in a deployed `.env` is now a startup crash naming the new one —
  delete it, do not keep both. And `scripts/check_settings_have_readers.py`
  guards the other direction: the field cannot be removed while
  `flow_jwt.py` reads it.
  It **is** in the AWS Secrets Manager key list as of 2026-09-14
  (`deploy/aws/terraform/config.tf`, `_extra_secret_ref`), injected into
  `fastapi` and `hatchet-worker` only — the two services that import
  `services/flow_jwt.py`. It is deliberately NOT in the common
  `_secret_ref` set every task receives: it is an HS256 signing key, and
  laravel-*, the hatchet engine and the sparse model server have no use
  for it. Until that date it was absent entirely while compose marked it
  `${VAR:?}` required, so dev could not start without it and production
  ran without it — the field defaults to `""`, so FastAPI started anyway
  and the bridge would have raised 500 on first use.
- `AZURE_DOCUMENT_INTELLIGENCE_*` / the `docintel-*` secret refs on
  hatchet-worker-cc are dead since ADR-0019; remove them, they are not
  rotated.
- Nothing rotates on a schedule. There is no reminder, no calendar hook and
  no expiry alert; the cadences in §1 are policy, not automation.

---

## 16. Record it

Every rotation gets a line in the `authz_audit` channel, which on Azure
lands in `ContainerAppConsoleLogs_CL` as JSON:

```bash
az containerapp exec -g $RG -n laravel-octane-cc --command "php artisan tinker --execute 'Log::channel(\"authz_audit\")->info(\"secret_rotation\", [\"credential\" => \"REDIS_PASSWORD\", \"actor\" => \"<you>\", \"reason\" => \"scheduled\"]);'"
```

Then update `.env.production.enc` (`sops .env.production.enc`) so the
encrypted record matches what is deployed, and commit it. An encrypted
record that lags the portal is the state this deployment was found in on
2026-08-22; the whole point of §0's discovery loop is that nobody should
have to rely on it.
