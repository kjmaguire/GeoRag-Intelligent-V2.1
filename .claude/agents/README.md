# GeoRAG Claude Code Agents

This directory contains the Claude Code subagent definitions and project-level
context for the GeoRAG build. Installing these gives Claude Code specialized
workers for each part of the stack, with model assignments optimized for a
Max 100 plan (minimal Opus usage, Sonnet workhorse, Haiku for boilerplate).

## What's in here

```
georag-agents/
├── README.md                    # This file
├── senior-reviewer.md           # Opus — milestone gate reviews (read-only)
├── backend-laravel.md           # Sonnet — Laravel + Octane + Horizon + Reverb
├── backend-fastapi.md           # Sonnet — FastAPI + Pydantic AI + RAG
├── data-engineer.md             # Sonnet — Dagster + ingestion + PostGIS + formats
├── graph-engineer.md            # Sonnet — Neo4j Community + Cypher
├── frontend-engineer.md         # Sonnet — React + Inertia + shadcn/ui + viz
├── devops-engineer.md           # Sonnet — Docker Compose + deployment + tuning
├── test-engineer.md             # Sonnet — all testing + golden queries
└── boilerplate-writer.md        # Haiku — migrations + docstrings + scaffolding
```

## Installation

The agent definitions are already committed in `.claude/agents/`. There is no
copy step — Claude Code discovers the agents from this directory automatically:

```bash
# Verify agents are in place
ls .claude/agents/*.md
```

Project context lives at the project root: `./CLAUDE.md`. Claude Code auto-loads
it into every session — including subagents launched from this directory.
**Edit it directly; do not duplicate it here.** A previous version of this
README described a "source-of-truth copy" pattern under `.claude/agents/`;
that pattern was retired because the two copies drifted in practice. The
project root is now the single source of truth.

## Commit these to the repo

```bash
git add .claude/agents/ CLAUDE.md
git commit -m "chore: add Claude Code agents and project context"
```

Committing these files means any contractor who clones the project gets the
same specialized workers with the same instructions. This is the point —
consistency across anyone working on the codebase.

## How Claude Code uses them

### Automatic delegation

When you ask Claude Code a question in the main session, it reads the
`description` field of each agent and auto-delegates to the best fit:

- "Build a migration for the collars table" → routes to `boilerplate-writer` (Haiku)
- "Implement the CRS detection pipeline" → routes to `data-engineer` (Sonnet)
- "Review the hallucination prevention layer" → routes to `senior-reviewer` (Opus)

### Explicit invocation

You can ask Claude Code to use a specific agent in natural language:

```
Use the backend-fastapi agent to implement the query_spatial tool for the Pydantic AI agent
```

This skips auto-delegation and goes straight to the named agent.

### Parallel execution

You can request multiple agents run in parallel:

```
Run these in parallel:
1. Use test-engineer to write golden query tests for spatial queries
2. Use boilerplate-writer to update the README with the new endpoints
3. Use data-engineer to review the Bronze → Silver transformation logic
```

Each runs in its own context window, saving your main session from bloat.

## Model budget strategy

This setup assumes Max 100 plan with shared usage. Model assignments are:

| Model | Agents | Usage Frequency |
|---|---|---|
| **Opus** | `senior-reviewer` only | Rare — milestone gates only |
| **Sonnet** | 7 specialized builders | Primary workhorse |
| **Haiku** | `boilerplate-writer` | Aggressive for pattern-matching tasks |

### When to invoke senior-reviewer

The Opus-powered `senior-reviewer` should be invoked ONLY at these moments:
- Before milestone sign-off (verifies Section 07e acceptance criteria)
- When an architectural decision doesn't match the doc
- When code touches hallucination prevention layers (Section 04i)
- When Laravel ↔ FastAPI interface contracts change
- When you're unsure if something is Octane-safe or async-correct

Do NOT invoke it for:
- Routine code review
- Bug investigation
- "Looks good?" checks
- Implementation questions that should go to the specialist agents

The Opus budget is shared with Kyle's other work. Every unnecessary invocation
is a real cost.

### When to use boilerplate-writer

Haiku is cheap and fast. Use `boilerplate-writer` aggressively for:
- Generating migrations from schemas in Section 04e
- Writing docstrings and comments
- Updating README files
- Scaffolding Eloquent models, Pydantic models, TypeScript types
- CHANGELOG entries
- Simple config file templates

Anything where the work is pattern-matching against existing code or specs.

## Customizing the agents

Each agent file is a markdown document with YAML frontmatter. Edit them as
the project evolves:

```yaml
---
name: agent-name
description: When to use this agent
tools: Read, Write, Edit, Bash, Glob, Grep
model: sonnet   # or opus, haiku, inherit
color: cyan     # visual marker in the CLI
---

System prompt goes here...
```

When you update an agent, commit the change so the contractor picks it up.

## Related resources

- **Architecture doc**: `georag-architecture.html` — the authoritative spec
- **Claude Code docs**: https://docs.anthropic.com/en/docs/claude-code
- **Pydantic AI docs**: https://ai.pydantic.dev/ (for `backend-fastapi` work)
- **shadcn/ui docs**: https://ui.shadcn.com/ (for `frontend-engineer` work)

## Troubleshooting

**Claude Code doesn't see the agents**
- Verify they're in `.claude/agents/` (project-level) not elsewhere
- Run `ls .claude/agents/*.md` to confirm files are in place
- Check YAML frontmatter syntax — bad frontmatter silently disables an agent

**The wrong agent is getting invoked**
- Refine the `description` field with clearer trigger keywords
- Use explicit `@agent-name` invocation for critical paths

**Opus budget is getting burned**
- Check if `senior-reviewer` is auto-delegating for non-review tasks — tighten its description
- Use explicit invocation for everything except the main session's natural delegation

**A Sonnet agent is struggling with a hard problem**
- Escalate explicitly: "This is complex — review with senior-reviewer before implementing"
- Don't just change the agent's model to Opus; that defeats the budget strategy
