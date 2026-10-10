# Schema `ops` — Data Dictionary (skeleton)

Created by [2026_05_13_140100](../../../database/migrations/2026_05_13_140100_create_ops_support_schema.php).

## Tables

| Table | Purpose | Status |
|---|---|---|
| `ops.support_tickets` | Per-ticket envelope for SupportCockpit | Live |
| `ops.support_ticket_traces` | trace_id / Tempo deep-link per ticket | Live |
| `ops.support_replay_runs` | Per-replay-run state for the support_replay Hatchet workflow | Live |

## Columns added 2026-10-10

[2026_10_10_120000](../../../database/migrations/2026_10_10_120000_support_replay_idempotency_and_workspace_columns.php):

| Column | Purpose |
|---|---|
| `ops.support_replay_runs.replay_request_id` | Idempotency key (UNIQUE) minted by Laravel per replay request. A second dispatch with the same key returns the first run's row and runs nothing; the failure hook finds a run's row by it |
| `ops.support_replay_runs.workspace_id`, `ops.support_ticket_traces.workspace_id` | The ticket's workspace. `database/raw/phase0/98-rls-tenant-isolation-block3.sql` makes both NOT NULL with a strict tenant policy; the writers now supply it |

## Reader

Frontend [SupportCockpit.tsx](../../../resources/js/Pages/Foundry/SupportCockpit.tsx).
Hatchet `support_replay` writes `support_replay_runs` and broadcasts
`ReplayProgress` on Reverb.
