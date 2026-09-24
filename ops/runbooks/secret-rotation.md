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
lives, which services read it, and the exact sequence that rotates it
without leaving a consumer on the old value. Ported to AWS on 2026-09-24;
the Azure Container Apps version this replaced is in git history
(`git log -- ops/runbooks/secret-rotation.md`), and the compose-era
version is in `_archived/` — neither's commands apply here.

The two procedures that touch encrypted data — `APP_KEY` and
`FASTAPI_SERVICE_KEY` — are owned by `docs/RUNBOOK.md`. This runbook says
how to *execute* them on ECS Fargate and what to roll afterwards; it does
not restate their internals.

```bash
CLUSTER=georag
```

Every command below assumes the shell already has AWS credentials for the
production account (`AWS_REGION` / `AWS_PROFILE`, or an assumed role) —
the same posture `scripts/operator/aws-preflight.sh` and
`deploy/aws/README.md` assume, and never AWS credentials pasted into chat.

---

## 0. How secrets are held here, and the three traps

**There is no Key Vault, no Bicep, and no per-app secret list.** One
Secrets Manager secret, `georag/app`, holds a single JSON object; the
execution role reads individual keys out of it by ARN
(`<arn>:<KEY_NAME>::`) and hands each task definition only the keys it
names (`deploy/aws/terraform/config.tf`, `_secret_ref` / `_extra_secret_ref`
/ `service_secrets`). The container never sees the ARN or the rest of the
JSON, only the value it was handed. `production.tfvars` is the record of
every *non-secret* value; `config.tf`'s top-of-file comment is the record
of every key `georag/app` is expected to hold, and
`scripts/check-ecs-secret-keys.py` fails CI if a key is referenced that is
undocumented, or documented and unreferenced.

Discover, never assume. Before rotating anything, find out which
*standing services* actually reference a key — a one-off task definition
(`georag-migrate`, `georag-app-key-rotation`) is not returned by
`list-services` and does not need restarting, because it reads the secret
fresh on every run rather than holding a task that was started earlier:

```bash
for svc in $(aws ecs list-services --cluster "$CLUSTER" --query 'serviceArns[]' --output text | tr '\t' '\n'); do
  td=$(aws ecs describe-services --cluster "$CLUSTER" --services "$svc" --query 'services[0].taskDefinition' --output text)
  echo "== ${svc##*/}"
  aws ecs describe-task-definition --task-definition "$td" \
    --query 'taskDefinition.containerDefinitions[].secrets[].{env:name,key:valueFrom}' --output table
done
```

`deploy/aws/rotation/rotate-hatchet-token.sh`'s `consumers()` function is
this exact loop, filtered to one key name — the reference implementation
of "discover, don't list from memory."

The three traps, each of which has already bitten this deployment:

1. **A secret change does not restart anything.** `put-secret-value`
   updates the store; a running task keeps the old value for its entire
   life, because ECS resolves a task's secrets once, at `RunTask`, and
   never again. Always follow a secret change with
   `aws ecs update-service --cluster georag --service <svc> --force-new-deployment`
   on every service the discovery loop names. There is no ECS equivalent
   of a stray "restart the wrong revision" — `force-new-deployment` always
   replaces the *running* tasks of that one service — but there is a
   slower failure in its place: a service mid-rollout when the secret
   changes can end up with old and new tasks both serving until the
   rollout finishes, which is the window every zero-downtime section below
   is written around.
2. **Never split "edit the JSON" from `put-secret-value`.** The
   2026-09-18 Hatchet-token rehearsal (`deploy/aws/README.md`, "Minting
   the real one") did exactly this: the edit step failed its own assertion
   and exited, the upload step ran anyway from the unmodified file, and
   AWS returned a new version id over identical content — which looks
   exactly like success with nothing to distinguish it from a real
   rotation. Chain the read, the edit and the write with a pipe (`jq ... |
   aws secretsmanager put-secret-value ...`), and read the value back
   before moving on.
3. **Never put a secret value on a command line.** Any value that lands in
   `argv` is readable by every other process on the machine for the
   life of the command (`ps`), and if the command is itself constructed
   from a variable, a stray `set -x` prints it to whatever the shell's
   `PS4` destination is. Build the new value in a shell variable, pipe it
   into `jq` through the **environment**, not `--arg` — `--arg` still
   copies the value into the process's argv on some shells' `jq` builds,
   `env` never does — and pipe `jq`'s output into
   `put-secret-value --secret-string file:///dev/stdin`. Never `echo` a
   secret, never paste one into chat, never `psql --set`. The generic
   shape, used by every section below:

```bash
set +x
NEW="$(openssl rand -base64 48 | tr -d '\n')"          # or the service's own generator
V="$NEW" jq -c '.SOME_KEY = env.V' \
  <(aws secretsmanager get-secret-value --secret-id georag/app --query SecretString --output text) \
  | aws secretsmanager put-secret-value --secret-id georag/app --secret-string file:///dev/stdin \
      --query VersionId --output text
unset NEW
for svc in <the services the discovery loop named>; do
  aws ecs update-service --cluster "$CLUSTER" --service "$svc" --force-new-deployment --query 'service.serviceName' --output text
done
aws ecs wait services-stable --cluster "$CLUSTER" --services <same list>
```

`Stable` (every task `RUNNING` and passing its health check, per
`aws ecs describe-services`) is the only acceptable end state. A service
stuck below its desired count means the new task could not start on the
new value — read
`aws logs tail /ecs/georag --since 10m --filter-pattern <service-name>`
and roll the secret back (put the old value, force-new-deployment again)
before anything else.

**Maintenance window.** RDS is stopped roughly 17:00–08:30
America/Vancouver by the nightly sweep (`ops/runbooks/aws-oncall.md` §0,
`deploy/aws/scheduler/`). Nothing in this runbook that touches a database
role works while the instance is stopped, and a service rolled during the
window boots against no database and looks broken. Rotate outside it.

---

## 1. Inventory

Key names below are exactly `deploy/aws/terraform/config.tf`'s and
`deploy/aws/README.md`'s "Step 3" table — the list `scripts/check-ecs-secret-keys.py`
enforces as exhaustive. "Read by" names the bare ECS service names
(`services.tf` sets `name = each.key`, unprefixed) whose task definition
actually injects the key, per `config.tf`'s `service_secrets` map — not
which service's application code happens to use it. That distinction
matters once, for `APP_KEY`: `_secret_ref` hands the five common keys to
every service in the *default* branch of `service_secrets` — the six
application-image services — regardless of whether that service's own
code reads the value, because `fastapi`, `hatchet-worker` and `sparse`
share one Pydantic `Settings` class with the two Python readers that do.

| Credential | Holder of truth | Read by (task definition) | Zero-downtime? | Section |
| --- | --- | --- | --- | --- |
| `APP_KEY` | `APP_KEY` key of `georag/app` | laravel-octane, laravel-horizon, laravel-reverb, fastapi, hatchet-worker, sparse (+ `georag-migrate`, `georag-app-key-rotation`); consumed only by the three laravel-* services | No — laravel-octane and laravel-horizon scaled to **zero** while `query_audit_log` is re-encrypted; scripted (`deploy/aws/rotation/rotate-app-key.sh`) | §2 |
| `FASTAPI_SERVICE_KEY` (+ `_KID`) | `georag/app` | laravel-octane, laravel-horizon, laravel-reverb, fastapi, hatchet-worker, sparse | Not currently — see §3 for the gap | §3 |
| `FASTAPI_SERVICE_KEY_PREVIOUS` (+ `_KID`) | documented in `config.tf`'s comment; **not wired into any ECS secret reference** | nobody, on AWS, today | n/a — see §3 | §3 |
| `GEORAG_APP_PASSWORD` | `georag/app` (role: `georag_app`) | laravel-octane, laravel-horizon, laravel-reverb, fastapi, hatchet-worker, sparse (as `DB_PASSWORD` / `POSTGRES_PASSWORD`) | Brief `28P01` on each consumer until rolled | §4 |
| RDS master password (`georag`) | AWS-managed Secrets Manager secret (`manage_master_user_password = true`, `data.tf:137`) — never in `georag/app` | operators only; no application connects as it | Yes — nothing to roll | §4 |
| `martin_readonly` password | embedded in `MARTIN_DATABASE_URL` | martin | Yes (order matters — see §4) | §4 |
| `hatchet` DB role password | embedded in `HATCHET_DATABASE_URL` | hatchet | No — the engine holds one long-lived connection | §4 |
| `REDIS_PASSWORD` | `georag/app` | laravel-octane, laravel-horizon, laravel-reverb, fastapi, hatchet-worker, sparse, redis (server) | Yes, via live ACL — see §5 for how without ECS Exec | §5 |
| `QDRANT_API_KEY` | `georag/app` (injected into qdrant as `QDRANT__SERVICE__API_KEY`) | laravel-octane, laravel-horizon, laravel-reverb, fastapi, hatchet-worker, sparse, qdrant | No — Qdrant holds one read-write key | §6 |
| ~~`AZURE_FOUNDRY_API_KEY`~~ | retired, ADR-0022 | — | — | §7 |
| ~~storage account key~~ | retired, ADR-0022 | — | — | §7 |
| `COHERE_API_KEY` | `georag/app` | fastapi, hatchet-worker | Not fully — see §8 | §8 |
| `HATCHET_CLIENT_TOKEN` | minted by the hatchet engine, stored in `georag/app` | fastapi, hatchet-worker, laravel-octane, laravel-horizon, laravel-reverb | Yes — old token stays valid until its own 90-day expiry (not revoked) | §9 |
| `HATCHET_ADMIN_PASSWORD` | `georag/app` (injected into hatchet as `ADMIN_PASSWORD`) | hatchet (only at first-boot seed time) | n/a — only applies when the seed *creates* the account | §10 |
| CloudFront origin secret (`X-Origin-Verify`) | operator-chosen Terraform input; mirrored into its own secret, `georag/cloudfront-origin-secret` | the ALB listener rules (not an ECS task) | Yes, scripted — see §11 | §11 |
| `REVERB_APP_SECRET` | `georag/app` | laravel-octane, laravel-horizon, laravel-reverb | Yes | §12 |
| `REVERB_APP_KEY` | `var.reverb_app_key` (Terraform variable, public by design) | laravel-octane, laravel-horizon, laravel-reverb, **the Vite bundle** | Key: needs an image rebuild | §12 |
| `AUDIT_ENCRYPTION_KEY` | not currently in `georag/app` | nobody — see §13 | n/a | §13 |
| `EXTERNAL_NOTIFICATION_HMAC_SECRET` | not currently in `georag/app` | nobody — see §13 | n/a | §13 |
| `ANTHROPIC_API_KEY` | not currently in `georag/app` | nobody — see §14 | n/a | §14 |
| Sanctum tokens / sessions | RDS / Redis | users | per-user | §15 |
| GitHub: `AWS_DEPLOY_ROLE_ARN`, `AWS_PRIVATE_SUBNET_IDS`, `AWS_TASK_SECURITY_GROUP_ID` | OIDC federated role + repo secrets | `cd.yml` | n/a — identifiers/config, not secrets, except the role trust itself | §16 |
| `FLOW_JWT_SECRET` | `georag/app` | fastapi, hatchet-worker | Yes | §17 |

Cadence (unchanged): `APP_KEY` annual; `FASTAPI_SERVICE_KEY`, `COHERE_API_KEY`
quarterly; Postgres, Redis, Qdrant keys annual; everything else on
compromise. Whatever the calendar says, rotate on suspected exposure
immediately.

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

## 3. `FASTAPI_SERVICE_KEY` — the shared Laravel ↔ FastAPI key, and a real gap

One value does three jobs (`docs/RUNBOOK.md` § "Key separation note"):
the `X-Service-Key` header, the HS256 signing key for the 60-second JWTs
Laravel mints, and the HMAC for `log_safe.query_hash`.

**The application code fully supports zero-downtime overlap; Terraform
does not wire it up.** `service_key_matches` (`src/fastapi/app/services/auth.py:49`)
accepts `FASTAPI_SERVICE_KEY_PREVIOUS` alongside the primary, and the
mirror-image check lives in `app/Http/Middleware/VerifyServiceKey.php`
(`services.fastapi.service_key_previous`) for Hatchet's `/internal/v1/*`
bridge calls and FastAPI's callbacks into Laravel. But
`deploy/aws/terraform/config.tf`'s `_secret_ref` map — the thing that
decides which Secrets Manager keys an ECS task definition is handed —
lists only `APP_KEY`, `FASTAPI_SERVICE_KEY`, `QDRANT_API_KEY`,
`HATCHET_CLIENT_TOKEN` and `REDIS_PASSWORD`. `FASTAPI_SERVICE_KEY_PREVIOUS`
and `FASTAPI_SERVICE_KEY_KID` are named in the file's own top comment as
expected keys and injected into **nothing** — confirmed by grepping every
`.tf` file in `deploy/aws/terraform/` for the string, which matches only
that one comment. There is no `deploy/aws/rotation/rotate-service-key.sh`,
either — unlike `APP_KEY`, `HATCHET_CLIENT_TOKEN` and the CloudFront
secret, this one has no script at all on AWS.

**What that means in practice: rotating `FASTAPI_SERVICE_KEY` on this
deployment today is a hard cutover, not the zero-downtime rotation the
application code was built for.** Writing the new value and
force-deploying every reader at once means every in-flight request signed
with the old key — and every task still rolling from the previous
revision — gets a 401 until its own task is replaced. On a service with
`desired = 2` (laravel-octane, laravel-reverb) that window is short but
real; on the `desired = 1` services (laravel-horizon, fastapi,
hatchet-worker) it is the time for one task to stop and a new one to pass
its health check.

Two ways to close the gap, in order of preference:

- **Wire `FASTAPI_SERVICE_KEY_PREVIOUS` / `_KID` into `config.tf`**, the
  same shape `_secret_ref` already uses, and let a normal `terraform
  apply` carry it. This is a code change, not a rotation step, and belongs
  in its own PR before the next scheduled rotation — do not improvise it
  live.
- **A manual out-of-band task-definition revision**, mirroring
  `cd.yml`'s smoke-test pattern (`.github/workflows/cd.yml:450-459`):
  `describe-task-definition`, add the extra `secrets` entries with `jq`,
  `register-task-definition`, `update-service --task-definition <new-rev>`.
  This works, but **the next `terraform apply` silently reverts it** —
  Terraform owns `aws_ecs_task_definition.this` and will re-register the
  revision it computes from `config.tf`, which does not have the extra
  keys. Anyone taking this path must finish the rotation and drop the
  overlap keys *before* the next apply, the same discipline the CloudFront
  script's tfvars warning enforces (§11).

Until one of those lands, treat this as a hard cutover and schedule it
for the lowest-traffic window available, same as any other change that
briefly 401s live traffic.

Generate (≥ 32 bytes is enforced on both sides at startup):

```bash
set +x
NEW="$(python3 -c 'import secrets; print(secrets.token_urlsafe(48))')"
```

Rotate (hard cutover — every reader at once):

```bash
V="$NEW" jq -c '.FASTAPI_SERVICE_KEY = env.V' \
  <(aws secretsmanager get-secret-value --secret-id georag/app --query SecretString --output text) \
  | aws secretsmanager put-secret-value --secret-id georag/app --secret-string file:///dev/stdin \
      --query VersionId --output text
unset NEW
for svc in laravel-octane laravel-horizon laravel-reverb fastapi hatchet-worker; do
  aws ecs update-service --cluster "$CLUSTER" --service "$svc" --force-new-deployment --query 'service.serviceName' --output text
done
aws ecs wait services-stable --cluster "$CLUSTER" --services laravel-octane laravel-horizon laravel-reverb fastapi hatchet-worker
```

`sparse` is intentionally excluded from the restart list: its task
definition carries the key (the shared `Settings` class requires it to be
present), but `sparse_service.py` never calls `verify_service_key`, so
restarting it buys nothing and only widens the window other services are
already open for.

Verify with the same smoke check CD runs, as a one-off task rather than an
`exec` — ECS Exec is not enabled on this cluster
(`deploy/aws/terraform/rotation.tf`'s header explains why, and it applies
here too):

```bash
def=$(aws ecs describe-task-definition --task-definition georag-fastapi --query taskDefinition)
new=$(echo "$def" | jq '
  .family = "georag-smoke"
  | .containerDefinitions[0].command = ["python3","/app/scripts/ops/post_deploy_smoke.py"]
  | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities, .registeredAt, .registeredBy)')
rev=$(aws ecs register-task-definition --cli-input-json "$new" --query 'taskDefinition.taskDefinitionArn' --output text)
NET=$(aws ecs describe-services --cluster "$CLUSTER" --services fastapi --query 'services[0].networkConfiguration.awsvpcConfiguration')
TASK=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$rev" --launch-type FARGATE \
  --network-configuration "$(jq -c '{awsvpcConfiguration:.}' <<<"$NET")" --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"
aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" --query 'tasks[0].containers[0].exitCode'
```

`fastapi-self` and `laravel-bridge` must both pass (exit 0 and the log
stream, `/ecs/georag`, `smoke/.../<task-id>`, carries no `[FAIL]` line).
Then watch for stragglers with CloudWatch Logs Insights:

```bash
aws logs start-query --log-group-name /ecs/georag \
  --start-time "$(date -u -d '30 minutes ago' +%s)" --end-time "$(date -u +%s)" \
  --query-string 'fields @timestamp, @logStream, @message
| filter @message like /401/ and (@message like /X-Service-Key/ or @message like /Unknown JWT kid/ or @message like /service.key/)
| stats count() by bin(5m), @logStream'
```

A steady stream after every service is `stable` means one consumer still
has the old value — re-run the discovery loop (§0).

`PROD_SMOKE_PROJECT_ID` is not set in `config.tf`'s `common_environment`,
so the answer-path check is skipped and the real query path is verified
by a user, not the probe.

---

## 4. Postgres — master, `georag_app`, `martin_readonly`, `hatchet`

`data.tf:103` provisions one RDS instance (`georag-pg`), single-AZ, with
`manage_master_user_password = true`. There is no PgBouncer in this
topology (CLAUDE.md's own inventory: "the AWS deployment has no pooler"),
so every connection is direct. Roles and who uses them (`deploy/aws/bootstrap.sql`):

| Role | LOGIN | Used by |
| --- | --- | --- |
| `georag` | yes | RDS master; owns every table; operators only, via `georag-migrate`'s `MIGRATE_DB_*` env and by hand |
| `georag_app` | yes | laravel-octane, laravel-horizon, laravel-reverb, fastapi, hatchet-worker, `georag-migrate` |
| `martin_readonly` | yes | martin only |
| `hatchet` | yes | hatchet (its own `hatchet` database, `SERVER_MSGQUEUE_KIND=postgres`) |
| `georag_read` / `georag_write` / `georag_audit` | **no** | grant-holders only; nothing connects as them |

**RDS master password — nothing to roll.** `manage_master_user_password`
means AWS generates and stores it in its own Secrets Manager secret
(`db_master_secret_arn` output; `data.tf:134-137`'s comment: "nothing in
this repo or in CI ever holds it"). No application connects as `georag` in
steady state — only `georag-migrate`, which reads
`MIGRATE_DB_PASSWORD` fresh from that AWS-managed secret on every run —
so there is no consumer to roll and no `georag/app` key to update.
Rotating it on demand, if ever needed, is AWS's own mechanism against its
own secret, not this runbook's `put-secret-value` pattern:

```bash
aws secretsmanager rotate-secret --secret-id "$(terraform -chdir=deploy/aws/terraform output -raw db_master_secret_arn)"
```

Both role rotations below connect as the master (`georag`) to run `ALTER
ROLE`, which needs its AWS-managed password — never typed, read once into
`PGPASSWORD` so it reaches `psql` through its environment rather than a
command-line flag or a prompt a screen-recorder could catch:

```bash
set +x
export PGPASSWORD="$(aws secretsmanager get-secret-value \
  --secret-id "$(terraform -chdir=deploy/aws/terraform output -raw db_master_secret_arn)" \
  --query SecretString --output text | jq -r .password)"
export PGHOST="$(terraform -chdir=deploy/aws/terraform output -raw db_endpoint)"
```

**The server never sees the password, only its SCRAM verifier.** RDS logs
statement text: `log_min_duration_statement` is set (`data.tf`), and any
statement that ERRORS is logged in full regardless. `ALTER ROLE ... PASSWORD
'<cleartext>'` would put the new password in the RDS log the moment it
failed. Postgres accepts a pre-computed `SCRAM-SHA-256$...` verifier in the
same position and stores it as-is, which is what `\password` does
internally. Define this once per shell (checked on 2026-09-24 against
PostgreSQL 16: the role logs in with the password, and a wrong one is
refused):

```bash
scram () {   # stdin: a password -> the SCRAM-SHA-256 verifier Postgres stores
  python3 -c '
import base64, hashlib, hmac, os, sys
pw = sys.stdin.read().encode()
salt, i = os.urandom(16), 4096
salted = hashlib.pbkdf2_hmac("sha256", pw, salt, i)
ck = hmac.new(salted, b"Client Key", "sha256").digest()
sk = hmac.new(salted, b"Server Key", "sha256").digest()
b = lambda x: base64.b64encode(x).decode()
print(f"SCRAM-SHA-256${i}:{b(salt)}${b(hashlib.sha256(ck).digest())}:{b(sk)}")'
}
```

**`georag_app`** — set the role password first (old sessions keep working;
new connections need the new password), then roll every consumer the
discovery loop names:

```bash
set +x
NEW="$(openssl rand -base64 32 | tr -d '=+/' | cut -c1-32)"
case "$NEW" in *[!A-Za-z0-9]*) echo "not alphanumeric, refusing"; exit 1;; esac
printf "ALTER ROLE georag_app LOGIN PASSWORD '%s';\n" "$(printf '%s' "$NEW" | scram)" \
  | psql "dbname=georag user=georag sslmode=require" \
      --set=ON_ERROR_STOP=1 --quiet --file -

V="$NEW" jq -c '.GEORAG_APP_PASSWORD = env.V' \
  <(aws secretsmanager get-secret-value --secret-id georag/app --query SecretString --output text) \
  | aws secretsmanager put-secret-value --secret-id georag/app --secret-string file:///dev/stdin \
      --query VersionId --output text
unset NEW
for svc in laravel-octane laravel-horizon laravel-reverb fastapi hatchet-worker; do
  aws ecs update-service --cluster "$CLUSTER" --service "$svc" --force-new-deployment --query 'service.serviceName' --output text
done
aws ecs wait services-stable --cluster "$CLUSTER" --services laravel-octane laravel-horizon laravel-reverb fastapi hatchet-worker
```

The SQL goes on stdin, not `--command` and not `--set`: `--command` does
not expand `:'var'`, and `--set` puts the password in `ps` output for every
user on the host. The alphanumeric check is what makes the `printf`
interpolation safe; keep them together.

Verify: every service `stable`, and no `28P01` in the logs:

```bash
aws logs start-query --log-group-name /ecs/georag \
  --start-time "$(date -u -d '15 minutes ago' +%s)" --end-time "$(date -u +%s)" \
  --query-string 'fields @timestamp, @logStream, @message | filter @message like /28P01/ | stats count() by @logStream'
```

**`martin_readonly`** — one role, one consumer, and the order matters:
Martin holds persistent connections, so the old password keeps working
until the task is replaced. There is no scripted equivalent of the Azure
`rotate-martin-credential.sh` on this deployment yet; by hand:

```bash
set +x
NEW="$(openssl rand -base64 32 | tr -d '=+/' | cut -c1-32)"
case "$NEW" in *[!A-Za-z0-9]*) echo "not alphanumeric, refusing"; exit 1;; esac
printf "ALTER ROLE martin_readonly LOGIN PASSWORD '%s';\n" "$(printf '%s' "$NEW" | scram)" \
  | psql "dbname=georag user=georag sslmode=require" \
      --set=ON_ERROR_STOP=1 --quiet --file -

# MARTIN_DATABASE_URL is a full connection string, not a bare password —
# read the current one, swap only the password segment, write it back.
CUR=$(aws secretsmanager get-secret-value --secret-id georag/app --query SecretString --output text | jq -r .MARTIN_DATABASE_URL)
NEWURL=$(printf '%s' "$CUR" | sed -E "s|://martin_readonly:[^@]+@|://martin_readonly:${NEW}@|")
V="$NEWURL" jq -c '.MARTIN_DATABASE_URL = env.V' \
  <(aws secretsmanager get-secret-value --secret-id georag/app --query SecretString --output text) \
  | aws secretsmanager put-secret-value --secret-id georag/app --secret-string file:///dev/stdin \
      --query VersionId --output text
unset NEW CUR NEWURL
aws ecs update-service --cluster "$CLUSTER" --service martin --force-new-deployment --query 'service.serviceName' --output text
aws ecs wait services-stable --cluster "$CLUSTER" --services martin
```

**`hatchet`'s own database role** follows the identical shape against
`HATCHET_DATABASE_URL`, restarting `hatchet` instead of `martin`. This one
is genuinely **not** zero-downtime: the engine holds one long-lived
connection for `SERVER_MSGQUEUE_KIND=postgres`, and `hatchet` runs
`desired = 1` with no overlap, so the engine — and every worker registered
against it — is unreachable for the seconds it takes the replacement task
to pass its health check.

---

## 5. `REDIS_PASSWORD`

redis runs `--requirepass "$REDIS_PASSWORD"` (`services.tf`'s
`service_command.redis`, run through `sh -c` for exactly one reason: the
env var has to expand, and ECS hands a command array to the container with
no shell). **Rolling the redis service loses everything in it** —
Horizon's queues, sessions, the dedupe windows — so do not restart it to
change the password. Redis ACLs let one user hold several passwords at
once, which is the zero-downtime path — but on Azure that ran the ACL
command *inside* the running container over `az containerapp exec`, and
**ECS Exec is disabled cluster-wide** (`deploy/aws/terraform/rotation.tf`'s
header; no `enable_execute_command` anywhere in `services.tf`, no
`ssmmessages` grant in `iam.tf`). There is no way to open a shell in the
live redis task.

The AWS-native substitute needs no shell in the target container: run a
**separate, one-off task from the same image**, in the same private
subnets and security group as the standing `redis` service, and have it
dial redis over the network (Cloud Map: `redis.<namespace>:6379`) instead
of a local socket. This is the same trick `deploy/aws/README.md` uses to
mint a Hatchet token — a fresh task from a vendor image, not a shell into
a running one.

**Neither password may travel in the `run-task` request.** CloudTrail
records `RunTask` request parameters, container overrides included, and
`describe-tasks` returns them for as long as the task is listed. So the
one-off task gets both values the way every service gets secrets, from
Secrets Manager at task start, using version stages: write the new value
first (Secrets Manager keeps the old one as `AWSPREVIOUS`), then give the
one-off task `REDIS_OLD` from `AWSPREVIOUS` and `REDIS_NEW` from
`AWSCURRENT`. The command override then names only variables.

Two consequences of that order. Between step 1 and step 2, a client task
that happens to start (a Spot replacement, an autoscale) reads the NEW
password before redis accepts it and fails AUTH until step 2 finishes, which
takes about a minute. Don't do this during a deploy. And **nothing else may
write `georag/app` until step 5**, or `AWSPREVIOUS` stops being the old
password.

```bash
set +x
CLUSTER=georag
SECRET_ARN=$(aws secretsmanager describe-secret --secret-id georag/app --query ARN --output text)
NS=$(aws servicediscovery list-namespaces --query "Namespaces[?Type=='DNS_PRIVATE'].Name | [0]" --output text)
NET=$(aws ecs describe-services --cluster "$CLUSTER" --services redis \
        --query 'services[0].networkConfiguration.awsvpcConfiguration')
NETCFG="awsvpcConfiguration={subnets=[$(jq -r '.subnets|join(",")' <<<"$NET")],securityGroups=[$(jq -r '.securityGroups|join(",")' <<<"$NET")],assignPublicIp=DISABLED}"

# 1. The new password into the secret. The old one stays as AWSPREVIOUS.
NEW="$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | head -c 40)"
aws secretsmanager get-secret-value --secret-id georag/app --query SecretString --output text \
  | V="$NEW" jq -c '.REDIS_PASSWORD = env.V' \
  | aws secretsmanager put-secret-value --secret-id georag/app --secret-string file:///dev/stdin \
      --query VersionId --output text
unset NEW

# 2. A one-off task definition: the redis image, both versions as secrets,
#    no volumes (it never touches redis's data directory).
REDIS_TD=$(aws ecs describe-services --cluster "$CLUSTER" --services redis --query 'services[0].taskDefinition' --output text)
aws ecs describe-task-definition --task-definition "$REDIS_TD" --query taskDefinition \
  | jq --arg arn "$SECRET_ARN" '
      .family = "georag-redis-acl"
      | .volumes = []
      | .containerDefinitions |= map(select(.name == "redis")
          | .mountPoints = [] | .portMappings = [] | del(.healthCheck)
          | .secrets = [{name: "REDIS_OLD", valueFrom: "\($arn):REDIS_PASSWORD:AWSPREVIOUS:"},
                        {name: "REDIS_NEW", valueFrom: "\($arn):REDIS_PASSWORD:AWSCURRENT:"}])
      | del(.taskDefinitionArn, .revision, .status, .requiresAttributes,
            .compatibilities, .registeredAt, .registeredBy, .deregisteredAt)' \
  > /tmp/redis-acl-td.json
ACL_TD=$(aws ecs register-task-definition --cli-input-json file:///tmp/redis-acl-td.json \
           --query 'taskDefinition.taskDefinitionArn' --output text)
rm -f /tmp/redis-acl-td.json

acl () {   # $1: the redis-cli command line, referring to $REDIS_OLD / $REDIS_NEW by name only
  local ovr task code
  ovr=$(jq -nc --arg cmd "$1" '{containerOverrides: [{name: "redis", command: ["sh", "-c", $cmd]}]}')
  task=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$ACL_TD" --launch-type FARGATE \
           --network-configuration "$NETCFG" --overrides "$ovr" --query 'tasks[0].taskArn' --output text)
  aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$task"
  code=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$task" --query 'tasks[0].containers[0].exitCode' --output text)
  echo "exit $code"; [ "$code" = "0" ]
}

# 3. Redis accepts both passwords.
acl "redis-cli -h redis.$NS -a \"\$REDIS_OLD\" --no-auth-warning ACL SETUSER default \">\$REDIS_NEW\" | grep -qx OK"

# 4. Every client onto the new one.
for svc in laravel-octane laravel-horizon laravel-reverb fastapi hatchet-worker; do
  aws ecs update-service --cluster "$CLUSTER" --service "$svc" --force-new-deployment --query 'service.serviceName' --output text
done
aws ecs wait services-stable --cluster "$CLUSTER" --services laravel-octane laravel-horizon laravel-reverb fastapi hatchet-worker

# 5. Retire the old password, and prove the new one works on its own.
acl "redis-cli -h redis.$NS -a \"\$REDIS_NEW\" --no-auth-warning ACL SETUSER default \"<\$REDIS_OLD\" | grep -qx OK"
acl "redis-cli -h redis.$NS -a \"\$REDIS_NEW\" --no-auth-warning PING | grep -qx PONG"
acl "! redis-cli -h redis.$NS -a \"\$REDIS_OLD\" --no-auth-warning PING | grep -qx PONG"

aws ecs deregister-task-definition --task-definition "$ACL_TD" --query 'taskDefinition.status' --output text
```

If step 3 exits non-zero, nothing has changed on redis. Put the old value
back (the `--rollback` pattern in §9: restore only the key, from
`AWSPREVIOUS`) before any client restarts.

The ACL change made by the one-off task is in the **live redis-server's
memory**, not on redis's own task definition — the two are independent
processes, and the one-off task exits as soon as `redis-cli` returns.
`REDIS_PASSWORD` in Secrets Manager is what the redis service's *next*
restart reads; step 1's `put-secret-value` is what makes that true without
restarting redis itself. Step 5 already proved the new password alone
works; also check Horizon is consuming: `aws logs tail /ecs/georag --filter-pattern laravel-horizon --since 5m`
should show supervisors starting on the new task.

---

## 6. `QDRANT_API_KEY`

qdrant reads `QDRANT__SERVICE__API_KEY` (config.tf's `service_secrets.qdrant`);
laravel-octane, laravel-horizon, laravel-reverb, fastapi, hatchet-worker
and sparse all hold `QDRANT_API_KEY` under that name. Qdrant holds exactly
one read-write key, so there is no overlap: clients fail from the moment
the qdrant service restarts on the new key until they restart too. Every
query refuses while that lasts (retrieval returns nothing, the guards do
their job). Data is on EFS and survives the roll — unlike the Azure SMB
share, rotating this never touches the mount.

```bash
set +x
NEW="$(openssl rand -base64 32 | tr -d '=+/')"
V="$NEW" jq -c '.QDRANT_API_KEY = env.V' \
  <(aws secretsmanager get-secret-value --secret-id georag/app --query SecretString --output text) \
  | aws secretsmanager put-secret-value --secret-id georag/app --secret-string file:///dev/stdin \
      --query VersionId --output text
unset NEW
aws ecs update-service --cluster "$CLUSTER" --service qdrant --force-new-deployment --query 'service.serviceName' --output text
aws ecs wait services-stable --cluster "$CLUSTER" --services qdrant
for svc in fastapi hatchet-worker laravel-octane laravel-horizon laravel-reverb; do
  aws ecs update-service --cluster "$CLUSTER" --service "$svc" --force-new-deployment --query 'service.serviceName' --output text
done
aws ecs wait services-stable --cluster "$CLUSTER" --services fastapi hatchet-worker laravel-octane laravel-horizon laravel-reverb
```

Qdrant first, clients immediately after — do it in the quiet hour before
the maintenance window, same reasoning as Azure. Verify with the
`post_deploy_smoke.py` one-off task (§3's pattern, `qdrant` check must
pass). If Qdrant errors persist after every service is `stable`, that is a
different incident (data missing from the collection, not the key) —
`refusal-rate-spike.md` §3.

---

## 7. Retired: the Foundry key and the storage account keys

Both are gone, per ADR-0022. Bedrock (Embed v4, Rerank 3.5) authenticates
with the ECS task role — no long-lived credential exists to rotate. S3
authenticates with the same task role, and presigned URLs (Laravel's
`temporaryUrl()`, export and figure downloads) are native to it, so the
Azure `allowSharedKeyAccess` workaround has no AWS successor either.
Nothing in `deploy/aws/terraform/` or in `georag/app` holds a Foundry key
or a storage key, and `aws-preflight.sh` has no check for one because
there is nothing to check.

---

## 8. `COHERE_API_KEY`

The one long-lived model-tier credential left, per ADR-0023: Command A+
chat (`LLM_BACKEND=cohere`) and Parse 5 OCR (`OCR_ENGINE=cohere_parse`)
both authenticate with it, against Cohere's own API rather than through
Bedrock. `config.tf`'s `_extra_secret_ref` hands it to exactly two
services — `fastapi` and `hatchet-worker`, each its own copy, because the
worker runs the parser in-process rather than calling out to FastAPI for
it. A worker without the key logs one `CRITICAL` and silently runs
`tesseract` on every scanned page instead, which extracts no tables and
raises nothing — check for `ocr_method='tesseract'` on newly ingested
pages if a rotation is suspected of having missed the worker.

Rotate in Cohere's dashboard first (issue the new key there — this is not
an AWS-generated value), then merge it in with the same read-modify-write
pipeline every other key uses, never splitting the edit from the upload:

```bash
set +x
NEW="<paste from Cohere's dashboard into the variable, never into a command>"
V="$NEW" jq -c '.COHERE_API_KEY = env.V' \
  <(aws secretsmanager get-secret-value --secret-id georag/app --query SecretString --output text) \
  | aws secretsmanager put-secret-value --secret-id georag/app --secret-string file:///dev/stdin \
      --query VersionId --output text
unset NEW
for svc in fastapi hatchet-worker; do
  aws ecs update-service --cluster "$CLUSTER" --service "$svc" --force-new-deployment --query 'service.serviceName' --output text
done
aws ecs wait services-stable --cluster "$CLUSTER" --services fastapi hatchet-worker
```

**This is not zero-downtime**, for the same reason as §3: there is no
`COHERE_API_KEY_PREVIOUS`, in the code or in Terraform, so a request that
lands on the old task after the new key exists but before that task is
replaced, or a request signed against Cohere after the old key is revoked
but before the new task is up, can fail. The window is one service
rollout, not a whole maintenance cycle, but it is real — plan the Cohere
console's revocation for *after* both services report `stable`, not
before.

Confirm the new key covers **both** models before revoking the old one —
a key entitled to chat but not Parse deploys cleanly and then sends every
scanned page to tesseract, which is exactly the failure mode with the
weakest signal (`aws-preflight.sh` A-11 wants a fresh report from each
probe):

```bash
COHERE_API_KEY="$NEW_FOR_VERIFICATION" bash ops/validation/cohere_probe.sh
git add ops/validation/reports/cohere_probe_*.json
git commit -m "chore(validation): re-run the Cohere wire-contract probe after key rotation"
```

Only once the probe passes and both services are `stable`, revoke the old
key in Cohere's dashboard.

---

## 9. `HATCHET_CLIENT_TOKEN`

A JWT issued by the Hatchet engine for the default tenant, read by
fastapi, hatchet-worker, laravel-octane, laravel-horizon and
laravel-reverb. Use `deploy/aws/rotation/rotate-hatchet-token.sh` rather
than repeating the mint-by-hand procedure in `deploy/aws/README.md`
("Minting the real one") — that procedure is what the script automates,
and it is what it was first run for real against, on 2026-09-24.

```bash
bash deploy/aws/rotation/rotate-hatchet-token.sh              # preview: consumers + the current token's claims
bash deploy/aws/rotation/rotate-hatchet-token.sh --apply       # mint, store, restart every consumer, verify
bash deploy/aws/rotation/rotate-hatchet-token.sh --rollback    # only if --apply's verification failed
```

**Both tokens stay valid at once, so `--apply` is genuinely
zero-downtime — but "zero-downtime" here means the roll, not the
lifecycle.** Hatchet tokens are independent signed JWTs; minting a new one
does not invalidate the old one. The script rolls every consumer onto the
new token and confirms the worker is finishing steps on it before it calls
the rotation done.

**The old token is not revoked, and nothing in this deployment can revoke
it.** Hatchet's own API refuses the operation for a bearer token calling
on its own behalf (`api/v1/server/authz`: "bearer tokens cannot read,
list, or write other bearer tokens") — revocation needs a signed-in
dashboard session, and the dashboard is not reachable from outside the VPC
(§10 explains why, and it is the same reason here). So the old token
simply keeps working, silently, alongside the new one, until it hits its
own 90-day expiry (`exp - iat` on the minted JWT is 7776000 seconds).
Nothing alarms on that expiry either — it is a fact about the JWT, not
something CloudWatch watches. The preview mode prints the current token's
expiry every time; read it.

**State lives in `~/.hatchet-token-rotation`** (`$STATE` in the script) —
the old token's `token_id` and expiry, not the token itself. Nothing
secret is in it. `--rollback` reads it to know what it is putting back,
and deletes it once rollback succeeds.

**Rollback restores only the token key.** It reads `AWSPREVIOUS` from
`georag/app`, extracts `HATCHET_CLIENT_TOKEN` from it, and merges that one
key into the *current* JSON — the same `jq -c '.HATCHET_CLIENT_TOKEN = env.T'`
pattern every section here uses, not a wholesale restore of the previous
secret version. If anything else wrote to `georag/app` between the
rotation and the rollback, that other change is preserved; only the token
reverts. If the previous version's token equals the current one (nothing
to roll back to) or holds no `HATCHET_CLIENT_TOKEN` at all, the script
refuses rather than writing something meaningless.

The token itself is never printed by this script. The mint task's only
output channel is its own CloudWatch log stream
(`hatchet/hatchet/<task-id>` under `/ecs/georag`); the script reads the
new token from there and deletes that stream immediately afterward, so it
does not sit in the logs the way the very first hand-minted token did on
2026-09-18 (`deploy/aws/README.md`, "The token reaches you through
CloudWatch Logs").

Hatchet's own server secrets (cookie and encryption keys, part of the
engine's own config) invalidate every client token when rotated; that is a
bigger operation and not covered here.

---

## 10. `HATCHET_ADMIN_PASSWORD`

hatchet-lite's entrypoint runs `hatchet-admin quickstart` on **every**
boot, and its seed creates a dashboard user, `admin@example.com`, whenever
that email is absent from the engine's database
(`cmd/hatchet-admin/cli/seed/seed.go`). The password it assigns is
`ADMIN_PASSWORD` — which Terraform injects from the `HATCHET_ADMIN_PASSWORD`
key of `georag/app` (`config.tf`'s `service_secrets.hatchet`) — or, if
that is unset, Hatchet's own **published default** (v0.91.2,
`pkg/config/database/config.go`).

**The seed only creates the account. It never updates an existing
password.** That single fact governs everything below: setting or
rotating `HATCHET_ADMIN_PASSWORD` changes nothing for a database whose
`admin@example.com` row already exists — the next boot's seed sees the
email is taken and moves on, ADMIN_PASSWORD or not.

**The production engine was seeded on 2026-09-18, before this key existed
in `georag/app`.** `HATCHET_ADMIN_PASSWORD` was added to Terraform on
2026-09-24. As of today, `admin@example.com` on the production engine
still has Hatchet's **published default password** — writing a value into
`georag/app` now and applying does not close this, for the reason above.

This exposure is scoped to the VPC only: port 8888 (the dashboard) is in
no ALB target group, and the task security group admits only sibling
tasks — nothing outside the VPC can reach the login page at all. Closing
it on the existing account means changing that password from inside the
dashboard itself (Settings → Profile), which needs a way to reach port
8888 that this deployment deliberately does not provide (no ECS Exec, no
target group, no bastion). **Whether to open one is an open decision for
the owner, not a step this runbook prescribes.**

Do not include the default value in any document, script, or chat
message, and do not log in with it as a way of "checking" — reaching the
dashboard at all requires deciding how, which is exactly the open
question above.

For a **fresh** engine — a new database, or after a deliberate reseed —
setting this before the first boot that creates the account is what makes
it take effect:

```bash
set +x
PW=$(python3 -c 'import secrets, string
a = string.ascii_letters + string.digits
while True:
    p = "".join(secrets.choice(a) for _ in range(40))
    if any(c.isupper() for c in p) and any(c.islower() for c in p) and any(c.isdigit() for c in p):
        print(p); break')
V="$PW" jq -c '.HATCHET_ADMIN_PASSWORD = env.V' \
  <(aws secretsmanager get-secret-value --secret-id georag/app --query SecretString --output text) \
  | aws secretsmanager put-secret-value --secret-id georag/app --secret-string file:///dev/stdin \
      --query VersionId --output text
unset PW
```

The validation rule (8–64 characters, an upper, a lower and a digit) is
enforced by the seed itself and checked without printing the value by
`aws-preflight.sh` A-15 — an invalid value aborts the seed **before** it
creates the default tenant, which leaves no tenant for
`HATCHET_CLIENT_TOKEN` to be minted against (§9). No `force-new-deployment`
follows this write on an existing engine: the hatchet service does not
need restarting for this key to matter, because restarting it changes
nothing on a database that already has the account.

---

## 11. CloudFront origin secret (`X-Origin-Verify`)

Not an ECS-task secret at all — it is a Terraform *input*
(`cloudfront_origin_secret` in `deploy/aws/terraform/edge.tf`), sensitive
but not generated by Terraform, and it governs the ALB security posture
rather than an application credential: the load balancer's security group
admits every CloudFront edge, not only this deployment's distribution, so
the listener refuses any request that lacks the header the distribution
adds (`services.tf`'s listener rules carry the same header condition).
Unset, the ALB accepts traffic from **any** CloudFront distribution, not
just this one.

Use the script — the ordering is the entire point, and doing it by hand
against a live ALB and a CloudFront distribution that takes minutes to
propagate is not something to improvise:

```bash
bash deploy/aws/rotation/rotate-cloudfront-origin-secret.sh            # preview: what exists, changes nothing
bash deploy/aws/rotation/rotate-cloudfront-origin-secret.sh --apply    # rotate
bash deploy/aws/rotation/rotate-cloudfront-origin-secret.sh --finish   # only if --apply stopped part-way
```

It widens every listener rule to accept old-and-new, waits for the
distribution to report `Deployed` on the new value, checks a real request
through CloudFront still succeeds, then narrows every rule to the new
value alone — never a window where a real request is refused.

**The new value's home is `georag/cloudfront-origin-secret`, a Secrets
Manager secret of its own — not `georag/app`.** `georag/app` holds values
ECS tasks read at start; this value is read by nothing at task start, only
by Terraform at `apply` time, and by the script itself. It is also the
**only** copy of the value when the platform is powered off:
`power = "off"` destroys the distribution and the ALB outright (§0's
maintenance-window note is about RDS stopping, not this — the ALB and
CloudFront have no stopped state at all), and `power = "on"` rebuilds them
from `production.tfvars`, which is why the next step matters.

**After `--apply` succeeds, update `production.tfvars`'s
`cloudfront_origin_secret` before the next `terraform apply`, or that
apply silently puts the old value back** — Terraform does not know the
script changed anything, and a `terraform apply` with the old tfvars value
present is itself a valid, successful apply that reintroduces the exposure
the rotation just closed:

```bash
aws secretsmanager get-secret-value --secret-id georag/cloudfront-origin-secret --query SecretString --output text
# put that value into production.tfvars's cloudfront_origin_secret, or:
export TF_VAR_cloudfront_origin_secret="$(aws secretsmanager get-secret-value --secret-id georag/cloudfront-origin-secret --query SecretString --output text)"
```

---

## 12. Reverb keys

`REVERB_APP_SECRET` is server-side (`_extra_secret_ref`: laravel-octane,
laravel-horizon and laravel-reverb all hold it) — rotate it with the
generic §0 pattern against all three. `REVERB_APP_KEY` is not a secret in
the usual sense — the browser receives it — and it is a Terraform
*variable* (`var.reverb_app_key`), not a Secrets Manager key. Rotating it
means **rebuilding the Laravel image**: `cd.yml` bakes
`VITE_REVERB_APP_KEY` into the Vite bundle from the `vars.VITE_REVERB_APP_KEY`
repository variable (`.github/workflows/cd.yml:167,197`), and that value
must match `var.reverb_app_key` in `production.tfvars` exactly — `cd.yml`
itself fails the deploy if the repository variable is unset
(`.github/workflows/cd.yml:199-200`). Change both, in the same change, and
let CD ship the rebuilt image. Until the new image is live the frontend
connects with the old key and every channel silently drops.

---

## 13. `AUDIT_ENCRYPTION_KEY` and `EXTERNAL_NOTIFICATION_HMAC_SECRET` — not provisioned

**Neither is currently a key in `georag/app`.** `deploy/aws/README.md`'s
Step 3 table — the exhaustive list `scripts/check-ecs-secret-keys.py`
enforces — does not name either, and no `.tf` file references them.

`AUDIT_ENCRYPTION_KEY` is the pgcrypto key `services/flow_jwt.py` would
use to encrypt the per-flow JWT keys in `workflow.flow_jwt_keys`
(`src/fastapi/app/services/flow_jwt.py:135` reads it via
`os.environ.get("AUDIT_ENCRYPTION_KEY", "")`, defaulting to empty). Unset,
as it is in production today, the module logs
`"flow_jwt: AUDIT_ENCRYPTION_KEY unset — no per-flow keys"` and every flow
runs with none. This is consistent with — not a regression from — §17's
finding that the integrations bridge `flow_jwt.py` guards has no caller in
this deployment at all: there is nothing to encrypt yet.

`EXTERNAL_NOTIFICATION_HMAC_SECRET` signs outbound notifications from
`app/hatchet_workflows/external_notification.py`; it has real code and no
Terraform wiring either.

If either capability is ever activated for AWS, provisioning the key
follows the same shape as every other addition to `_extra_secret_ref` —
name the reader(s) explicitly, add the key to `config.tf`'s comment and
the README's Step 3 table, and let `terraform apply` create the reference
before the first value is written. Until then there is nothing to rotate.

---

## 14. `ANTHROPIC_API_KEY` — not provisioned

Optional fallback LLM (`LLM_BACKEND=anthropic`), `fastapi` only
(`src/fastapi/app/config.py:458` defaults it to `""`). Like §13, it is not
a key in `georag/app` today — `LLM_BACKEND` is `"cohere"` in
`config.tf`'s `common_environment`, and nothing in Terraform names this
key. If it is ever added: issue a key in the Anthropic console, add
`ANTHROPIC_API_KEY` to `_extra_secret_ref` for `fastapi`, apply, then
rotate with the generic §0 pattern and delete the old key in the console.
Because `LLM_BACKEND` defaults away from it, a wrong or missing value is
invisible until the fallback path is actually exercised — confirm
`LLM_BACKEND` before assuming a rotation here matters at all.

---

## 15. Users: Sanctum tokens and sessions

Per-user, revoked rather than rotated. With ECS Exec disabled cluster-wide,
this runs the same way §3's smoke check does — a one-off task built from
the standing `georag-migrate` task definition (full Laravel env, correct
network placement) with its command overridden:

```bash
def=$(aws ecs describe-task-definition --task-definition georag-migrate --query taskDefinition)
new=$(echo "$def" | jq \
  --arg cmd "php artisan tinker --execute='App\\\\Models\\\\User::find(42)->tokens()->delete();'" '
  .family = "georag-oneoff"
  | .containerDefinitions[0].command = [$cmd]
  | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities, .registeredAt, .registeredBy)')
rev=$(aws ecs register-task-definition --cli-input-json "$new" --query 'taskDefinition.taskDefinitionArn' --output text)
NET=$(aws ecs describe-services --cluster "$CLUSTER" --services laravel-octane --query 'services[0].networkConfiguration.awsvpcConfiguration')
TASK=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$rev" --launch-type FARGATE \
  --network-configuration "$(jq -c '{awsvpcConfiguration:.}' <<<"$NET")" --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"
```

`georag-migrate`'s container entrypoint is `["/bin/sh","-c"]`
(`services.tf:654`), so the override above is one shell-string argument,
same as its default migration command — quote it accordingly, and never
put a live token value in the string (the example above deletes by user
ID, not by token).

Sessions live in Redis; a compromised session ends with the user's logout
or with §5 (a Redis roll, which ends everyone's).

---

## 16. GitHub and operator-side credentials

- **`AWS_DEPLOY_ROLE_ARN`** is the OIDC federated role `cd.yml` assumes
  (`aws-actions/configure-aws-credentials@v4`, `role-to-assume`) — an
  identifier naming a trust relationship, not a secret by itself. Rotate
  by changing the IAM role's trust policy (which repo/branch it trusts),
  not by generating a new value; there is no key to leak because OIDC
  issues short-lived tokens per run.
- **`AWS_PRIVATE_SUBNET_IDS`** and **`AWS_TASK_SECURITY_GROUP_ID`** are
  repository secrets holding `terraform output -raw private_subnet_ids`
  and `terraform output -raw task_security_group_id` — configuration, not
  credentials. Re-run those outputs and update the repo secrets whenever
  the VPC changes (a `power=off` → `power=on` cycle recreates both).
- **SOPS / age** (`scripts/operator/bootstrap-secrets.sh`,
  `.env.production.enc`) governs the pre-cutover, on-prem/k3s deployment
  model that `charts/georag/` still targets — it is not part of this AWS
  runbook, and nothing in the AWS deploy path reads it. `georag/app` in
  Secrets Manager is the equivalent record here, and §0's discovery loop
  is how it is kept honest rather than a separate encrypted file that can
  drift from what is actually deployed.
- ECR pulls use the tasks' execution role; nothing to rotate.

---

## 17. Dead and pending

- `FLOW_JWT_SECRET` (renamed from `KESTRA_FLOW_JWT_SECRET`, ADR-0022) has a
  reader but no caller. `services/flow_jwt.py` signs and verifies with it;
  nothing invokes the bridge, because Kestra — the integration edge it was
  built for — was removed 2026-07-28.
  It **is** in the AWS Secrets Manager key list, injected into `fastapi`
  and `hatchet-worker` only — the two services that import
  `services/flow_jwt.py` (`config.tf`'s `_extra_secret_ref`). Deliberately
  NOT in the common `_secret_ref` set every application service receives:
  it is an HS256 signing key, and laravel-*, the hatchet engine and the
  sparse model server have no use for it. Rotate it with the generic §0
  pattern against `fastapi` and `hatchet-worker` only, on compromise —
  there is no active caller to notice a stale value either way, which is
  exactly why a rotation here would go unverified without deliberately
  exercising `/admin/integrations/jwt-keys/rotate` first.
- Nothing rotates on a schedule. There is no reminder, no calendar hook and
  no expiry alert — except `HATCHET_CLIENT_TOKEN`'s own 90-day JWT expiry
  (§9), which is a property of the token, not an alarm. The cadences in §1
  are policy, not automation.

---

## 18. Record it

Every rotation gets a line in the `authz_audit` channel, which on ECS
lands in CloudWatch Logs (`/ecs/georag`) as JSON. With ECS Exec disabled,
write it the same way §15 revokes a token — a one-off task from
`georag-migrate`:

```bash
def=$(aws ecs describe-task-definition --task-definition georag-migrate --query taskDefinition)
new=$(echo "$def" | jq \
  --arg cmd 'php artisan tinker --execute='"'"'Log::channel("authz_audit")->info("secret_rotation", ["credential" => "REDIS_PASSWORD", "actor" => "<you>", "reason" => "scheduled"]);'"'"'' '
  .family = "georag-oneoff"
  | .containerDefinitions[0].command = [$cmd]
  | del(.taskDefinitionArn, .revision, .status, .requiresAttributes, .compatibilities, .registeredAt, .registeredBy)')
rev=$(aws ecs register-task-definition --cli-input-json "$new" --query 'taskDefinition.taskDefinitionArn' --output text)
NET=$(aws ecs describe-services --cluster "$CLUSTER" --services laravel-octane --query 'services[0].networkConfiguration.awsvpcConfiguration')
TASK=$(aws ecs run-task --cluster "$CLUSTER" --task-definition "$rev" --launch-type FARGATE \
  --network-configuration "$(jq -c '{awsvpcConfiguration:.}' <<<"$NET")" --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"
```

There is no `.env.production.enc` to keep in sync on this deployment
(§16) — `georag/app` in Secrets Manager already is the record, and §0's
discovery loop is what confirms it matches what every task actually
holds, the same way it always has.
