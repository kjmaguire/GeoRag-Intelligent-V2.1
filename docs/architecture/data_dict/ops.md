# Schema `ops` — Data Dictionary (skeleton)

Created by [2026_05_13_140100](../../../database/migrations/2026_05_13_140100_create_ops_support_schema.php).

## Tables

| Table | Purpose | Status |
|---|---|---|
| `ops.support_tickets` | Per-ticket envelope for SupportCockpit | Live |
| `ops.support_ticket_traces` | trace_id / Tempo deep-link per ticket | Live |
| `ops.support_replay_runs` | Per-replay-run state for the support_replay Hatchet workflow | Live |

## Reader

Frontend [SupportCockpit.tsx](../../../resources/js/Pages/Foundry/SupportCockpit.tsx).
Hatchet `support_replay` writes `support_replay_runs` and broadcasts
`ReplayProgress` on Reverb.
