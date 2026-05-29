# Phase 0 raw SQL — substrate foundation

These twelve `.sql` files install the multi-tenant substrate that every
later layer (silver tables, audit, workflow telemetry, RLS) builds on.
Apply them once per database **as a superuser**, before any Laravel
migration runs:

```bash
docker compose exec -T postgresql \
    psql -U postgres -d georag \
    -f /var/lib/postgresql/init/phase0/10-layer-a-workspace-foundation.sql
# ... repeat in numeric order for every file in this directory
```

Files apply in lexicographic order. The `100-`, `110-`, `120-` files run
**after** `95-rls-policies.sql` (numeric sort: 10 → 20 → … → 95 → 100 →
110 → 120). Don't renumber unless you re-order dependencies first.

## GUC contracts

Phase 0 introduces four **per-connection PostgreSQL GUC settings** that
the rest of the stack reads through `current_setting(...)` inside RLS
policies, encryption helpers, and audit triggers. Whoever opens a
PostgreSQL connection is responsible for setting them correctly — RLS
policies elsewhere assume they're either set to a real workspace/project
UUID or unset (single-tenant fallback).

### `georag.project_id` (uuid, transaction-local)

* **Set by**: FastAPI `AgentDeps.acquire_scoped()` in
  `src/fastapi/app/agent/deps.py`, immediately after acquiring a pooled
  asyncpg connection.
* **Read by**: Project-scoped RLS policies installed in
  `database/migrations/2026_04_17_120200_replace_toothless_rls_with_guc_aware_policies.php`
  on `silver.collars`, `silver.samples`, `silver.well_log_curves`,
  `silver.lithology_intervals`, and the Neo4j-backed
  `silver.evidence_items`.
* **Contract**:
  ```sql
  USING ( current_setting('georag.project_id', true) IS NULL
          OR project_id = current_setting('georag.project_id', true)::uuid )
  ```
  When **unset** (single-tenant / Dagster / admin scripts) the policy
  admits every row. When **set** it admits only rows whose
  `project_id` column matches.
* **Set with**:
  ```python
  await conn.execute(f"SET LOCAL georag.project_id = '{project_uuid}'")
  ```
  Note: `SET LOCAL` is transaction-scoped. asyncpg auto-commits implicit
  transactions, so callers must wrap with `async with conn.transaction()`
  or use `set_config(name, value, true)` instead.

### `georag.workspace_id` (uuid, transaction-local)

* **Set by**: `AgentDeps.acquire_scoped()` (Module 9 Chunk 9.3 onward)
  and every Hatchet workflow that needs to write workspace-scoped
  silver rows (`field_outcome_learning`, `evaluate_workspace`, etc.).
* **Read by**: Workspace-scoped RLS policies installed in
  `database/migrations/2026_04_22_170000_extend_rls_workspace_coverage.php`
  on `silver.evidence_items`, `silver.answer_runs`,
  `silver.answer_retrieval_items`, `silver.answer_citation_items`,
  `silver.answer_citation_spans`, `silver.document_revisions`,
  `silver.document_passages`, `silver.message_feedback`.
* **Contract**: identical shape to `georag.project_id` — null admits
  all, set admits matching `workspace_id`.

### `app.workspace_id` (uuid, transaction-local) — Laravel-side alias

* **Set by**: Laravel's `App\Database\GUCConnection` mixin (Octane
  pipeline) and Hatchet workflows that re-set both keys for
  belt-and-suspenders coverage.
* **Read by**: Newer migrations that landed via Laravel rather than the
  raw-SQL phase 0 path:
  * `2026_05_12_180008_enable_rls_phase3_silver_tables.php`
  * `2026_05_13_090000_create_silver_saved_map_views.php`
  * `2026_05_13_100000_create_targeting_schema.php` (and following)
* **Why two keys exist**: The Phase 0 substrate used `georag.*` to
  avoid collision with hypothetical `app.*` GUCs that PostgreSQL itself
  or downstream extensions might claim. Laravel-side migrations later
  picked `app.workspace_id` for symmetry with Laravel's `config('app.*')`
  conventions. Both keys must be set whenever code spans both schools
  of policy:
  ```python
  await conn.execute("SELECT set_config('app.workspace_id', $1, false)", ws_id)
  await conn.execute("SELECT set_config('georag.workspace_id', $1, false)", ws_id)
  ```
  Consolidation onto a single key is tracked under the production-
  readiness plan; until then **set both, always**.

### `app.audit_encryption_key` (text, transaction-local)

* **Set by**: Code paths that need to encrypt or decrypt secrets via
  pgcrypto inside SQL — currently only the external-notification
  sender registry and per-flow JWT key registry:
  * `src/fastapi/app/hatchet_workflows/external_notification.py:230`
  * `src/fastapi/app/services/per_flow_jwt.py` (and its sync sibling
    `flow_jwt._load_per_flow_key_sync`)
* **Read by** (pgcrypto `pgp_sym_encrypt` / `pgp_sym_decrypt`):
  * `database/raw/phase4/10-external-notification-senders.sql` —
    `usage.register_external_notification_sender()` and
    `usage.lookup_external_notification_sender_secrets()`
  * `database/raw/phase5/20-per-flow-jwt-keys.sql` — the analogous
    helpers for per-flow JWT signing keys
* **Contract**: when the helper function runs, the GUC **must** be set
  to the same 32-byte secret stored in `AUDIT_ENCRYPTION_KEY` env var.
  Helpers raise `EXCEPTION` if the GUC is missing — never silently
  encrypt with an empty key.
* **Why a GUC, not a function arg**: The encryption helpers are SQL
  functions invoked from many call sites (admin CLI, Laravel jobs,
  FastAPI workers). Threading a key parameter through every signature
  bloats the surface; the GUC pattern lets callers set it once per
  transaction and lets the function be a pure `SELECT … FROM …` call.
  See `docs/RUNBOOK.md` "Rotating the audit encryption key" for the
  rotation procedure (no application change needed).

## Single-tenant escape hatch

In single-tenant deployments (one workspace per database) callers may
leave the workspace / project GUCs unset. The RLS policies above use
`current_setting('...', true) IS NULL` as the "admit everything" branch
specifically so that:

* Dagster ingestion processes don't need to know about workspaces
* Backup-agent reads all rows for full-DB exports
* Admin CLI scripts (`php artisan georag:*`) work without per-command
  GUC plumbing

When `MULTI_TENANT_ENFORCEMENT_ENABLED=true` (FastAPI config), the
**application layer** refuses to start if `SINGLE_TENANT_MODE` is also
false and no workspace context is available — preventing accidental
multi-tenant deploys that rely on the fail-open escape hatch.

## Substrate verifier

`scripts/substrate_verify.py` enforces every contract in this file. The
nightly cron + the pre-promotion gate both run it; failures in any
contract block promotion. Treat substrate verifier output as the
authoritative end-to-end check that all four GUCs are wired correctly
across every layer.

## File-by-file reference

| File | Adds | Notes |
|---|---|---|
| `10-layer-a-workspace-foundation.sql` | `workspace`, `audit`, `usage` schemas; `workspace.workspaces` registry; pgcrypto + uuid-ossp extensions | Foundational — must run first |
| `20-layer-b-audit-ledger.sql` | `audit.audit_ledger` hash-chain table | Append-only ledger; tamper-evident |
| `30-layer-c-workflow-runs.sql` | `audit.workflow_runs` for Hatchet + Kestra tracing | trace_id ↔ Tempo cross-link |
| `40-layer-d-outbox.sql` | `outbox.events` for at-least-once dispatch | Polled by external_notification |
| `50-layer-e-operational-contract.sql` | `ops.runtime_contract` for healthcheck state | Reflects multi-tenant enforcement |
| `60-layer-f-usage-cost.sql` | `usage.*` cost-accounting tables | Per-tenant cost rollup |
| `70-layer-g-findings.sql` | `audit.findings` for tenant-isolation auditor results | Long-lived audit history |
| `80-layer-h-credentials-audit.sql` | `audit.credentials_audit` for secret rotations | Operator visibility |
| `90-audit-hash-chain-trigger.sql` | `BEFORE INSERT` trigger that fills `previous_hash` and computes `hash` on `audit_ledger` | Makes the chain self-maintaining |
| `95-rls-policies.sql` | RLS enable + base policies on the audit/workflow tables | Reads `app.workspace_id` |
| `100-audit-verify-function.sql` | `audit.verify_ledger_hash_chain()` | Used by substrate verifier |
| `110-phase0-agent-defaults.sql` | Default agent definitions for `app.agent_definitions` | Seeded Pydantic AI agents |
| `120-phase0-step6-support-packets.sql` | Support packet schema (`audit.support_packets`) | Read by Support Cockpit |

## Adding a new GUC

If you introduce a new GUC, update this README in the same commit
**and** add a substrate-verifier check that exercises the contract.
The verifier acts as the executable spec; this README is the human-
readable index.
