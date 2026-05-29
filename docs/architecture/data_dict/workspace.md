# Schema `workspace` — Data Dictionary (skeleton)

See [Ch 11 — Tenancy + RLS](../manual/11-tenancy-and-rls.md) for the
tenancy spine. This schema holds multi-tenancy + RBAC + agent budgets.

## Tables (per Phase 0 §3.2)

| Table | Purpose | Status |
|---|---|---|
| `workspace.workspaces` | (mirror entry — canonical is `silver.workspaces`; this schema's table predates the silver move and persists for back-compat reads from agent paths) | Live |
| `workspace.users` | Workspace-scoped user roster | Live |
| `workspace.memberships` | User ↔ workspace ↔ role join | Live |
| `workspace.workspace_roles` | Per-workspace + system-global roles. System-global rows have `workspace_id IS NULL` and are visible to all sessions per [phase0/95-rls-policies.sql:101-114](../../../database/raw/phase0/95-rls-policies.sql) | Live |
| `workspace.agent_permissions` | Per-agent per-workspace tool grants | Live |
| `workspace.approval_requirements` | Tier-3 + sensitive-op approval gating | Live |
| `workspace.tool_invocations` | Per-tool-call audit ledger (Pydantic-AI surface) | Live |
| `workspace.agent_timeouts` | Per-agent timeout budgets used by the LangGraph route node | Live |
| `workspace.prompt_versions` | Versioned LLM prompts | Live |
| `workspace.idempotency_keys` | Idempotency keys for FastAPI mutations. `georag_app` has explicit DELETE here. | Live |
| `workspace.dry_run_outputs` | Dry-run preview results. `georag_app` has explicit DELETE here. | Live |
| `workspace.entities` | Workspace-scoped entity bag (NER + manual). **Canonical home for what older docs called `silver.entities`.** | Live |
| `workspace.flow_registry` | Per-flow encrypted JWT key registry (Kestra↔FastAPI auth). Holds pgcrypto-encrypted private keys. | Live |

## Functions

- `workflow.flow_jwt.*` family (technically lives in `workflow` schema) — encrypts/decrypts entries in `workspace.flow_registry`. See [phase6/10-flow-jwt-keys-multikid.sql](../../../database/raw/phase6/10-flow-jwt-keys-multikid.sql).

## RLS

All tables enabled in Phase 0 sweep + the May-25 reconciliation. See
[Ch 11 §5](../manual/11-tenancy-and-rls.md).
