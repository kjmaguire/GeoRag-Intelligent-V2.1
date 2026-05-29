# Schema `workflow` — Data Dictionary (skeleton)

See [Ch 03 §8](../manual/03-schemas.md), [Ch 07 §2](../manual/07-orchestration.md).

## Tables

| Table | Purpose | Status |
|---|---|---|
| `workflow.workflow_runs` | App-side mirror of Hatchet engine workflow runs; partitioned monthly via pg_partman; carries `workspace_id` so RLS applies | Live |
| `workflow.workflow_run_events` | Per-event log for a run | Live |
| `workflow.workflow_run_steps` | Per-step status + duration + retry count | Live |
| `workflow.flow_registry` | Per-flow encrypted JWT key registry (Kestra↔FastAPI). Encrypted via pgcrypto with `AUDIT_ENCRYPTION_KEY`. | Live (see [phase4/20-flow-registry-table.sql](../../../database/raw/phase4/20-flow-registry-table.sql)) |

## Writers

- Laravel reads via the `pgsql_hatchet` connection for the Hatchet Worker Dashboard.
- FastAPI `services/flow_jwt.py` reads/writes `flow_registry`.

## Reapers

- `flow_jwt_key_reaper` Hatchet workflow (weekly cron) rotates per-flow keys past TTL.
