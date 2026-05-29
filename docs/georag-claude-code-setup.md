# GeoRAG Claude Code Setup Guide

**Document version:** 1.0
**Status:** Recommendations, not locked spec. Adjust based on real usage friction.
**Companion to:** `georag-phase0-implementation-kickoff.md` + master plan v2.4.2 + registry v1.3
**Date:** May 2026

---

## What this document is

A focused recommendation set for what to install in Claude Code to make GeoRAG implementation work go faster — covering skills (procedural knowledge), MCP servers (external tool access), and connectors. This is opinionated for *this project* and *this stack*, not a generic Claude Code setup.

The document covers:

- **Core MCP servers** every GeoRAG implementation session should have
- **Strong-fit MCP servers** worth installing for specific phases
- **Custom skills** to author for the GeoRAG project specifically
- **Connectors already enabled** in the Claude.ai project space
- **Setup commands** for each, with scope guidance
- **Discipline notes** — what to deliberately *not* install

When in doubt, install fewer things. Claude Code's Tool Search lazy-loads tool definitions on demand, but procedural discipline beats tool sprawl every time.

---

## Mental model: skills vs MCPs vs connectors

These three things solve different problems and shouldn't be confused:

| Concept | What it does | When to use |
|---|---|---|
| **Skill** (`.claude/skills/<name>/SKILL.md`) | Procedural knowledge — *how* to do something. Loaded on-demand based on task context. ~30–50 tokens overhead. | Repeated procedures: schema migrations, agent wrapping, ADR writing, phase verification. |
| **MCP server** | External tool connection — Claude can *do* something in an external system (query a database, push a PR, run a browser). | Connectivity: GitHub, PostgreSQL, Sentry, browser automation. |
| **Connector** (Claude.ai project space) | Pre-built MCP servers exposed by Anthropic for the Claude.ai web/desktop app and shared with Claude Code via the project. | Pre-authenticated services: Postman, Sentry, Google Drive, etc. |

**Heuristic:** procedural patterns → skill. External system access → MCP server. Already-authenticated SaaS → connector if available, MCP if not.

---

## Currently connected connectors (already enabled in Claude.ai project)

These were configured in the Claude.ai workspace per the system prompt. They're available in Claude Code automatically without additional install. **Use what's already here before adding new MCP servers.**

| Connector | URL | Use for GeoRAG |
|---|---|---|
| **Sentry** | mcp.sentry.dev | Error tracking and root cause investigation. Already in scope per project memory. Critical for Phase 0 onward — every agent emits errors here. |
| **Google Drive** | drivemcp.googleapis.com | Document storage. Useful for sharing architecture docs, ADRs, meeting notes. |
| **Gmail** | gmailmcp.googleapis.com | Search project-related email threads. Marginal but free. |
| **Google Calendar** | calendarmcp.googleapis.com | Scheduling. Marginal for implementation work. |
| **Microsoft Learn** | learn.microsoft.com/api/mcp | Microsoft documentation lookup. Marginal unless you touch Azure or .NET. |
| **Microsoft 365** | microsoft365.mcp.claude.com | OneDrive/SharePoint integration. Useful when SharePoint Archive Agent ships in Phase 7. |
| **Cloudflare Developer Platform** | bindings.mcp.cloudflare.com | Cloudflare resources. Use only if deployment touches Cloudflare (likely yes for SaaS edge). |
| **Postman** | mcp.postman.com/minimal | API spec work. **Actively used per project memory** — keep using. |
| **Hugging Face** | huggingface.co/mcp | Model exploration, dataset access. Useful for `Qwen/Qwen3-14B-AWQ` model card and any future evaluation work. |

**No action needed for these** — they're already authenticated and ready.

---

## Core MCP servers to install (must-have for GeoRAG)

These four MCP servers cover the highest-value Claude Code capabilities for GeoRAG implementation. Install all four with project scope so the team shares the same config.

### 1. GitHub MCP — repository operations

The single most valuable MCP for code work. Manages PRs, issues, branches, code search, file ops directly from Claude Code without context-switching to the browser.

```bash
# Install with project scope so the team shares config
claude mcp add github --scope project \
  -- npx -y @modelcontextprotocol/server-github
```

**Why for GeoRAG:** Phase 0 alone produces ~30 PRs (one per schema migration, one per agent build card, one per acceptance test). Without GitHub MCP, every PR requires terminal/browser context switching. With it, Claude Code can review diffs, write descriptions from the kickoff doc's verification commands, link issues, and manage labels without leaving the session.

**Auth:** Uses a GitHub Personal Access Token. Scope it minimally — `repo`, `workflow`, `issues`, `pull_requests`. Avoid `admin:org` unless explicitly needed.

### 2. PostgreSQL MCP — direct database access

GeoRAG is a database-heavy project. Phase 0 alone deploys 24 tables across 8 namespaces. Without direct DB access, Claude Code is guessing at schema state; with it, Claude Code can verify migrations, inspect table definitions, run sample queries, and validate the acceptance tests in the kickoff doc.

```bash
# Install with project scope (the database is per-project)
claude mcp add postgres --scope project \
  -- npx -y @modelcontextprotocol/server-postgres "$DATABASE_URL"
```

**Why for GeoRAG:** Phase 0's Step 2 (Schema deployment) and Step 8 (Acceptance tests) are mostly Postgres verification commands. Direct DB access lets Claude Code run those itself rather than asking you to run them and paste results.

**Safety:** Configure read-only by default (`POSTGRES_READONLY=true` env var). Phase 0 work should be migration-driven (versioned migrations in repo), not ad-hoc DB writes from Claude Code. The MCP is a verification tool, not a write path.

### 3. Filesystem MCP — secure local file ops

```bash
# Scope to the GeoRAG repo root only
claude mcp add filesystem --scope project \
  -- npx -y @modelcontextprotocol/server-filesystem /path/to/georag-repo
```

**Why for GeoRAG:** Phase 0 produces structured artifacts (migration files, agent code, test scripts, verification scripts). Filesystem MCP gives Claude Code controlled write access scoped to the repo without giving it full host filesystem access. Critical safety boundary.

**Note:** Claude Code already has built-in file editing tools. The filesystem MCP layers explicit permission control on top — for a serious project like GeoRAG, the explicit permission boundary is worth the overhead.

### 4. Context7 MCP — live library documentation

Documentation lookup for any library at the version actually pinned in your project. Replaces "what's the current Laravel Octane API?" guesses with the actual current docs.

```bash
claude mcp add context7 --scope user \
  -- npx -y @upstash/context7-mcp
```

**Why for GeoRAG:** the stack is broad and some pieces are at the edge of training data — Pydantic AI, Hatchet, LangGraph all evolve fast; Laravel and FastAPI ship breaking changes between major versions. Context7 looks up the actual docs for the version pinned in your `composer.json` / `pyproject.toml`, not what Claude trained on.

**Scope choice:** `--scope user` (global) since it's useful across all projects, not just GeoRAG.

---

## Strong-fit MCP servers (install per phase as needed)

These add real value for specific work but aren't day-one essentials.

### 5. Playwright MCP — browser automation

```bash
claude mcp add playwright --scope project \
  -- npx -y @microsoft/mcp-server-playwright
```

**Why for GeoRAG:** Phase 0's acceptance test #5 verifies the Workflow Run Dashboard renders the test workflow run. Phase 4 ships the customer chat surface. Phase 5+ ships visualizations. Playwright lets Claude Code actually verify "the dashboard renders" instead of taking it on faith.

**When to install:** Phase 0 step 8 (acceptance tests) — defer until then.

### 6. Docker MCP (via Docker Desktop MCP Toolkit)

Docker MCP Toolkit gives access to 200+ MCP servers from the Docker MCP Catalog, isolated in containers, with secure credential storage and OAuth handling. One install in Docker Desktop, many MCPs available.

**Setup:**
1. Update Docker Desktop to 4.48 or newer (per Docker's docs).
2. Open MCP Toolkit in Docker Desktop.
3. Create a profile called `georag` (groups MCPs together).
4. Browse the catalog and add: `postgres`, `github`, `filesystem`, `puppeteer` (browser), `time`, `fetch`.
5. Connect Claude Code to the profile via the Docker MCP Gateway.

**Why for GeoRAG:** if you prefer GUI-managed MCPs over CLI-managed, Docker Desktop's Toolkit handles credentials, isolation, and updates automatically. It supports profiles for organizing servers by project, so you can have one profile for GeoRAG and another for unrelated work.

**Trade-off vs `claude mcp add`:** Docker Toolkit adds a small layer of indirection (the Gateway routes calls), which is cleaner for security but slightly slower. Use whichever ergonomics you prefer; GeoRAG works with either.

### 7. Neo4j MCP — graph database access

```bash
# Install when Phase 1 stands up Neo4j
claude mcp add neo4j --scope project \
  -- npx -y @neo4j/mcp-server "$NEO4J_URI" "$NEO4J_USER" "$NEO4J_PASSWORD"
```

**Why for GeoRAG:** Phase 1+ stands up Neo4j for graph reasoning, entity resolution, and analogue traversal. Direct Cypher access from Claude Code accelerates schema exploration and query verification — same reasoning as the PostgreSQL MCP.

**When to install:** Phase 1, when Neo4j actually ships. Don't install in Phase 0.

---

## Custom skills to author for the GeoRAG project

Skills go in `.claude/skills/<skill-name>/SKILL.md` and are committed to the repo so the team shares them. Each skill is a small markdown file that teaches Claude Code a procedure or pattern specific to GeoRAG.

These are the skills worth authoring early. Each one solves a procedure that comes up dozens of times across phases.

### Skill: `georag-context`

**Purpose:** Loads a compact summary of GeoRAG's architectural posture so Claude Code doesn't have to re-read the full master plan + registry every session.

**Contents:** A 100-line SKILL.md that summarizes:
- The five-orchestrator architecture and what each owns
- The §2 non-negotiable rules (especially §2.9 public/private posture, §2.10 tooling discipline, §2.11 regulatory language)
- The risk-tier system R0–R5 and what each requires
- Pointers to master plan v2.4.2 and registry v1.3 for full reference

**When it triggers:** Claude Code auto-loads when working in the GeoRAG repo or when the conversation references "GeoRAG" or "agent" terminology.

### Skill: `postgres-migration`

**Purpose:** Laravel migration patterns + raw SQL conventions for PG18+ extension features (partitioning via `pg_partman`, RLS policies, `pg_trgm` indexes).

**Contents:** Templates for:
- Standard Laravel migration with `up()` + `down()`
- Raw SQL companion file for PG-specific features
- RLS policy template (the canonical form for workspace-scoped tables)
- pg_partman parent + child partition setup
- Verification SQL block at end of each migration

**When it triggers:** Adding a new migration in `database/migrations/` or `database/raw/`.

### Skill: `agent-wrapper`

**Purpose:** The `@georag_agent` decorator (Python) and `AgentInvoker` class (PHP) pattern from §35.1 Operational Contract. Every new agent in Phase 4+ uses this.

**Contents:** Annotated examples for both languages:
- Python decorator with required metadata (name, risk_tier, phase, prompt_id)
- PHP equivalent using attributes
- Failure-recovery hook patterns per risk tier
- Idempotency-key recipe per tier (R2 vs R3 vs R4 vs R5)

**When it triggers:** Implementing or modifying an agent.

### Skill: `hatchet-workflow`

**Purpose:** How to register and structure Hatchet workflows per the conventions in master plan §6 + Phase 0 kickoff Step 4.

**Contents:**
- Workflow declaration template
- Step function patterns (idempotency, retry policy, timeout)
- Audit-ledger emit at workflow start + end
- Outbox write pattern when workflow updates secondary stores
- Verification commands (`hatchet workflow list`, etc.)

**When it triggers:** Creating or modifying a Hatchet workflow.

### Skill: `audit-emit`

**Purpose:** Standard pattern for `emit_audit()` calls — what action_type values to use, payload conventions, hash-chain considerations.

**Contents:**
- Action type taxonomy: `agent.invoke`, `workspace.create`, `report.signoff`, etc.
- Canonical payload structures per action class
- When to include `target_id` vs leave null
- Common mistakes (forgetting workspace_id, including PII in payload)

**When it triggers:** Any code that writes to `audit_ledger`.

### Skill: `adr-template`

**Purpose:** Architecture Decision Record template that matches the existing GeoRAG ADR style (ADR-0001 through ADR-0005).

**Contents:**
- Standard ADR structure (Status, Context, Decision, Consequences, Alternatives Considered)
- Examples from existing ADRs
- Naming convention (`docs/adrs/ADR-NNNN-<short-name>.md`)
- When to write an ADR vs put a decision inline (master plan boundaries)

**When it triggers:** Conversation mentions "ADR," "architecture decision," or proposes a structural change.

### Skill: `phase-verify`

**Purpose:** The bash verification commands from each phase's "Definition of done" section in the kickoff doc. Easy access to the actual commands without re-reading the kickoff each time.

**Contents:**
- One section per phase step
- Copy-paste-ready bash commands
- Expected outputs / pass criteria
- Notes on what to do if a check fails

**When it triggers:** Running acceptance tests, debugging a failed step.

### Skill: `commit-and-pr`

**Purpose:** Conventional commits + PR description template that matches the project's discipline (linking to phase steps, registry agents, ADRs).

**Contents:**
- Conventional commits prefix taxonomy (`feat:`, `fix:`, `chore:`, etc.)
- Mandatory cross-references in PR descriptions (registry agent name if relevant, master plan section, kickoff step)
- "Definition of done" checklist per PR

**When it triggers:** Creating commits or PRs.

---

## What to deliberately *not* install

A few popular MCPs and patterns that don't fit GeoRAG's architecture:

| Skip | Why |
|---|---|
| **Anything that adds an autonomous multi-step agent loop** | Violates §2.10 tooling discipline. The Answer Graph state machine is the deliberate alternative — bounded, auditable, refusable. |
| **Closed-API LLM MCPs** (OpenAI, Anthropic API direct, etc.) | Master plan §12 locks vLLM-served `Qwen/Qwen3-14B-AWQ` as the only inference path. A second LLM access route undermines that lock and creates audit/compliance gaps. |
| **Web-scraping MCPs without rate limiting** | Public-geo TTLs and License/Terms Review Agent enforce careful source ingestion. A rate-unlimited scraper bypasses that. |
| **Generic "AI memory" MCPs** (Mem0, etc.) | Claude Code already has a memory system. A second memory layer creates inconsistency. |
| **Slack MCPs** unless team uses Slack | The architecture supports Slack notifications via Activepieces in Phase 2; a separate Slack MCP for Claude Code is redundant. Use what the architecture already specifies. |
| **Random "tool" plugins from third-party marketplaces** | Each adds attack surface and credential exposure. Stick to first-party MCPs (Anthropic, Microsoft, GitHub, Postgres-org, Docker-curated). |

Tool sprawl is its own problem. A typical five-server setup with 58 tools uses approximately 55,000 tokens before any conversation starts, even with Tool Search reducing the practical impact. Add deliberately, remove what isn't earning its keep.

---

## Setup walkthrough

Recommended install order for getting GeoRAG-ready Claude Code:

### Day 1 — pre-Phase 0 setup

```bash
cd /path/to/georag-repo

# 1. Verify Claude Code can read the existing connectors
# (Sentry, Postman, etc.) — no install needed; they're already authenticated.

# 2. Install the four core MCPs
claude mcp add github --scope project \
  -- npx -y @modelcontextprotocol/server-github
claude mcp add postgres --scope project \
  --env POSTGRES_READONLY=true \
  -- npx -y @modelcontextprotocol/server-postgres "$DATABASE_URL"
claude mcp add filesystem --scope project \
  -- npx -y @modelcontextprotocol/server-filesystem "$(pwd)"
claude mcp add context7 --scope user \
  -- npx -y @upstash/context7-mcp

# 3. Verify all MCPs registered
claude mcp list
# Expected: github, postgres, filesystem, context7

# 4. Author the foundational skills
mkdir -p .claude/skills/georag-context
mkdir -p .claude/skills/postgres-migration
mkdir -p .claude/skills/adr-template
# (Author SKILL.md in each — see "Custom skills" section above)

# 5. Commit to git so the team shares the config
git add .mcp.json .claude/skills/
git commit -m "chore: configure Claude Code for GeoRAG (MCPs + foundational skills)"
```

### Day 1 verification

```
# In a Claude Code session, verify access:
> List my GitHub repositories
# Should return list via GitHub MCP

> Run a SELECT 1 query against the dev Postgres
# Should execute via postgres MCP

> Show me the current Hatchet documentation
# Should pull live docs via context7

> Read the GeoRAG master plan §35.1 from /docs
# Should access via filesystem MCP
```

If any of the four don't respond, fix the auth/install before starting Phase 0.

### Phase 0 step 8 — add Playwright

```bash
# When you reach Phase 0 acceptance tests:
claude mcp add playwright --scope project \
  -- npx -y @microsoft/mcp-server-playwright
```

### Phase 1 — add Neo4j

```bash
# When Neo4j stands up in Phase 1:
claude mcp add neo4j --scope project \
  -- npx -y @neo4j/mcp-server "$NEO4J_URI" "$NEO4J_USER" "$NEO4J_PASSWORD"
```

Add other phase-specific MCPs as the work surfaces a need. Don't preemptively install.

---

## Maintenance discipline

A few habits that keep the Claude Code setup healthy over the 12-phase build:

### Quarterly audit

Once a quarter, run:

```bash
claude mcp list
ls .claude/skills/
```

For each entry, ask: "did I use this in the last 90 days?" If no, remove it. Tool sprawl creeps in; pruning prevents it.

### Skill ownership

Every custom skill in `.claude/skills/` should have an owner (a person on the team who maintains it). When a skill drifts from current practice, the owner updates it or removes it. Unmaintained skills are worse than no skills — they teach Claude Code outdated patterns.

### MCP server version pinning

The `npx -y @modelcontextprotocol/server-postgres` pattern uses the latest version every time. For prod-stability, pin to specific versions in `.mcp.json` once you confirm a version works:

```json
{
  "mcpServers": {
    "postgres": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-postgres@0.6.2", "$DATABASE_URL"]
    }
  }
}
```

Bump versions deliberately, not silently.

### When to add a new MCP

Three signals that an additional MCP would help:

1. You find yourself pasting the same command into Claude Code repeatedly — that suggests a tool that should be one Claude Code call away.
2. Claude Code makes a class of error that direct system access would prevent (e.g., schema mismatch errors that PostgreSQL MCP would have caught).
3. A new phase introduces a system Claude Code has no way to verify (e.g., Phase 1 standing up Qdrant — at that point, a Qdrant MCP is worth investigating).

Don't add MCPs speculatively. Add them when a real friction surfaces.

### When to remove a skill

If a skill has been triggered fewer than 5 times in a month and the task it covers is one Claude Code handles correctly without the skill, remove it. Skills that don't earn their place dilute the ones that do.

---

## Reference card

Quick-reference summary for grabbing later:

**Pre-authenticated connectors** (already enabled): Sentry, Google Drive, Gmail, Postman, Hugging Face, Cloudflare, Microsoft Learn, Microsoft 365, Google Calendar.

**Day-1 MCP installs** (must-have): GitHub, PostgreSQL (read-only), Filesystem (repo-scoped), Context7.

**Phase-by-phase MCP additions:**
- Phase 0 step 8 → Playwright (acceptance tests)
- Phase 1 → Neo4j (when stood up)
- Phase 6 → potentially Qdrant if a maintained MCP exists by then

**Day-1 skills to author:** `georag-context`, `postgres-migration`, `agent-wrapper`, `hatchet-workflow`, `audit-emit`, `adr-template`, `phase-verify`, `commit-and-pr`.

**Hard-no list:** autonomous multi-step agent loops, closed-API LLM MCPs, unrate-limited scrapers, redundant memory MCPs, third-party marketplace plugins.

**Discipline:** install deliberately, prune quarterly, version-pin in `.mcp.json`, every skill has an owner.

---

End of setup guide. Tooling is a means; the architecture is the end. Don't let MCP sprawl substitute for getting Phase 0 right.
