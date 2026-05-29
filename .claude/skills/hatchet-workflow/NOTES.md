# Skill: `hatchet-workflow` — pending authoring

> **Status: NOT YET A SKILL.** Claude Code only loads `SKILL.md`; NOTES.md keeps this inert until source content lands.

## Purpose (per `georag-claude-code-setup.md`)

How to register and structure Hatchet workflows per the conventions in master plan **§6** + Phase 0 kickoff **Step 4**.

## Required source documents (NOT YET IN REPO)

- **Master plan v2.4.2 §6** — workflow / orchestration conventions
- **`georag-phase0-implementation-kickoff.md` Step 4** — Hatchet bring-up + first workflow registration
- **Hatchet upstream docs** (Context7 MCP can fetch live) — version-pinned API for the workflow declaration

## Hatchet posture in GeoRAG (what's known so far)

- Hatchet is one of the orchestration paths alongside Dagster
- CLAUDE.md hard rule #7: **don't duplicate orchestration.** Laravel queues = user-triggered async. Dagster = scheduled/bulk pipelines. Hatchet's role within that split needs to be explicit when §6 lands.
- Audit-ledger emit at workflow start + end is the pattern (see `audit-emit` NOTES.md)

## Authoring scope when source lands

Body should cover:

1. **Workflow declaration template** — Python / TypeScript / whatever the project standardizes on
2. **Step function patterns:**
   - Idempotency contract per step
   - Retry policy (bounded, with jitter)
   - Timeout selection — how to pick based on the step's blast radius
3. **Audit-ledger emit at workflow start + end** — defer to `audit-emit` for the payload shape
4. **Outbox write pattern** — when a workflow updates secondary stores (Postgres + Qdrant + Neo4j), the outbox decouples the writes for consistency
5. **Verification commands** — `hatchet workflow list`, `hatchet workflow run`, etc.
6. **Boundary with Dagster + Laravel queues** — when to use Hatchet vs. each alternative (per §6 + CLAUDE.md hard rule #7)

## Frontmatter to use when authoring SKILL.md

```yaml
---
name: hatchet-workflow
description: Hatchet workflow registration + step structure for GeoRAG per master plan §6 + Phase 0 kickoff Step 4. Use when creating or modifying a Hatchet workflow, when adding a step function, or when deciding whether work belongs in Hatchet vs Dagster vs Laravel queues. Triggers on Hatchet, hatchet workflow, workflow.run, step function, or orchestration boundary questions.
metadata:
  origin: GeoRAG master plan §6 + Phase 0 kickoff Step 4
  authoritative-sources:
    - <master-plan-path> §6
    - <kickoff-doc-path> Step 4
    - https://docs.hatchet.run (live via context7 MCP)
  scope: Hatchet-side workflow + step authoring. Does not cover Dagster (separate path) or Laravel queues (CLAUDE.md hard rule #7 boundary).
  see-also:
    - audit-emit (workflow start/end ledger pattern)
    - agent-wrapper (when steps invoke agents)
---
```

## Watch out for

- **Hatchet's API moves fast.** When authoring, pull current docs via context7 MCP rather than relying on training data.
- **Respect rule #7.** Don't suggest Hatchet for work that belongs in Laravel queues or Dagster.

## Author trigger

Promote NOTES.md → SKILL.md when:
1. Master plan §6 is available, AND
2. Phase 0 kickoff Step 4 (Hatchet bring-up) is documented, AND
3. At least one workflow is registered in the repo so the example can be lifted from real code
