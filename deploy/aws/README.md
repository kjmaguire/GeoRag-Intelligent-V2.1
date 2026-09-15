# deploy/aws

Production infrastructure for GeoRAG on AWS (ADR-0022, 2026-09-08).

The difference from `deploy/azure/` that matters most: **this deploys
itself.** The Azure tree was hand-applied resource definitions — its README
opened by saying nothing in it was touched by CI or CD — and there was no
Bicep, Terraform or ARM template for the container apps at all, so ~55
environment variables per app were set in the portal and drifted freely
from `.env.production.example`. Nothing running could be diffed against
anything in the repository. Starting from a blank cloud is the one chance
not to repeat that.

## Layout

| Path | What it is |
| --- | --- |
| `terraform/` | Everything: VPC, ALB, ECS, RDS, EFS, S3, ECR, IAM, Secrets Manager, EventBridge Scheduler, CloudWatch |
| `scheduler/` | The two nightly sweep scripts, embedded into task definitions by `terraform/scheduler.tf` |
| `scheduler/tests/` | Behavioural tests for the sweeps, run against a fake `aws` CLI |
| `rotation/` | The `APP_KEY` rotation, and the script it runs inside a one-off task (`terraform/rotation.tf`) |
| `rotation/tests/` | Behavioural tests for the rotation, against a fake `aws` CLI and a fake `php artisan` |

```bash
cd deploy/aws/terraform
terraform init
terraform plan -var-file=production.tfvars
```

`production.tfvars` is not in the repository. The variables with no default
are the ones a deployment must decide: `acm_certificate_arn`, `app_domain`,
`reverb_app_key`, `alert_email`, and the two Bedrock Marketplace endpoint
names.

`app_domain` is the bare public hostname — no scheme, no path — and must be
a name on `acm_certificate_arn`. It drives `APP_URL`, and through it
Sanctum's stateful-domain list, and it is the Reverb WebSocket origin
allowlist.

`reverb_app_key` needs saying once, because it is one value that has to be
set identically in two unrelated places:

| Where | How it gets there |
| --- | --- |
| The Reverb server and the two publishers | `reverb_app_key` in this tfvars |
| The browser bundle | the `VITE_REVERB_APP_KEY` **repository variable**, baked in by CD |

Its secret half, `REVERB_APP_SECRET`, goes into Secrets Manager out of band
with the other application secrets. The key is public by design
(`config/reverb.php`) — the browser receives it — so it is a repository
variable rather than a secret; the secret is the half that authorises
publishing. If the bundle's key and the server's key disagree the chat
stream simply never connects, so CD fails the build when the variable is
unset rather than shipping a bundle with `key: undefined`.

## Step 0, before anything else

**Confirm in-region that Cohere Command A+ and Parse 5 are subscribable in
Bedrock Marketplace, and that Embed v4 and Rerank 3.5 are enabled
serverless.** The entire model tier rests on this and it could not be
verified from the session that wrote it:

```bash
aws bedrock list-foundation-models --region "$BEDROCK_REGION" \
  --query 'modelSummaries[?providerName==`Cohere`].[modelId,modelName]' --output table
```

If Command A+ or Parse 5 are not available, the fallback is the option Kyle
declined: chat and parse on `api.cohere.com`, embeddings and reranking left
on Bedrock. ADR-0022 §11 records that as the escape hatch rather than a
redesign.

Then run the wire-contract probe and commit its report before trusting any
adapter. Cohere Parse's wire shape has **never** been empirically verified,
on Foundry or on Bedrock, and the chat path's three confirmed Foundry
behaviours do not carry over by assumption.

## Step 1: bootstrap the database by hand, once

`docker/postgresql/init/*.sql` runs from `/docker-entrypoint-initdb.d/` on a
fresh compose volume and nowhere else. Ch 02 §1.2 records that those scripts
"never run on Azure"; they will not run on RDS either. Everything they create
that neither `migrate` nor `db:apply-raw` creates has to be applied once, as
the master user:

```bash
psql -h "$(terraform -chdir=deploy/aws/terraform output -raw db_endpoint)" \
     -U georag -d georag -f deploy/aws/bootstrap.sql
```

Getting this wrong does not fail loudly. The extensions are the visible half.
The other half is the Hatchet engine's own role and database — Hatchet runs
with `SERVER_MSGQUEUE_KIND=postgres`, so that database is both its schema
store and its message queue, and without it the engine starts, fails to
migrate, and the worker registers nothing: 51 workflows and 29 crons quietly
do not exist while every container reports healthy.

`bootstrap.sql` says what it deliberately leaves to something else, and why.

## Step 2: set the application role's password

`database/raw/phase1/10-georag-app-role.sql` creates `georag_app` with a
placeholder password that is committed to this repository
(`georag-app-dev-replace-via-alter-role`). The role name says what to do with
it. `db:apply-raw` will not change it on a re-run, because the whole `CREATE
ROLE` sits behind `IF NOT EXISTS`, so this is a real step and not a formality:

```bash
psql -h "$(terraform -chdir=deploy/aws/terraform output -raw db_endpoint)" \
     -U georag -d georag \
     -c "ALTER ROLE georag_app PASSWORD '<the value you put in GEORAG_APP_PASSWORD>';"
```

Every application container connects as `georag_app`, never as `georag`.
`georag` is the RDS master and owns every table, and `ENABLE ROW LEVEL
SECURITY` does not apply to a table's owner — only `FORCE` does. Connecting
the app as the owner would make every tenancy guarantee in the platform
depend on `FORCE` having reached every table without exception.

## Step 3: write the secrets, by exact key

Terraform creates `georag/app` in Secrets Manager and deliberately does not
populate it: values in `terraform apply` are values in Terraform state. The
single secret holds a JSON object, and each container is handed individual
keys out of it by the execution role.

These are the keys, and the list is exhaustive — `scripts/check-ecs-secret-keys.py`
fails CI if the Terraform references a key that is not documented here, or if
this table names one nothing reads.

| Key | Read by | What it is |
| --- | --- | --- |
| `APP_KEY` | the three Laravel services | Laravel encryption key, `base64:`-prefixed. Rotation has its own runbook below. |
| `GEORAG_APP_PASSWORD` | every application service | `georag_app`'s password, as set in Step 2. Injected twice, as `DB_PASSWORD` for Laravel and `POSTGRES_PASSWORD` for the Python services. |
| `FASTAPI_SERVICE_KEY` | every application service | The `X-Service-Key` both sides check on every internal hop. |
| `QDRANT_API_KEY` | every application service, and qdrant | Injected into qdrant under the name *it* reads, `QDRANT__SERVICE__API_KEY`. |
| `HATCHET_CLIENT_TOKEN` | every application service | Worker/client auth to the engine. Generated by the engine on first boot. |
| `REDIS_PASSWORD` | every application service, and redis | The server sets `requirepass` from it; the clients authenticate with it. Both halves or neither. |
| `MARTIN_DATABASE_URL` | martin | Full connection string, as `martin_readonly`. |
| `HATCHET_DATABASE_URL` | hatchet | Full connection string for the `hatchet` role and database that `bootstrap.sql` creates. |
| `FLOW_JWT_SECRET` | fastapi, hatchet-worker | HS256 signing key for per-flow integration JWTs. |
| `REVERB_APP_SECRET` | the three Laravel services | Signs requests to the Pusher events API. The paired `REVERB_APP_KEY` is public and is a tfvar, not a secret. |
| `APP_KEY_NEXT` | the rotation task only | **Not a go-live key.** It exists only while an `APP_KEY` rotation is in flight; the rotation script writes it, the task reads it, the script removes it. `terraform/rotation.tf` explains why it is a secret rather than a task override, and relies on its absence to make the rotation task unrunnable at any other time. Do not create it now. |

### The one ordering constraint

ECS will not start a task that references a secret key which does not exist —
it fails before the container does, so the failure shows up as a task that
never starts rather than an application error. Write every go-live key above
before the first apply.

`HATCHET_CLIENT_TOKEN` is the awkward one, because the Hatchet engine mints
it and the engine is not up yet. Write a placeholder for it with the rest, so
the tasks that reference it can start at all, then replace it:

```bash
# after the first apply, once the hatchet service is running
aws ecs execute-command --cluster georag --task <hatchet-task> \
  --container hatchet --interactive \
  --command "/hatchet-admin token create --name ecs --tenant-id <tenant>"
# write the real value into georag/app, then:
aws ecs update-service --cluster georag --service georag-fastapi --force-new-deployment
aws ecs update-service --cluster georag --service georag-hatchet-worker --force-new-deployment
aws ecs update-service --cluster georag --service georag-laravel-octane --force-new-deployment
aws ecs update-service --cluster georag --service georag-laravel-horizon --force-new-deployment
aws ecs update-service --cluster georag --service georag-laravel-reverb --force-new-deployment
```

Until that swap the engine is healthy and every worker and client is not,
which reads like a Hatchet fault and is not one. `terraform apply` does not
wait for steady state, so it will report success while this is still true.

## Step 4: create the Qdrant collections, once

`scripts/init_qdrant.py` is the only thing in this repository that creates
`georag_chunks`, and nothing in the runtime path calls it on a cold start. The
drift self-heal in `embed_pending_passages` looks like it would cover this, but
it will not: it fires only when Qdrant is empty AND at least 50 passages are
already marked embedded in Postgres, which is a wipe, not a fresh install.

So on a brand-new Qdrant the collection simply is not there. Run it as a
one-off task against the deployed FastAPI image:

```bash
aws ecs run-task --cluster georag --task-definition georag-fastapi \
  --launch-type FARGATE --network-configuration "$PRIVATE_SUBNET_CONFIG" \
  --overrides '{"containerOverrides":[{"name":"fastapi",
    "command":["python3","/app/scripts/init_qdrant.py"]}]}'
```

It creates both collections with the named dense `""` slot **and** the named
sparse `"text"` slot. The sparse slot is not optional: SPLADE++ has no hosted
equivalent anywhere, so it runs as its own service here, and a collection
bootstrapped without that slot silently loses the sparse leg of hybrid
retrieval. A 2026-06-01 incident was exactly this.

You do not have to remember this one. CD runs `scripts/ops/post_deploy_smoke.py`
as a one-off task after every deploy (`cd.yml`), and its check 4 fails when
Qdrant is up but the collections the query path needs are absent. If you skip
this step the deploy gate tells you.

The collection is sized from `BEDROCK_EMBED_DIMENSION`, the same variable the
writer uses, so it cannot drift from what Cohere Embed v4 is asked to return.

## The reasoning carried over from Azure

The Azure tree is being deleted, and several of its files were the only
place a hard-won operational fact was written down. Those facts moved here
rather than going with it.

**The nightly sweeps are not `set -e`, and they log to stderr.** One failed
action must not strand the others — on the startup side that would leave
the platform down for the working day — and stdout is block-buffered until
the process exits, so a killed sweep's stdout lines all carry the timestamp
of its death. That is how a truncated sweep once printed "shutdown sweep
complete". Full reasoning in `scheduler/shutdown-sweep.sh`'s header.

**Results are judged by reading state, never by an exit code.** The AWS CLI
has the same property the Azure one did: a stop fails both when it failed
and when the thing was already stopped. The converse matters more — exit 0
with the instance still `available` is a failure, and only a state read
catches it.

**The scheduler role is narrow from the start.** The two Azure scheduler
jobs held Contributor over the whole resource group until 2026-08-23, and
two cron jobs deleted the database with it.

**The DST double-fire mechanism is gone.** Container Apps Jobs schedule in
UTC only, so each sweep fired at both candidate hours with an in-script
guard exiting 0 on the wrong one — and that guard was subtly wrong for two
days a year until 2026-08-21. EventBridge Scheduler takes a timezone.

**The alert suppression window is derived, not written out again.** It
comes from the two cron expressions (`local.maintenance_window_hours`), and
is surfaced as a Terraform output so it can be checked.

## What changed, on purpose

Three defects the Azure deployment carried are fixed here rather than
reproduced. A fresh deployment is the one moment when that costs nothing.

- **Redis gets persistence.** `redis-cc` ran with AOF off and no volume, so
  every restart and every nightly scale-to-zero dropped all sessions and
  any queued Horizon job. Now EFS plus `--appendonly yes`.
- **Qdrant loses two failure classes.** It *did* have persistent storage on
  Azure — an Azure Files share — but that share was mounted with the
  storage account key (rotating it broke the mount on the next restart) and
  had a fixed quota whose exhaustion stalled the optimiser and generated
  10.8M storage transactions in a day. EFS is elastic and IAM-authorised.
- **Bronze gets a backup posture.** It was the one irreplaceable copy: no
  backup workflow, no restore procedure, and Azure PITR covers Postgres
  only. Versioning plus a 90-day non-current retention is the fix, and on
  S3 it is configuration.

Two further things the move buys:

- **The Hatchet worker actually stops overnight.** `--min-replicas 0` is a
  floor, not an off switch, and the worker's 29 crons meant it was never
  idle — so the largest single line item, 4 vCPU / 8 GiB, ran 24/7 through
  every "shutdown". ECS `desired-count 0` stops it.
- **Both ALB-reachable services run two tasks.** At one, every deploy and
  task replacement is a user-visible outage. For Octane the Azure cost
  objection was already answered on evidence (`max_connections` 429
  against a 24h peak of 99; Octane opens PDO connections lazily per
  worker; session, cache and queue are all Redis) — that reasoning is in
  ADR-0022 §3. For Reverb the outage is every open WebSocket, which on
  this platform is every in-flight answer stream.

  Reverb's second task is only correct because `REVERB_SCALING_ENABLED`
  is on. Each instance holds only the subscribers connected to it, and
  Cloud Map hands a publisher one task at random, so without the Redis
  pub/sub backplane roughly half of every query's frames would be
  published to a task with none of that query's subscribers and dropped
  silently. **The desired count and that flag move together.** The count
  also lives in two places — `local.services` in
  `terraform/main.tf` and the `DESIRED` table in
  `scheduler/startup-sweep.sh`, which overwrites Terraform's value every
  morning — so changing one alone reverts overnight. The sweep test
  harness asserts both services come back at 2.

## What this is NOT

Two AZs and an ALB is not high availability. Only the two ALB-reachable
services run more than one task, RDS is Single-AZ, Qdrant and Redis are
single tasks holding EFS mounts, and the platform is stopped nightly on
purpose. Reverb's second task also makes Redis a dependency of WebSocket
fan-out where before it was a dependency of nothing — Redis being a single
task on EFS is unchanged, but it now has one more thing resting on it. The second AZ exists because an ALB requires two subnets. Making
RDS Multi-AZ also means giving up the nightly stop, which is the largest
cost lever this deployment has.

## The Bedrock endpoints are the sharp edge

Command A+ and Parse 5 are not in Bedrock's serverless catalogue, so they
run on SageMaker-managed endpoints that bill for as long as they exist.
There is no idle state — the only way not to pay is to delete them, so the
sweeps do, and recreate them each morning from their retained configs.

A failed recreate is not a degraded path. It is **no chat and no OCR**,
with no Bedrock invocation metric to alarm on because there are no
invocations to fail. The startup sweep therefore waits for `InService` and
does not report success without it; the `BEDROCK_ENDPOINT_NOT_INSERVICE`
marker it emits is matched by a CloudWatch metric filter and alarmed Sev 1.
This is the one operational cost the Bedrock route added that has no Azure
precedent, and it lives entirely in the scheduler.

## What is still not measured

Unchanged by the move, and recorded so it stays visible rather than being
rediscovered: nothing in production measures answer quality except
`answer_quality_watch` reading `silver.answer_runs`; the two `/metrics`
endpoints that exist are scraped by nothing in either environment; and
Laravel Pulse collects data nobody can view in production. See Ch 12.

Also unchanged: `RERANKER_SCORE_THRESHOLD_HOSTED` is 0.2, measured against
Cohere Rerank **v4**, and Bedrock serves **3.5**. It is the only
retrieval-quality gate in the system, and it is carried over unvalidated.

This document used to say "re-measure it on the golden set". That was wrong
and is worth correcting rather than deleting: calibrating a relevance floor
needs (query, chunk, relevant?) triples, and
`tests/golden_questions/seed_template.yaml` has none — its own header reads
"Status: SKELETON", and all 38 entries carry empty `expected_citations` and
`expected_numeric_values` marked "SME fills". So the re-measurement is
blocked on SME labelling that has not started, on top of needing a corpus
and in-region credentials.

Once the deployment carries traffic there is a route that needs no labels:
`answer_runs.reranker_version` records `cohere-bedrock:cohere.rerank-v3-5:0`,
so 3.5-scored runs are separable from v4-scored ones after the fact and the
floor can be picked from the observed score distribution against refusal
outcomes. Until then, treat 0.2 as unverified and watch the refusal rate —
`ops/runbooks/refusal-rate-spike.md` names this as the first thing to check.

## Testing the sweeps and the rotation

```bash
bash deploy/aws/scheduler/tests/run.sh
bash deploy/aws/rotation/tests/run.sh
```

No credentials, no network, no mutation — a fake `aws` CLI driven by
environment variables, plus a fake `php artisan` for the rotation. There is
still no staging environment to rehearse either on, so these harnesses
remain the only thing between a scheduler edit and finding out at 06:00, or
between a rotation edit and finding out that the audit ledger is
unreadable. Both run in CI.

## Rotating APP_KEY

```bash
export ROTATE_SUBNETS="$(terraform -chdir=terraform output -raw private_subnet_ids)"
export ROTATE_SECURITY_GROUP="$(terraform -chdir=terraform output -raw task_security_group_id)"

bash rotation/rotate-app-key.sh            # preflight + plan, mutates nothing
bash rotation/rotate-app-key.sh --apply    # do it
```

`ops/runbooks/secret-rotation.md` §2 is the procedure and the reasoning.
Two things to know before running it:

- **It takes the platform down.** `laravel-octane` and `laravel-horizon`
  are scaled to zero while `query_audit_log` is re-encrypted, because both
  write `encrypted` columns and `APP_MAINTENANCE_DRIVER` is `file` — a
  maintenance page cannot be made to cover two Octane tasks.
- **It must run inside the maintenance window**, after `startup-sweep.sh`
  has brought RDS up. It refuses to start against a stopped database.
