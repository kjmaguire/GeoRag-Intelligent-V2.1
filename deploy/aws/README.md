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

```bash
cd deploy/aws/terraform
terraform init
terraform plan -var-file=production.tfvars
```

`production.tfvars` is not in the repository. The variables with no default
are the ones a deployment must decide: `acm_certificate_arn`,
`alert_email`, and the two Bedrock Marketplace endpoint names.

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
- **Octane runs two tasks.** At one, every deploy and task replacement is a
  user-visible outage on the only public service. The Azure cost objection
  was already answered on evidence (`max_connections` 429 against a 24h
  peak of 99; Octane opens PDO connections lazily per worker; session,
  cache and queue are all Redis) — that reasoning is in ADR-0022 §3.

## What this is NOT

Two AZs and an ALB is not high availability. Every service except
`laravel-octane` runs a single task, RDS is Single-AZ, Qdrant and Redis are
single tasks holding EFS mounts, and the platform is stopped nightly on
purpose. The second AZ exists because an ALB requires two subnets. Making
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
retrieval-quality gate in the system. Re-measure it on the golden set
before this deployment carries real traffic.

## Testing the sweeps

```bash
bash deploy/aws/scheduler/tests/run.sh
```

No credentials, no network, no mutation — a fake `aws` CLI driven by
environment variables. There is still no staging environment to rehearse a
sweep on, so this harness remains the only thing between a scheduler edit
and finding out at 06:00.
