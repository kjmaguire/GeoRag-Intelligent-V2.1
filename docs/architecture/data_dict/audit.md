# Schema `audit` — Data Dictionary (skeleton)

See [Ch 03 §7](../manual/03-schemas.md), [Ch 12 §6](../manual/12-observability.md),
and [docs/audit_ledger_hash_recipe.md](../../audit_ledger_hash_recipe.md).

## Tables

| Table | Created by | Status |
|---|---|---|
| `audit.audit_ledger` | [phase0/20-layer-b-audit-ledger.sql:25](../../../database/raw/phase0/20-layer-b-audit-ledger.sql) | Live — partitioned monthly via pg_partman, 24-month retention |
| `audit.audit_ledger_verification_runs` | same SQL :92 | Live |
| `audit.audit_ledger_chain_fork_quarantine` | [2026_05_19_180400](../../../database/migrations/2026_05_19_180400_audit_ledger_chain_fork_quarantine.php) | Live |
| `audit.query_audit_log` | original `public.query_audit_log` → moved by [2026_05_07_120000](../../../database/migrations/2026_05_07_120000_move_query_audit_log_to_audit_schema.php) | Live |
| `audit.integration_credentials_audit` | [phase0/80-layer-h-credentials-audit.sql:15](../../../database/raw/phase0/80-layer-h-credentials-audit.sql) | Live |

## Triggers

- `audit_ledger_compute_hash_trg` (BEFORE INSERT on `audit_ledger`) — calls `audit.compute_audit_hash()`; locks previous row of same workspace `FOR UPDATE`; SHA-256 of canonical message including `previous_hash`. See [Ch 03 §10](../manual/03-schemas.md).

## Functions exposed to `georag_app`

`audit.compute_audit_hash()`, `audit.recompute_hash(...)`,
`audit.verify_hash_chain(...)`, `audit.run_verification(...)` — see
[phase1/10-georag-app-role.sql:66-69](../../../database/raw/phase1/10-georag-app-role.sql).

## Daily verifier

Hatchet workflow `audit_ledger_verify` (cron `0 2 * * *` UTC) replays the
previous 24h chain and writes to `audit.audit_ledger_verification_runs`.
Chain forks land in `audit.audit_ledger_chain_fork_quarantine` and page
Alertmanager.
