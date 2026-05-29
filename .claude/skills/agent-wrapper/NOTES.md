# Skill: `agent-wrapper` — pending authoring

> **Status: NOT YET A SKILL.** Claude Code only loads `SKILL.md`; this NOTES.md keeps the skill inert until source content is canonical.

## Purpose (per `georag-claude-code-setup.md`)

The `@georag_agent` decorator (Python / FastAPI side) and `AgentInvoker` class (PHP / Laravel side) pattern from master plan **§35.1 Operational Contract**. Every new agent built from Phase 4 onward uses this.

## Required source documents (NOT YET IN REPO)

- **Master plan v2.4.2 §35.1 Operational Contract** — the canonical decorator + invoker definitions
- **Registry v1.3** — list of agents per phase + their risk tiers
- **`georag-phase0-implementation-kickoff.md`** — Phase 0 builds the first agent and pins the contract; this skill defers to those concrete examples

## Likely overlap with existing skills

This skill should explicitly **defer to** `georag-octane-bridge` for HTTP-bridge mechanics:
- Agent-wrapper covers: decorator metadata, risk-tier hooks, idempotency keys
- Octane-bridge covers: HTTP client construction, JWT minting, chunked streaming forwarding

If an agent calls FastAPI from Laravel, *both* skills apply — agent-wrapper for the agent's own contract, octane-bridge for the wire-level call shape.

## Authoring scope when source lands

Body should cover:

1. **Python decorator** (`@georag_agent`) — required metadata fields:
   - `name` (matches registry entry)
   - `risk_tier` (R0–R5)
   - `phase` (when first introduced)
   - `prompt_id` (versioned prompt registry reference)
   - any other §35.1 fields
2. **PHP equivalent** (`AgentInvoker` or attribute-based, depending on §35.1)
3. **Failure-recovery hook patterns per risk tier:**
   - R2: best-effort retry, log on final failure
   - R3: idempotency key + bounded retry
   - R4: idempotency key + retry + audit on every attempt
   - R5: idempotency key + retry + audit + manual review queue on final failure
4. **Idempotency-key recipe** — what fields go into the key per tier (workspace_id + project_id + payload_hash, etc.)
5. **Annotated examples** — at least one full agent in each language

## Frontmatter to use when authoring SKILL.md

```yaml
---
name: agent-wrapper
description: GeoRAG agent contract — the @georag_agent decorator (Python/FastAPI) and AgentInvoker pattern (PHP/Laravel) from master plan §35.1 Operational Contract. Use when creating or modifying a GeoRAG agent, when reviewing agent code in PRs, or when wiring failure-recovery hooks per risk tier. Triggers on @georag_agent, AgentInvoker, risk_tier, R0/R1/R2/R3/R4/R5, idempotency_key, or "agent contract".
metadata:
  origin: GeoRAG master plan §35.1 Operational Contract
  authoritative-sources:
    - <master-plan-path> §35.1
    - <registry-path> agent catalog
  scope: The agent CONTRACT (decorator metadata, hooks, idempotency). Does not cover HTTP transport — see georag-octane-bridge for that.
  see-also:
    - georag-octane-bridge (HTTP wire-level, JWT, streaming)
    - georag-rag-citations (citation-first enforcement on agent outputs)
---
```

## Watch out for

- **Don't redefine §35.1.** Quote the canonical decorator signature; don't paraphrase risk-tier semantics.
- **Reconcile with `georag-octane-bridge`.** Where they overlap, this skill points there rather than duplicating.

## Author trigger

Promote NOTES.md → SKILL.md when:
1. Master plan §35.1 text is available, AND
2. At least one Phase 0 agent has been built with the actual decorator (so we can show a real annotated example, not a synthetic one)
