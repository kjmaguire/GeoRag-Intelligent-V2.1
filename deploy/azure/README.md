# deploy/azure

Hand-applied Azure resource definitions. Nothing in this directory is
touched by CI or CD -- `.github/workflows/cd.yml` pushes container images
and runs `laravel-migrate-job`, and that is all. These files exist so the
live configuration has a reviewed source, not so it deploys itself.

Both job headers have cited this README since they were written; it did
not exist until 2026-08-21.

## containerapps/

| file | applies to | apply with |
| --- | --- | --- |
| `shutdown-job.yaml` | `shutdown-scheduler-cc` | `az containerapp job update -g georag -n shutdown-scheduler-cc --yaml <file>` |
| `startup-job.yaml` | `startup-scheduler-cc` | `az containerapp job update -g georag -n startup-scheduler-cc --yaml <file>` |
| `redis.yaml` | `redis-cc` | `bash deploy/azure/containerapps/apply-redis.sh --apply` |
| `probes.json` | five apps that had none | `bash deploy/azure/containerapps/apply-probes.sh --apply` |

`redis.yaml` is the one file here that must **not** be applied with a
bare `az containerapp update --yaml`. It has to carry a `secrets:` block
for Container Apps to accept the shape, and that block's value is the
placeholder `REPLACE_AT_DEPLOY_TIME` — sending it sets the live
`redis-password` secret to that literal string and every client fails
auth. `apply-redis.sh` strips the block, refuses to send anything still
holding the placeholder, and verifies the secret survived.

Both scheduler jobs run with a system-assigned identity holding the
custom **GeoRAG Nightly Scheduler** role on the `georag` resource group --
`*/read` plus `Microsoft.App/containerApps/write` and the flexible-server
start/stop actions. They held Contributor over the whole group until
2026-08-23, which let two cron jobs delete the database. See
`rbac/apply-scheduler-role.sh`.

### The scheduler bodies are generated

A Container Apps Job takes one inline script, so the bash lives twice:
once as a reviewed, tested file under `containerapps/scripts/`, and once
as the `args` block in the YAML. The script is the source.

```bash
python scripts/check_scheduler_job_parity.py --write
```

CI (`scheduler-jobs`) runs `containerapps/scripts/tests/run.sh` and then
the checker, which verifies both that the two copies agree and that the
cron's UTC hours still resolve to the guard's target local hour.

### The maintenance window

`0 6,7 * * *` and `0 13,14 * * *` UTC, guarded down to 23:00 and 06:00
US-Pacific. Container Apps Jobs have no timezone support, so each job
fires at both candidate hours and its DST guard exits 0 on the wrong one.
That is why there are two cron hours and why the parity checker refuses a
cron that does not pair with the guard.

## alerts/

`create-alerts.sh` creates the Azure Monitor rules the group is missing.
It prints the commands by default and only mutates with `--apply`, so it
is safe to read first:

```bash
bash deploy/azure/alerts/create-alerts.sh
```

What it adds, and why each one is not already covered:

| rule | why |
| --- | --- |
| `scheduler-sweep-failed` | nothing watches container-app job outcomes; the 15 metric alerts are all app/PG counters |
| `scheduler-sweep-missing` | a sweep killed at its replicaTimeout emits no verdict at all, so a failure rule alone cannot see it |
| `laravel-octane-cc-5xx` | the only public ingress has no availability or error-rate rule, though Container Apps emits `Requests` split by `statusCodeCategory` for free |
| `laravel-octane-cc-dead-air` | the restart counter cannot tell "crash-looped and served nothing" from "restarted once and recovered" |
| `suppress-during-maintenance` | ...and dead air is the *intended* state during the nightly window, so the rule above is suppressed across it |
| `georag-foundry-cc-client-errors` | Foundry blocked 1,421 of 2,524 calls on 2026-08-17 and nothing noticed; the metric was already there |
| `pg-to-law` diagnostic setting | `az monitor diagnostic-settings list` returns `[]` for georag-pg-cc, so a CPU alert at 03:00 has no query-level evidence behind it |

Two things worth knowing before editing it.

**Container App Jobs do not populate `ContainerAppName_s`.** Their console
logs land in `ContainerAppConsoleLogs_CL` with that column empty and the
job name in `ContainerJobName_s`. A rule written the obvious way parses,
runs, costs money and matches nothing, silently, forever. Every query in
the script was executed against the live workspace first.

**The suppression window is derived from the job crons**, not written out
again — change `cronExpression` and the window follows. That is the same
reason the parity checker verifies the cron against the guard: this
window is already spelled out in three places and does not need a fourth.

## What is NOT here

There is no Bicep, Terraform or ARM template for the container apps
themselves, so the ~55 environment variables per app are set by hand and
drift freely from `.env.production.example`. That is a known gap, tracked
separately; these three YAMLs are not a substitute for it.

## rbac/

| file | purpose |
| --- | --- |
| `georag-nightly-scheduler-role.json` | custom role definition for the two nightly sweeps |
| `apply-scheduler-role.sh` | creates it, grants it, and revokes Contributor -- in that order |

You cannot verify the role by running the jobs by hand: both sweeps open
with a DST double-fire guard that exits 0 unless the US-Pacific local hour
matches their target, so a manual start is a no-op. The next scheduled
fire is the test.

## Environment settings applied by hand

These live on `georag-env-cc` / the storage account / the workspace and
have no file here, because each is a single flag with no shape to review.
Recorded so they are not silently lost or "discovered" again:

| setting | value | why |
| --- | --- | --- |
| `peerTrafficConfiguration.encryption` | enabled 2026-08-23 | Encrypts all traffic inside the environment with an Azure-managed private certificate. The documented cost is latency and throughput under high load, which does not describe this deployment. |
| `peerAuthentication.mtls` | **left disabled** | A different thing from the above, and not a free win: ACA's mTLS is client-certificate mode on ingress, so every caller would have to present a certificate and every callee parse `X-Forwarded-Client-Cert`. Nothing here does either. The docs are also explicit that the runtime does not support authorization between apps via peer encryption, so this would buy authentication we already have inside the environment boundary. |
| Log Analytics `dailyQuotaGb` | 2 | Was uncapped. The 30-day peak is 0.185 GB/day and the mean 0.086, so this is ~11x the worst day observed and bounds a runaway at roughly $166/month instead of nothing. |
| Storage `allowSharedKeyAccess` | **still enabled** | Cannot be disabled yet. Laravel's `temporaryUrl()` signs export and figure download URLs with the account key, and `microsoft/azure-storage-blob` ^1.1 has no user-delegation-key SAS (the managed-identity equivalent). Blob read/write traffic is already 100% managed identity on every tier; the key's only remaining use is local SAS signing. Closing it means either proxying downloads through the app or hand-rolling a delegation SAS against the REST API. |
| Storage network `defaultAction` | **still Allow** | Restricting it needs stable egress IPs, which needs the environment to be VNet-integrated. `vnetConfiguration` is null. |
| `laravel-octane-cc` replicas | min 2 / max 2, 2026-08-23 | Was 1/1, so every deploy, node drain and platform restart was a user-visible outage on the only externally-facing app that serves pages. `--max-replicas 2` does NOT buy this: ACA only spawns the second replica under load, so the standby has to be a floor, not a ceiling. Checked before applying — `max_connections` is 429 and the 24h peak was 99, and the second replica moved the observed count by ~0 because Octane opens PDO connections lazily per worker. Safe to scale horizontally at all because session, cache and queue drivers are all redis and the container runs `octane:start` only (no scheduler to double-fire). Max stays at 2 deliberately: the app has no scale rule, and letting the implicit HTTP scaler burst is a separate, untested decision. |
