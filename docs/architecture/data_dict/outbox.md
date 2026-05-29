# Schema `outbox` — Data Dictionary (skeleton)

See [Ch 04 §6](../manual/04-ingestion-flow.md) and
[Appendix A §10](../appendix/A-medallion-contract.md#10-outbox-fan-out).

## Tables

| Table | Purpose | Status |
|---|---|---|
| `outbox.pending_propagations` | One row per (silver write → target store) pending fan-out. Polled `FOR UPDATE SKIP LOCKED` by the dispatcher. | Live |
| `outbox.propagation_attempts` | One row per dispatch attempt; status ∈ `succeeded\|transient_failure\|dead_lettered` | Live |

## Dispatcher

Hatchet workflow `outbox_dispatcher` (cron `* * * * *`). 3 transient failures → dead-letter. Each attempt is recorded in `propagation_attempts`. Idempotent per target. See [Appendix B §5](../appendix/B-event-payloads.md#5-outbox-payload) for the payload contract.
