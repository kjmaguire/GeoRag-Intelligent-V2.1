# Archived runbooks — do not follow these

These 40 documents describe the pre-Azure, docker-compose stack. They
were moved here on 2026-08-22 because **partial accuracy is more
dangerous than an explicit gap**: at 02:00 a runbook that is 70% right
sends someone confidently in the wrong direction, and every one of these
is 0% right about where the system runs.

Measured before archiving: `grep -rl 'containerapp\|Container Apps'
ops/runbooks/*.md` matched **1 of 41 files**. Not one of these 40
mentions Azure Container Apps or the `az` CLI.

What they still reference, none of which exists in the live deployment:
SeaweedFS, MinIO, PgBouncer, Neo4j, Dagster, Prometheus, Grafana, Loki,
Alertmanager, Ollama, and `docker compose`.

The two that mattered most:

- `on-call.md` opens by telling the operator to acknowledge the alert in
  the **Alertmanager UI on port 9093**. Nothing listens there. Its whole
  triage tree is keyed on PromQL alert names (`*LowAvailability`,
  `*HighLatencyP95`) that have never existed in this deployment.
- `service-outage.md` leads with PgBouncer, which was deleted, and every
  command in it is `docker logs georag-<x>` against a Docker host that
  does not exist.

**Use `ops/runbooks/azure-oncall.md` instead.**

## Why keep them at all

Three reasons they are archived rather than deleted:

1. Several contain domain reasoning that is still correct even though the
   commands are not — `refusal-rate-spike.md` on what a refusal spike
   *means*, `retrieval-tuning.md` on which knob affects what,
   `dr-1-postgres-loss.md` on recovery ordering.
2. The on-prem and air-gapped deployment targets in `kubernetes/` and
   `charts/` still run a compose-shaped stack, so parts of these apply
   there.
3. Git history alone would not surface them to someone writing the Azure
   equivalents.

If you port content out of one of these, port the reasoning and rewrite
the commands. Do not copy a command from this directory into a current
runbook.
