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

`redis.yaml` is the one file here that must **not** be applied with a
bare `az containerapp update --yaml`. It has to carry a `secrets:` block
for Container Apps to accept the shape, and that block's value is the
placeholder `REPLACE_AT_DEPLOY_TIME` — sending it sets the live
`redis-password` secret to that literal string and every client fails
auth. `apply-redis.sh` strips the block, refuses to send anything still
holding the placeholder, and verifies the secret survived.

Both scheduler jobs run with a system-assigned identity holding
Contributor on the `georag` resource group.

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
