# Schema `usage` — Data Dictionary (skeleton)

See [Ch 03 §8](../manual/03-schemas.md).

## Tables

| Table | Purpose | Status |
|---|---|---|
| `usage.usage_events` | Per-call event log (LLM tokens, tool invocations, exports) | Live |
| `usage.usage_aggregates_daily` | Daily rollups joined to `workspace_id` | Live |
| `usage.workspace_cost_ceilings` | Per-workspace hard cost ceiling (Tier 3 gating) | Live |
| `usage.workspace_cost_quotas` | Per-workspace quota windows (drives `cost_burn_watcher`) | Live |

## Writers

- FastAPI writes `usage.usage_events` for every chat turn from `persist_node` in `app/agent/agentic_retrieval/nodes.py` (and from `app/agents/wrapper.py` for the tool-agent path).
- Hatchet `cost_burn_watcher` (cron `*/15 * * * *`) re-aggregates daily rollups + alerts.
- Laravel jobs write export events for download tracking.
