# Skill: `georag-context` — pending authoring

> **Status: NOT YET A SKILL.** This is a design-notes file. Claude Code only loads `SKILL.md` from a skills directory. Until SKILL.md exists here, this skill is inert — which is the right state for a skill we can't author authoritatively yet.

## Purpose (per `georag-claude-code-setup.md`)

A compact summary of GeoRAG's architectural posture, so Claude Code doesn't have to re-read the master plan + registry every session. ~100 lines.

## Required source documents (NOT YET IN REPO)

- **Master plan v2.4.2** — five-orchestrator architecture, §2 non-negotiables, §12 inference path lock, etc.
- **Registry v1.3** — full agent + artifact catalog
- **`georag-phase0-implementation-kickoff.md`** — phase-step boundaries
- **ADRs 0001–0005** — the canonical decision record set (only 0001 currently in `docs/adr/`)

## Authoring scope when source lands

Body should cover:

1. **Five-orchestrator architecture** — what each orchestrator owns
2. **§2 non-negotiable rules**, especially:
   - §2.9 public/private posture
   - §2.10 tooling discipline (no autonomous multi-step agent loops)
   - §2.11 regulatory language posture
3. **Risk tier system R0–R5** — what each tier requires (idempotency-key recipes, failure-recovery hooks, etc.)
4. **§12 inference path lock** — vLLM-served Qwen3-30B-A3B as the only inference path (post-cutover; the doc says `qwen3:30b-a3b` — confirm whether it should reflect the AWQ variant we're now serving)
5. **Pointers** to master plan + registry for full reference

## Frontmatter to use when authoring SKILL.md

```yaml
---
name: georag-context
description: Compact summary of GeoRAG architectural posture — five-orchestrator layout, §2 non-negotiables, R0–R5 risk tiers, and the §12 inference-path lock. Use early in any GeoRAG implementation session before reading individual specs. Auto-loads when working in the GeoRAG repo or when conversation references "GeoRAG", "agent" terminology, "risk tier", or specific orchestrator names.
metadata:
  origin: GeoRAG master plan v2.4.2 + registry v1.3 + ADRs 0001–<latest>
  authoritative-sources:
    - <master-plan-path>
    - <registry-path>
    - docs/adr/*.md
  scope: Architectural orientation for new sessions. Does not replace reading the master plan for specific decisions.
---
```

## Watch out for

- **Don't paraphrase the master plan.** Quote section numbers and page-equivalent anchors. The master plan is the source of truth; this skill is a compact index, not an alternative interpretation.
- **Keep under 100 lines.** Skills auto-load tokens — bloat dilutes the other skills.
- **Update §12 inference-path detail** to reflect the post-cutover state (vLLM + ELVISIO/Qwen3-30B-A3B-Instruct-2507-AWQ) once the master plan v2.4.2+ acknowledges it.

## Author trigger

Promote this NOTES.md → SKILL.md when:
1. Master plan v2.4.2 is in the repo at a known path, AND
2. Registry v1.3 is in the repo at a known path, AND
3. The user explicitly asks "author georag-context now"
