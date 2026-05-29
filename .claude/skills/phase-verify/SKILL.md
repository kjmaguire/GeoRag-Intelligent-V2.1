---
name: phase-verify
description: Acceptance-test commands and pass criteria for each phase of the GeoRAG implementation kickoff. Use when running phase verification, debugging a failing acceptance test, or confirming a "Definition of done" before closing a phase step. Triggers on conversation mentions of "Phase 0", "Phase 1", "acceptance test", "definition of done", "verify the phase", or specific phase-numbered steps.
metadata:
  origin: GeoRAG project — derived from georag-phase0-implementation-kickoff.md "Definition of done" sections (full content pending; see status below).
  authoritative-sources:
    - georag-phase0-implementation-kickoff.md (per-phase verification commands — NOT YET IN REPO; this skill is a scaffold)
    - master plan v2.4.2 (referenced from kickoff doc)
    - registry v1.3 (agent / artifact catalog)
  scope: Per-phase acceptance commands. One section per phase step. Becomes load-bearing when the kickoff doc lands in the repo.
status: |
  SCAFFOLD — content pending. The kickoff doc + master plan + registry are
  not yet committed to this repo. The frontmatter + structure are stubbed
  so this skill triggers on the right tasks; flesh out each phase section
  as the kickoff doc steps land.
---

# GeoRAG Phase Verification

> **Status:** Scaffold awaiting `georag-phase0-implementation-kickoff.md`. The directory + frontmatter exist so the skill triggers on phase-related conversations and you can fill in concrete commands as they're authored.

This skill collects the bash verification commands from each phase's "Definition of done" section in one place — copy-paste-ready, with expected outputs and "what to do if it fails" notes.

## When to Apply

Activate when:
- Closing a phase step ("Phase 0 step 8 acceptance")
- Debugging a failing verification
- Onboarding a new operator who needs to confirm their environment matches the phase definition

## Phase 0 — Foundations

### Step 1: <name>

> **Pending kickoff doc.** Replace this block with the actual step name + acceptance commands.

```bash
# Verification (placeholder):
# <command from kickoff doc>
```

**Expected:** <pass criterion>
**On failure:** <pointer to runbook / ADR>

### Step 2: Schema deployment

> **Pending kickoff doc.** Likely covers `php artisan migrate` + raw SQL companion verification. See `postgres-migration` skill for the migration-side patterns.

```bash
# Verification (likely shape — confirm against kickoff doc when available):
docker exec georag-postgresql psql -U georag -d georag -c "\dt public.*"
docker exec georag-postgresql psql -U georag -d georag -c "\dt silver.*"
docker exec georag-postgresql psql -U georag -d georag -c "SELECT count(*) FROM information_schema.tables WHERE table_schema IN ('public','silver','bronze','gold','public_geoscience','audit')"
```

**Expected:** <pass criterion from kickoff doc>
**On failure:** Compare missing tables to `database/migrations/` filenames; run `php artisan migrate:status`.

### Step 3: <name>

> **Pending kickoff doc.**

### Step 4: Hatchet workflow registration

> **Pending kickoff doc + `hatchet-workflow` skill.**

### Step 5: <name>

> **Pending kickoff doc.**

### Step 6: <name>

> **Pending kickoff doc.**

### Step 7: <name>

> **Pending kickoff doc.**

### Step 8: Acceptance tests

> **Pending kickoff doc.** Likely uses Playwright MCP for the workflow-run dashboard render check (per `georag-claude-code-setup.md`).

```bash
# Verification (placeholder shape):
# - Postgres acceptance assertions
# - FastAPI /health + /v1/query smoke
# - Laravel /up + Inertia render
# - Reverb WS handshake
# - Workflow run dashboard renders the test workflow run (Playwright)
```

## Phase 1 — Knowledge Graph + Retrieval

> **Pending kickoff doc + master plan §6 / §04f reference.**

## Phase 2 — Ingestion + Audit

> **Pending kickoff doc.**

## Phase 3 → Phase 12

> **Pending kickoff doc.** Each phase gets its own section with the same shape: enumerated steps, copy-paste verification commands, expected outputs, on-failure pointers.

## Cross-cutting verification helpers

Commands that apply across phases (independent of the kickoff doc):

```bash
# Stack-wide health snapshot:
docker ps --format "table {{.Names}}\t{{.Status}}" | grep georag-

# Backend resolution check:
docker exec georag-fastapi python -c "from app.config import settings; print(settings.LLM_BACKEND, settings.effective_llm_model)"

# Migration status:
docker exec georag-laravel-horizon php artisan migrate:status

# Pint clean check:
docker exec georag-laravel-horizon vendor/bin/pint --test --format agent
```

## How to flesh this out

When the kickoff doc lands in `docs/`:

1. Replace each `> **Pending kickoff doc.**` placeholder with the actual step name + commands.
2. Lift the verification commands verbatim — don't paraphrase. Operators should be able to copy-paste.
3. Add **Expected:** with the literal expected output (count, status string, exit code) — not "should work".
4. Add **On failure:** with a concrete next step (runbook path, log to inspect, owner to ping).
5. Update the `status:` frontmatter to remove the SCAFFOLD warning.
6. Cross-reference back to `postgres-migration`, `hatchet-workflow`, `agent-wrapper`, `audit-emit` skills where steps overlap them.
