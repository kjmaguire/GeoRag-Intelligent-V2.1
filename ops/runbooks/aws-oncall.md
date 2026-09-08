# AWS on-call — the things that actually break

**This is the incident runbook.** Its companions are
`secret-rotation.md` (every credential, where it lives, how to roll it),
`refusal-rate-spike.md` (answer-quality triage) and `raw-sql-layer.md`.
Everything else under `ops/runbooks/` is compose-era and describes
infrastructure that no longer exists; those files are in
`ops/runbooks/_archived/`.

The system runs on **Amazon ECS Fargate**, cluster `georag`. There is no
Docker host, no PgBouncer, no SeaweedFS, no Neo4j, no Prometheus, no
Grafana, no Loki and no Alertmanager. If a document tells you to open a UI
on port 9093 or run `docker logs georag-<something>`, it is describing a
stack that was decommissioned long before this one.

**Ported from `azure-oncall.md` on 2026-09-08 (ADR-0022).** Everything
below that says "this trips everyone" was learned the expensive way on the
previous cloud, and the ones that are properties of the runtime rather
than of Azure survived the move. Where a section describes a failure mode
that is GONE, it says so rather than being deleted — knowing a class of
incident cannot happen any more is worth as much as knowing one can.

---

## 0. First: is anything actually wrong?

**The platform is DOWN ON PURPOSE for part of every day.** Before
diagnosing anything, establish whether you are inside the maintenance
window.

| what | when |
| --- | --- |
| shutdown sweep fires | 23:00 **US-Pacific** |
| startup sweep fires | 06:00 **US-Pacific** |
| so the stack is down | roughly **06:00–13:00 UTC** (PDT) / **07:00–14:00 UTC** (PST) |

One fire each, not two. EventBridge Scheduler is timezone-aware, so the
Azure-era double-fire — both candidate UTC hours, with a DST guard inside
each script exiting 0 on the wrong one — is gone, and so is the class of
incident where a skipped run looked exactly like a correct off-hour skip.
A sweep that runs now either does its work or reports a failure.

```bash
aws rds describe-db-instances --db-instance-identifier georag-pg \
  --query 'DBInstances[0].DBInstanceStatus' --output text
```

`stopped` inside the window is correct. `stopped` outside it is a real
incident. To see which, ask the scheduler:

```bash
aws ecs list-tasks --cluster georag --family georag-shutdown-sweep \
  --desired-status STOPPED --query 'taskArns[:5]' --output text \
| xargs -n1 -I{} aws ecs describe-tasks --cluster georag --tasks {} \
  --query 'tasks[0].{stopped:stoppedAt,code:containers[0].exitCode,reason:stoppedReason}'
```

Reading that:

- exit code `0` — the sweep ran and every action worked.
- exit code `1` — the sweep ran and reported at least one failed action.
  Get the detail from the logs (section 6).
- **no exit code at all** — the task was killed before its container
  finished. The sweep was cut off partway; assume the platform is in a
  half-stopped state and check each service individually.

---

## 1. Postgres is stopped when it should be running

The startup sweep failed to start it, or something stopped it.

```bash
aws rds describe-db-instances --db-instance-identifier georag-pg \
  --query 'DBInstances[0].{status:DBInstanceStatus,engine:EngineVersion,class:DBInstanceClass}'
aws rds start-db-instance --db-instance-identifier georag-pg
```

`start-db-instance` returns **non-zero both for a real failure and for an
instance that is already running**. Do not read the exit code — read the
state afterwards:

```bash
aws rds describe-db-instances --db-instance-identifier georag-pg \
  --query 'DBInstances[0].DBInstanceStatus' --output text
```

`available` means you are done regardless of what `start` printed.
Starting takes a couple of minutes; `starting` is fine, wait.

> RDS force-starts an instance that has been stopped for 7 days. The
> nightly cadence makes that a non-issue in practice, but if the schedule
> is ever disabled for a week, expect the database to come back on its
> own.

**The app tier will not recover on its own** if it came up against a
stopped database. After the instance reads `available`, restart the
consumers:

```bash
for svc in fastapi hatchet-worker laravel-octane laravel-horizon; do
  aws ecs update-service --cluster georag --service "$svc" \
    --force-new-deployment --query 'service.serviceName' --output text
done
```

---

## 2. The stack is down outside the maintenance window

Check every service at once:

```bash
aws ecs describe-services --cluster georag \
  --services laravel-octane laravel-horizon laravel-reverb fastapi hatchet \
             hatchet-worker qdrant redis martin sparse \
  --query 'services[].{name:serviceName,desired:desiredCount,running:runningCount}' \
  --output table
```

`desired: 0` outside the window means the shutdown sweep ran when it
should not have, or the startup sweep never restored the counts. Rather
than restoring them by hand, run the startup sweep — it does the tiers in
the right order and waits for each:

```bash
aws ecs run-task --cluster georag --task-definition georag-startup-sweep \
  --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[<private-subnets>],securityGroups=[<task-sg>],assignPublicIp=DISABLED}"
```

The order matters and is why the sweep exists: the Laravel tier's boot
guard refuses to serve traffic against a schema that is not there, and the
Hatchet worker spins retrying if the engine is not up yet.

---

## 3. Ingestion has stopped moving

Symptom: uploads accepted, nothing progresses. Usually the Hatchet worker
is up but not consuming.

```bash
aws ecs describe-services --cluster georag --services hatchet-worker \
  --query 'services[0].{desired:desiredCount,running:runningCount}' --output table
```

Then check whether steps are *finishing*, not just starting — a worker
that starts steps and never finishes them looks busy in the logs:

```bash
aws logs start-query --log-group-name /ecs/georag \
  --start-time "$(date -u -d '6 hours ago' +%s)" --end-time "$(date -u +%s)" \
  --query-string 'fields @timestamp | filter @logStream like /hatchet-worker/ | filter @message like /finished step run:/ | stats count() by bin(30m)'
```

Zero `finished step run:` over hours while the service is running means
the worker is not consuming. Force a new deployment (section 1's snippet).

---

## 4. Answers fail or come back empty

### Bedrock is refusing calls

Bedrock emits `InvocationClientErrors` as a free CloudWatch metric, and
both alarms carry thresholds MEASURED on the previous cloud: Foundry
blocked 1,421 of 2,524 calls on 2026-08-17 and nothing noticed, which is
what earned the rule its existence.

```bash
aws cloudwatch get-metric-statistics --namespace AWS/Bedrock \
  --metric-name InvocationClientErrors --statistics Sum --period 3600 \
  --start-time "$(date -u -d '6 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" --output table
```

Since ADR-0019 the same model family also serves **Cohere Parse 5**, the
scanned-page OCR engine, so an error spike during a large ingest can be
OCR rather than the answer path. Tell them apart with the ingest worker's
logs (`cohere_parse:` prefix) and the
`georag_ocr_pages_total{engine="cohere_parse"}` counter. A Parse failure
never stops ingestion: every failed page falls back to tesseract, which
extracts no table structure, so the symptom is passages with
`ocr_method='tesseract'` where `cohere_parse` was expected. A worker whose
env is missing `BEDROCK_PARSE_MODEL_ID` logs one CRITICAL line and runs
tesseract for every page.

Known false-positive source, carried across: the `> 50 / 15m` threshold
was tuned for the answer path. A large scanned ingest sends one Parse
request per page, and a throttle storm the adapter recovers from still
counts toward it. Check the OCR counter is still rising before treating it
as an outage.

### A Bedrock Marketplace endpoint did not come back

**New on AWS, and the one that will not announce itself.** Chat (Command
A+) and OCR (Parse 5) run on SageMaker-managed endpoints that bill while
they exist, so the nightly sweeps delete and recreate them. A failed
recreate leaves no chat and no OCR at all — and Bedrock's invocation-error
metrics cannot see it, because there are no invocations to fail.

```bash
aws sagemaker describe-endpoint --endpoint-name <chat-endpoint> \
  --query '{status:EndpointStatus,reason:FailureReason}'
```

Anything other than `InService` is the incident. The startup sweep emits
`BEDROCK_ENDPOINT_NOT_INSERVICE` and does not report success without it,
and a CloudWatch metric filter alarms on that marker at Sev 1. Recreate
from the retained config:

```bash
aws sagemaker create-endpoint --endpoint-name <chat-endpoint> \
  --endpoint-config-name <chat-endpoint>-config
```

The endpoint CONFIGS are never deleted, precisely so this is one call
rather than a rebuild. A cold create takes minutes.

### Qdrant's optimizer is stuck

**Largely gone, and worth knowing why.** On Azure the classic cause was
the storage share hitting its fixed quota — the symptom surfaced as
`Not enough space available for optimization`, which reads like a
disk-full error on the container and is not. It happened on 2026-08-20 at
a 10 GiB quota against ~7 GB used, and the optimizer loop that followed
generated 10.8 M storage transactions in a day.

EFS is elastic: there is no quota to exhaust. If you see that message
anyway, it is a genuine capacity or permissions problem rather than a
configured ceiling, and `elasticfilesystem:ClientWrite` on the task role
is the first thing to check.

---

## 5. A bad deploy needs rolling back

Mostly automatic now. Every service has the ECS deployment circuit breaker
with rollback enabled, so a service that cannot reach a steady state
reverts to its previous task definition without anyone intervening. Check
what it decided:

```bash
aws ecs describe-services --cluster georag --services laravel-octane \
  --query 'services[0].deployments[].{status:status,rollout:rolloutState,reason:rolloutStateReason,taskDef:taskDefinition}' \
  --output table
```

To roll back by hand, point the service at the previous revision:

```bash
aws ecs update-service --cluster georag --service laravel-octane \
  --task-definition georag-laravel-octane:<previous-revision>
```

**Migrations do not roll back with the image.** The schema task runs
before any service rolls, so a rollback returns the code but not the
schema. If the bad deploy included a migration, decide explicitly whether
the old image tolerates the new schema before rolling back. This is the
one thing ECS's circuit breaker cannot know about, and it is why CD's
failure summary prints the migration task ARN.

---

## 6. Reading the logs

Application logs:

```bash
aws logs tail /ecs/georag --since 1h --follow --format short \
  --filter-pattern '' --log-stream-name-prefix fastapi
```

**The scheduler writes to its own log group.** That is deliberate, and it
replaces an Azure trap worth recording: Container App **Jobs** did not
populate `ContainerAppName_s` — the column was empty and the job name
lived in `ContainerJobName_s` — so a query written the obvious way parsed,
ran, cost money and matched nothing, silently, forever.

```bash
aws logs tail /ecs/georag/scheduler --since 24h --format short
```

One log-reading trap that DID survive the move, because it is a property
of the container runtime and not of any cloud: **stdout is block-buffered
until the process exits; stderr is real-time.** Timestamps on stdout lines
cluster at the moment the container died, not when the work happened. That
is also how a truncated sweep once printed "shutdown sweep complete" — the
line was already in the buffer and the flush on teardown made a killed run
look finished. The sweep scripts write all progress to stderr for exactly
this reason.

---

## What is NOT covered here

- **Restore.** Postgres has RDS automated backups with 35-day PITR, and
  the S3 buckets have versioning with 90-day non-current retention — which
  is more than existed before, since blob storage on Azure had no backup
  workflow and no restore procedure at all. Neither has been
  restore-tested. Until one is, there is no restore procedure to document,
  only a mechanism that should work.
- **Paging.** There is no on-call rotation. The SNS topic has a single
  email receiver. If you are reading this, someone told you directly.
- **Secret rotation.** `secret-rotation.md` — per-credential procedures;
  the `APP_KEY` and `FASTAPI_SERVICE_KEY` internals stay in
  `docs/RUNBOOK.md`, which also owns PII decryption.
- **Refusal-rate spikes.** `refusal-rate-spike.md` — what the
  `ANSWER_QUALITY_REGRESSION` alert measures and how to find the cause.
- **Answer quality generally.** Nothing in production measures it beyond
  that one nightly check reading `silver.answer_runs`. Unchanged by the
  cloud move, and the single largest observability gap.
