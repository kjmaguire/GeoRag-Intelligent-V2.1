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

## Check the preconditions first

```bash
AWS_REGION=<region> bash scripts/operator/aws-preflight.sh
```

Read-only, and it exits non-zero rather than letting a cutover start on a
missed step. It reports **unverified** rather than passing for anything it
cannot reach the account to answer, so run it from a shell with AWS access —
a check nobody could answer is not a check that passed.

It covers the steps below that have a queryable answer: the variables with no
default, the secret keys in `georag/app`, the Marketplace endpoints and
— the one that costs you a morning — whether their configs carry the
`<endpoint-name>-config` name the nightly sweep looks for. The one-time
actions (Steps 1, 2 and 4) have no state to query and are listed by the
script as still yours.

Note that `scripts/operator/preflight.sh` is a **different** script and does
not gate this deployment: it predates ADR-0022 and checks SSH hosts and SOPS
for the compose model, which Fargate does not use.

## Before the first apply: remote state

Until 2026-09-15 this tree had **no backend block**, so state was a local file
next to whoever ran `terraform apply`. For the one record of what exists in the
account, that is the failure the rest of this tree was written to avoid: lose
the machine and the resources keep running with nothing able to manage them —
the next apply does not adopt them, it tries to *create* them and collides on
names already taken, and the way out is importing every resource by hand. It
also means no locking, and an apply from any ephemeral box (a CI runner, a
cloud dev environment) discards the state when the box is reclaimed.

Create the bucket once per account, then init against it:

```bash
bash deploy/aws/terraform/bootstrap-state.sh georag-tfstate-<account-id> <region>

cd deploy/aws/terraform
cp backend.hcl.example backend.hcl        # fill in the bucket
terraform init -backend-config=backend.hcl
```

The bucket is created by a script rather than by Terraform on purpose: it
cannot be managed by the state it holds. The script is idempotent, so re-running
it also serves as a check that versioning, encryption, the public-access block
and the TLS-only policy are still in place. Versioning is the one to care about
— state is overwritten on every apply.

Locking needs no DynamoDB table: `backend.tf` sets `use_lockfile`, so the lock
is a conditional write in the same bucket. That needs Terraform >= 1.10, which
is why `required_version` says so.

`backend.hcl` is gitignored, along with `*.tfstate` and `*.tfvars`. Nothing in
`backend.hcl` is secret, but it sits where something secret would get pasted.

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
serverless.** The entire model tier rests on this.

> **2026-09-15:** Kyle confirmed Command A+ and Parse 5 are in Bedrock
> Marketplace and deployed both from the Bedrock console. That is the right
> half of a distinction worth stating once, because the two look alike and
> only one works with this code: a **Bedrock** Marketplace deployment is
> invoked through `bedrock-runtime` Converse with the endpoint ARN as
> `modelId`, which is what `llm_bedrock.py` does. An **AWS Marketplace**
> SageMaker model package is a different product — `sagemaker-runtime.
> invoke_endpoint`, different request and response shape, ADR-0022 option C,
> a rewrite. Chat and OCR would fail at the first call, not at deploy.
>
> Availability is therefore settled. The wire shapes are **not** — that is
> the probe below, and it is still the largest open risk in this deployment.

The serverless half is still worth checking, since Embed v4 and Rerank 3.5
come from the catalogue rather than from an endpoint you deployed:

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

### Name the endpoint configs `<endpoint-name>-config`

Terraform does not create the two Marketplace endpoints — you do, by
subscribing in Bedrock Marketplace and deploying each model. Terraform only
takes their **names**, as `bedrock_chat_endpoint_name` and
`bedrock_parse_endpoint_name`.

The nightly cycle deletes those endpoints and recreates them each morning
from their **retained endpoint configs**, and it finds the config by
convention, not by discovery:

```
scheduler.tf:82-85   "<endpoint-name>=<endpoint-name>-config"
```

The sweep role holds `sagemaker:DescribeEndpointConfig` but deliberately not
`ListEndpointConfigs`, so it cannot go looking for a config under a different
name. If the console named yours anything else, the deployment works on day
one and then, on the first morning restart, `create-endpoint` fails and you
have no chat and no OCR — the failure mode the section below calls Sev 1.

So after deploying each model, check the config name and fix it if it does
not match:

```bash
for ep in "$CHAT_ENDPOINT" "$PARSE_ENDPOINT"; do
  actual=$(aws sagemaker describe-endpoint --endpoint-name "$ep" \
             --query EndpointConfigName --output text)
  echo "$ep -> $actual   (sweep will look for ${ep}-config)"
done
```

An endpoint config is immutable but cheap to re-create under the right name:
copy the production variant out of the existing one with
`describe-endpoint-config`, `create-endpoint-config --endpoint-config-name
"${ep}-config"` with the same variant, then `update-endpoint` onto it. Do
that now, while an outage costs nothing.

### Step 0 end to end, from a workstation

Run this on a machine with a browser. It will **not** complete in a cloud
development container: `aws login` exchanges its authorization code at
`signin.aws.amazon.com`, and an agent sandbox typically refuses that host
(verified 2026-09-15 — `403 to CONNECT`, while `oidc.*.amazonaws.com` and
every service endpoint were reachable). The sign-in page renders, because
that runs in your browser; the token exchange runs from the shell and does
not, so no credentials are ever written and the failure reads as a proxy
error. An IAM Identity Center account can use `aws sso login
--use-device-code --no-browser` from such a container instead.

Needs `uv` on PATH — `bedrock_probe.sh` runs `uv run --no-sync` inside
`src/fastapi`.

```bash
# Every value below is a placeholder with a plausible default. Read each one
# before running the block; none of them is guessed correctly for you.
export REGION=ca-west-1          # the stack's region
export PROFILE=georag

# 1. Authenticate. Credentials last 12h, renewable 90 days without the browser.
aws configure set region "$REGION" --profile "$PROFILE"
aws login --region "$REGION" --profile "$PROFILE"
aws sts get-caller-identity --profile "$PROFILE"

# 2. Find the two Marketplace endpoints, and which region they are really in.
#    That region is BEDROCK_REGION and may differ from the stack's region —
#    `bedrock_region` is a separate variable for exactly this reason. If this
#    comes back empty, the endpoints are in another region; try it there.
aws sagemaker list-endpoints --profile "$PROFILE" --region "$REGION" \
  --query 'Endpoints[].[EndpointName,EndpointStatus]' --output table

# 3. The naming check above, which is what the section you just read is for.
export BEDROCK_REGION="$REGION"  # or wherever step 2 actually found them
export CHAT_ENDPOINT=georag-chat     # the names you gave them in the console
export PARSE_ENDPOINT=georag-parse
for ep in "$CHAT_ENDPOINT" "$PARSE_ENDPOINT"; do
  actual=$(aws sagemaker describe-endpoint --endpoint-name "$ep" \
             --profile "$PROFILE" --region "$BEDROCK_REGION" \
             --query EndpointConfigName --output text)
  status=$(aws sagemaker describe-endpoint --endpoint-name "$ep" \
             --profile "$PROFILE" --region "$BEDROCK_REGION" \
             --query EndpointStatus --output text)
  echo "$ep [$status] -> config '$actual'   (sweep needs '${ep}-config')"
done

# 4. What the serverless catalogue actually offers in that region. Embed v4
#    and Rerank 3.5 are catalogue models, not endpoints anyone deployed — a
#    region that does not carry them has nowhere to run embeddings or
#    reranking. Take the exact modelIds from this output rather than
#    assuming them.
aws bedrock list-foundation-models --profile "$PROFILE" --region "$BEDROCK_REGION" \
  --query 'modelSummaries[?providerName==`Cohere`].[modelId,modelName]' --output table

# 5. The probe. It takes NO --region flag: it reads the environment, and the
#    two ARNs are built exactly as config.tf:331-332 builds them, so what is
#    probed is what production will use.
ACCOUNT=$(aws sts get-caller-identity --profile "$PROFILE" --query Account --output text)
export AWS_PROFILE="$PROFILE"
export AWS_REGION="$REGION"
export BEDROCK_CHAT_MODEL_ID="arn:aws:sagemaker:${BEDROCK_REGION}:${ACCOUNT}:endpoint/${CHAT_ENDPOINT}"
export BEDROCK_PARSE_MODEL_ID="arn:aws:sagemaker:${BEDROCK_REGION}:${ACCOUNT}:endpoint/${PARSE_ENDPOINT}"
export BEDROCK_EMBED_MODEL_ID=cohere.embed-v4:0        # confirm against step 4
export BEDROCK_RERANK_MODEL_ID=cohere.rerank-v3-5:0    # confirm against step 4
bash ops/validation/bedrock_probe.sh

# 6. COMMIT THE REPORT. The adapters carry [UNVERIFIED] until one exists,
#    and aws-preflight.sh A-11 fails until one lands here.
git add ops/validation/reports/bedrock_probe_*.json
git commit -m "chore(validation): commit the in-region Bedrock wire-contract probe report"

# 7. Everything the preflight could not answer without an account.
AWS_REGION="$REGION" AWS_PROFILE="$PROFILE" bash scripts/operator/aws-preflight.sh
```

Read the report before trusting any adapter. If Parse's shape differs from
what `_page_from_payload` expects, the probe says so — and since 2026-09-15
the adapter says so too, at runtime: an unrecognised body now fails soft to
tesseract behind the `COHERE_PARSE_UNRECOGNISED_RESPONSE` marker and its
alarm, rather than returning a silently blank page that no metric could see.

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

## One posture decision left open: X-Forwarded-For

`TRUSTED_PROXIES` is set for you (`config.tf`, the VPC CIDR) because without
it Laravel honours no `X-Forwarded-*` header at all and every request behind
the TLS-terminating ALB looks like plain HTTP.

`TRUST_FORWARDED_FOR` is **not** set, and that is deliberate.
`App\Support\ProxyTrust` strips `X-Forwarded-For` in production unless it is,
because Azure Container Apps' ingress passed a client-supplied chain straight
through — so `$request->ip()` was whatever the caller typed. Measured
2026-08-20.

An AWS ALB does not do that: it appends the real peer to the chain, and
`drop_invalid_header_fields = true` is set on the listener. So the condition
ProxyTrust names for turning it on — "an ingress that APPENDS to the chain" —
is met here, and with `TRUSTED_PROXIES` scoped to the VPC, Symfony can walk
the chain and discard trusted hops to get the true client IP.

It is left off because that file also says to verify before believing it, and
that cannot be done from a repository. Left off, `$request->ip()` is the
ALB's address: coarse, but unforgeable, and the two limiters that matter
degrade gracefully — the login limiter also keys on the submitted email and
the query limiter keys on the authenticated user id. What you lose is real
client IPs in audit records and in the guest-facing tile limiter.

To turn it on, run ProxyTrust's own three requests against the deployed ALB
and confirm the middle one does **not** change the answer:

```bash
curl -s -o /dev/null -w '%{http_code}\n' "https://$APP_DOMAIN/metrics"
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Forwarded-For: 10.0.0.1' "https://$APP_DOMAIN/metrics"
curl -s -o /dev/null -w '%{http_code}\n' -H 'X-Forwarded-For: 8.8.8.8'  "https://$APP_DOMAIN/metrics"
```

If a spoofed header still moves the result, leave it off and say so in the
runbook. If all three agree, set `TRUST_FORWARDED_FOR=true`.

### And one more: RATE_LIMIT_ENABLED

`main.py::_assert_production_posture` logs CRITICAL on a `GEORAG_ENV=production`
process when `RATE_LIMIT_ENABLED` is off, saying "no request throttling of any
kind is installed". On this deployment that line will appear on every FastAPI
boot, and it overstates the case.

FastAPI is not in an ALB target group. Nothing outside the VPC can reach it,
and every request it serves arrives from an Octane task over Cloud Map. The
per-client throttling is on the Laravel side, where the clients actually are:
`auth-login`, `queries`, `public-geoscience-tiles` and
`bridge:report-progress` limiters in `AppServiceProvider`, plus inline
`throttle:` on the sensitive routes.

Turning the FastAPI limiter on would not add a per-client control, because
`slowapi` is wired with `key_func=get_remote_address` and every remote address
it sees is an Octane task. At `RATE_LIMIT_DEFAULT=60/minute` that is a global
cap of roughly 60 requests per minute per Octane task across all users — a
crude ceiling that would start returning 429s under ordinary load.

So it is left off, and the CRITICAL line is noise rather than a finding. Worth
fixing properly at some point — either by keying the limiter on a
workspace/user header Laravel already forwards, or by narrowing the posture
check to deployments where FastAPI is internet-facing. Neither belongs in a
cutover.

No CloudWatch alarm watches for CRITICAL log lines, so this does not page.

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
