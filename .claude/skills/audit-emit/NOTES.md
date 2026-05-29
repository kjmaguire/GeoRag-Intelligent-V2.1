# Skill: `audit-emit` — pending authoring

> **Status: NOT YET A SKILL.** Claude Code only loads `SKILL.md`; NOTES.md keeps this inert.

## Purpose (per `georag-claude-code-setup.md`)

Standard pattern for `emit_audit()` calls — what `action_type` values to use, payload conventions, hash-chain considerations. Used everywhere the system writes to `audit_ledger`.

## Required source documents (NOT YET IN REPO)

- **Master plan v2.4.2** — audit-ledger schema + hash-chain mechanics
- **Action-type taxonomy** — the canonical list (`agent.invoke`, `workspace.create`, `report.signoff`, etc.)
- **`audit_ledger` migration** — when it lands in `database/migrations/`, the SKILL.md should cross-reference its exact column shape
- **Registry v1.3** — agents that emit audits, action types per agent

## Likely overlap with existing skills

This skill should **defer to** `georag-rag-citations` for RAG-response audit writes:
- `audit-emit` covers: action_type taxonomy, payload conventions, hash-chain mechanics, emit_audit() signature
- `georag-rag-citations` covers: citation persistence, answer_runs persistence, query.validated/refusal events specifically for RAG

If a query controller is writing both `query.dispatched` to audit_ledger AND citations/answer_runs, both skills apply.

## Authoring scope when source lands

Body should cover:

1. **Action-type taxonomy** — full canonical list grouped by domain:
   - Agent lifecycle: `agent.invoke`, `agent.complete`, `agent.refuse`, `agent.error`
   - Workspace: `workspace.create`, `workspace.suspend`, `workspace.delete`
   - Reports: `report.signoff`, `report.export`, `report.recall`
   - Query: `query.dispatched`, `query.validated`, `query.refused`
   - Ingestion: `ingest.start`, `ingest.complete`, `ingest.fail`
   - (etc. — full list comes from master plan)
2. **Canonical payload structures** per action class — what fields are required vs. optional
3. **`target_id` semantics** — when to set, when to leave NULL
4. **`workspace_id` requirement** — RLS depends on this; forgetting it is a top-tier bug
5. **PII discipline** — no operator names/emails in payload (or hash them); no licensed-data quotes
6. **Hash-chain mechanics** — how each row's `hash` is computed from `previous_hash` + payload (per `docs/audit_ledger_hash_recipe.md`), and why integrity verification scans the chain
7. **Common mistakes:**
   - Forgetting `workspace_id` (RLS hides the row)
   - Including PII in payload
   - Mismatched action_type capitalization
   - Skipping `target_id` for actions that have a clear target

## Frontmatter to use when authoring SKILL.md

```yaml
---
name: audit-emit
description: Standard pattern for emit_audit() calls — action_type taxonomy, payload conventions, hash-chain mechanics, workspace_id/target_id rules. Use any time code writes to audit_ledger or audit-related tables. Triggers on emit_audit, audit_ledger, action_type, hash_chain, "audit trail", or any code path that needs an audit row.
metadata:
  origin: GeoRAG master plan — audit-ledger schema + canonical action-type taxonomy
  authoritative-sources:
    - <master-plan-path> (audit-ledger section)
    - database/migrations/<audit_ledger_migration>.php (when authored)
    - <registry-path> agent → action_type mapping
  scope: Server-side audit emit pattern. RAG-specific audit writes (citations, answer_runs) are covered separately by georag-rag-citations.
  see-also:
    - georag-rag-citations (RAG-response audit persistence)
    - agent-wrapper (agent.* action types fire from the decorator's hooks)
---
```

## Watch out for

- **Don't invent action types.** The taxonomy is closed; new types require master plan update + senior-reviewer signoff.
- **Reconcile with `georag-rag-citations`.** Where they overlap, this skill points there rather than re-asserting.
- **Hash-chain integrity is non-negotiable.** Any code that touches the chain (e.g. backfill) needs its own ADR.

## Author trigger

Promote NOTES.md → SKILL.md when:
1. The master plan section on audit-ledger is available, AND
2. The `audit_ledger` migration is in `database/migrations/` (so column shape is concrete), AND
3. The canonical action-type list is referenceable
