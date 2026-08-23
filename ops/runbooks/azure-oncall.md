# Azure on-call — the five things that actually break

**This is the only current operations runbook.** Everything else under
`ops/runbooks/` is compose-era and describes infrastructure that no
longer exists; those files have moved to `ops/runbooks/_archived/`.

The system runs on **Azure Container Apps** in resource group `georag`,
region `canadacentral`. There is no Docker host, no PgBouncer, no
SeaweedFS, no Neo4j, no Prometheus, no Grafana, no Loki and no
Alertmanager. If a document tells you to open a UI on port 9093 or run
`docker logs georag-<something>`, it is describing a stack that was
decommissioned in the Azure migration.

Everything below was run read-only against the live subscription on
2026-08-22. Commands are copy-pasteable.

```bash
az account set --subscription d314ab40-b5b7-4e3e-8308-86023fb7638a
```

---

## 0. First: is anything actually wrong?

**The platform is DOWN ON PURPOSE for part of every day.** Before
diagnosing anything, establish whether you are inside the maintenance
window.

| what | when |
| --- | --- |
| shutdown sweep fires | `0 6,7 * * *` UTC (23:00 US-Pacific) |
| startup sweep fires | `0 13,14 * * *` UTC (06:00 US-Pacific) |
| so the stack is down | roughly **06:00–14:00 UTC** |

Two cron hours each because Container Apps Jobs have no timezone
support: each job fires at both candidate hours and a DST guard inside
the script exits 0 on the wrong one. A skipped run is a **success** with
no work done — that is expected, not a fault.

```bash
az postgres flexible-server show -g georag -n georag-pg-cc --query state -o tsv
```

`Stopped` between 06:00 and 14:00 UTC is correct. `Stopped` outside that
window is a real incident. To see which it was, ask the scheduler:

```bash
az containerapp job execution list -g georag -n shutdown-scheduler-cc --query "[0:5].{status:properties.status,start:properties.startTime,end:properties.endTime}" -o table
```

Reading that output:

- `Succeeded` — the sweep ran and every action worked.
- `Failed` **with** an `endTime` — the sweep ran and reported at least
  one failed action. Get the detail from the logs (section 6).
- `Failed` **with no** `endTime` — the container was killed at its
  `replicaTimeout`. The sweep was cut off partway; assume the platform is
  in a half-stopped state and check each app individually.

> Before 2026-08-22 this job exited 0 and printed "shutdown sweep
> complete" no matter what happened, so historical `Succeeded` results
> older than that date mean nothing.

---

## 1. Postgres is stopped when it should be running

The startup sweep failed to start it, or something stopped it.

```bash
az postgres flexible-server show -g georag -n georag-pg-cc --query "{state:state,version:version,tier:sku.tier}" -o table
az postgres flexible-server start -g georag -n georag-pg-cc
```

`start` returns **non-zero both for a real failure and for a server that
is already running** (`ServerIsNotStopped`). Do not read the exit code —
read the state afterwards:

```bash
az postgres flexible-server show -g georag -n georag-pg-cc --query state -o tsv
```

`Ready` means you are done regardless of what `start` printed. Starting
takes a couple of minutes; `Starting` is fine, wait.

**The app tier will not recover on its own** if it came up against a
stopped database. After Postgres reads `Ready`, restart the consumers:

```bash
for app in fastapi-cc hatchet-worker-cc laravel-octane-cc laravel-horizon-cc; do
  az containerapp revision restart -g georag -n "$app" --revision "$(az containerapp revision list -g georag -n "$app" --query "[?properties.active].name | [0]" -o tsv)"
done
```

---

## 2. The stack is down outside the maintenance window

Check every app at once:

```bash
az containerapp list -g georag --query "[].{name:name,running:properties.runningStatus,min:properties.template.scale.minReplicas}" -o table
```

`min: 0` outside the window means the shutdown sweep ran when it should
not have, or the startup sweep never restored the replica counts. Restore
them in dependency order — infrastructure first, then consumers, then the
web tier:

```bash
for app in redis-cc qdrant-cc hatchet-cc; do az containerapp update -g georag -n "$app" --min-replicas 1 --output none; done
for app in hatchet-worker-cc fastapi-cc; do az containerapp update -g georag -n "$app" --min-replicas 1 --output none; done
for app in laravel-octane-cc laravel-horizon-cc laravel-reverb-cc; do az containerapp update -g georag -n "$app" --min-replicas 1 --output none; done
```

The order matters: the Laravel tier's boot guard refuses to serve traffic
against a schema that is not there, and the Hatchet worker will spin
retrying if the engine is not up yet.

---

## 3. Ingestion has stopped moving

Symptom: uploads accepted, nothing progresses. Usually the Hatchet worker
is up but not consuming.

```bash
az containerapp show -g georag -n hatchet-worker-cc --query "{running:properties.runningStatus,min:properties.template.scale.minReplicas}" -o table
```

Then check whether steps are *finishing*, not just starting — a worker
that starts steps and never finishes them looks busy in the logs:

```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(6h)
| where ContainerAppName_s == "hatchet-worker-cc"
| where Log_s has "finished step run:"
| summarize finished = count() by bin(TimeGenerated, 30m)
| order by TimeGenerated desc
```

```bash
az monitor log-analytics query --workspace workspace-georag4ad7 --analytics-query "<the query above, on one line>" -o table
```

Zero `finished step run:` over hours while the app is Running means the
worker is not consuming. Restart it (section 1's restart snippet).

---

## 4. Answers fail or come back empty

### Foundry is refusing calls

Azure AI Foundry emits `ClientErrors` as a free platform metric. On
2026-08-17 it blocked 1,421 of 2,524 calls and nothing noticed.

```bash
az monitor metrics list --resource georag-foundry-cc --resource-group georag \
  --resource-type Microsoft.CognitiveServices/accounts \
  --metric ClientErrors --aggregation Total --interval PT1H \
  --start-time "$(date -u -d '6 hours ago' +%Y-%m-%dT%H:%M:%SZ)" \
  --end-time "$(date -u +%Y-%m-%dT%H:%M:%SZ)" -o table
```

A sustained non-zero count is throttling, quota exhaustion, or an expired
key. Quota is per-deployment; check the deployment before assuming the
key is wrong.

### Qdrant's optimizer is stuck

There is already a scheduled-query alert for this
(`qdrant-cc-optimizer-stuck`) and its description is accurate. The
classic cause is the storage share hitting its quota — the symptom
surfaces as `Not enough space available for optimization`, which reads
like a disk-full error on the container and is not.

```bash
az storage share-rm show --storage-account georagblobcc -n qdrant-storage --query "{quotaGiB:shareQuota,tier:accessTier}" -o table
```

If used capacity is near `shareQuota`, raise the quota. This exact
failure happened on 2026-08-20 at a 10 GiB quota against ~7 GB used; it
was resolved by raising the quota to 100 GiB.

---

## 5. A bad deploy needs rolling back

Every app is in **single-revision mode**, so rollback is by image, not by
traffic split. CD snapshots the previous image before it changes
anything:

```bash
az containerapp show -g georag -n laravel-octane-cc \
  --query "properties.template.containers[0].image" -o tsv
az containerapp revision list -g georag -n laravel-octane-cc \
  --query "[].{name:name,active:properties.active,created:properties.createdTime}" -o table
```

Roll back by pointing the app at the previous tag:

```bash
az containerapp update -g georag -n laravel-octane-cc \
  --image georagacrcc.azurecr.io/georag/laravel:<previous-short-sha>
```

**Migrations do not roll back with the image.** `laravel-migrate-job`
runs before the app rollout, so a rollback returns the code but not the
schema. If the bad deploy included a migration, decide explicitly whether
the old image tolerates the new schema before rolling back.

---

## 6. Reading the logs

Application logs:

```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(1h)
| where ContainerAppName_s == "fastapi-cc"
| project TimeGenerated, Log_s
| order by TimeGenerated desc
```

**Scheduler job logs are different, and this trips everyone.** Container
App **Jobs** do not populate `ContainerAppName_s` — that column is empty
and the job name lives in `ContainerJobName_s`:

```kusto
ContainerAppConsoleLogs_CL
| where TimeGenerated > ago(24h)
| where ContainerJobName_s in ("shutdown-scheduler-cc", "startup-scheduler-cc")
| project TimeGenerated, ContainerJobName_s, Log_s
| order by TimeGenerated asc
```

A query written the obvious way against `ContainerAppName_s` parses,
runs, costs money and matches nothing.

One more log-reading trap: **stdout in these containers is block-buffered
until the process exits; stderr is real-time.** Timestamps on stdout
lines cluster at the moment the container died, not when the work
happened. The sweep scripts deliberately write all progress to stderr for
this reason.

---

## What is NOT covered here

- **Backups.** All four backup workflows are known broken and have never
  been restore-tested. There is no working restore procedure to document.
- **Paging.** There is no on-call rotation. `georag-alerts-ag` has a
  single email receiver (kylejmaguire@gmail.com) and the PagerDuty
  dispatcher has no caller. If you are reading this, someone told you
  directly.
- **Secret rotation and PII decryption.** Still accurate in
  `docs/RUNBOOK.md`, which is the other document worth keeping.
