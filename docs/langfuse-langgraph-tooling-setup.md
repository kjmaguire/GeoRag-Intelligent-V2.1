# Langfuse + LangGraph tooling — installation status & next steps

**Date:** 2026-05-08 (revised — n8n removed)
**Scope:** Self-hosted Langfuse + LangGraph deps for the v1.15 agentic-orchestration upgrade. n8n was added during this round and removed before reaching production (see `docker/_deprecated/README.md` for rationale).

## Why Langfuse

LangSmith (the LangChain SaaS) was the canonical pick in the original tooling research, but:
- Apache 2.0 vs proprietary
- Self-hostable on the existing GeoRAG stack (reuses Postgres + Redis + SeaweedFS, only adds ClickHouse)
- No SaaS subscription needed for dev
- Aligns with the air-gapped junior-mining on-prem deployment target

## Why no n8n

The original tooling research suggested n8n as a SME-authored automations layer alongside Horizon + Dagster. Decision reversed before going live: it overlapped too much with the existing orchestration tiers, and the §07b "don't duplicate orchestration" hard rule made the responsibility split impractical to articulate. **LangGraph (inside FastAPI) covers agentic flows; Horizon covers user-triggered async work; Dagster covers bulk ingestion. That's enough.** If a future requirement makes a workflow-engine necessary, the restore procedure is documented in `docker/_deprecated/README.md`.

## What's installed (machine-state)

| Item | Where | Verify |
|---|---|---|
| **Langfuse self-host stack** (web + worker + ClickHouse) | running on `localhost:3001` | `curl http://localhost:3001/api/public/health` |
| **Langfuse MCP** (`avivsinai/langfuse-mcp` v0.9.1) | `claude_desktop_config.json` → `mcpServers.langfuse` (Docker image `georag/langfuse-mcp:latest`) | `docker images georag/langfuse-mcp` |
| **Docker MCP Gateway** | Already present in `claude_desktop_config.json` → `mcpServers.MCP_DOCKER`, profile `georag` | `docker mcp profile ls` |
| **`langchain-mcp-adapters` + `langgraph` + `langgraph-checkpoint-postgres` + `langfuse`** | `src/fastapi/pyproject.toml` under `[project.optional-dependencies] langgraph` | `grep -A6 'langgraph = ' src/fastapi/pyproject.toml` |
| **Langfuse compose overlay** | `docker/compose.langfuse.yml` (active) | `ls docker/compose.langfuse.yml` |

## Containers added by this round (running)

```
georag-clickhouse        Up    (healthy)   ~340 MB / 4 GB     OLAP trace store
georag-langfuse-web      Up    (healthy)   ~570 MB / 1 GB     UI + ingestion API
georag-langfuse-worker   Up    (healthy)   ~250 MB / 768 MB   Background trace processor
```

Plus reused infrastructure:
- Postgres database `langfuse` + role `langfuse_user` (in `georag-postgresql`)
- Redis DB 4 (in `georag-redis`)
- S3 buckets `langfuse-events` + `langfuse-media` (auto-created on first write in `georag-minio`)

## What you still need to do manually (3 things)

### 1. Sign up in the Langfuse UI

Open http://localhost:3001 and click **Sign up**. The `LANGFUSE_INIT_*` env shortcut needs an org-id which we didn't pre-generate — UI sign-up is the simplest path.

Create:
- Org: `GeoRAG`
- Project: `georag-dev`

### 2. Create API keys + paste them in two places

In Langfuse → **Settings → API Keys → Create new API key**, then copy both:

- `pk-lf-...` (public — visible always)
- `sk-lf-...` (secret — **shown once only, copy now**)

**Place A:** Edit `~/AppData/Roaming/Claude/claude_desktop_config.json` and replace the two `REPLACE_WITH_...` placeholders in the `langfuse` block:

```json
"env": {
  "LANGFUSE_PUBLIC_KEY": "pk-lf-...",
  "LANGFUSE_SECRET_KEY": "sk-lf-...",
  "LANGFUSE_HOST": "http://host.docker.internal:3001"
}
```

**Place B:** Paste them into WSL `.env` for FastAPI's future LangGraph tracing:

```bash
wsl -d Ubuntu -- bash -c 'sed -i "s|^LANGFUSE_PUBLIC_KEY=.*|LANGFUSE_PUBLIC_KEY=pk-lf-...|; s|^LANGFUSE_SECRET_KEY=.*|LANGFUSE_SECRET_KEY=sk-lf-...|" /home/georag/projects/georag/.env'
```

The two contexts use different `LANGFUSE_HOST` values — `host.docker.internal:3001` for Claude Code (running on Windows), `langfuse-web:3000` for FastAPI (in the Docker network). Both refer to the same service.

### 3. Restart Claude Code

After credentials land in `claude_desktop_config.json`, restart Claude Code so the `langfuse` MCP picks up the real keys. Run this to verify:

> "show me recent Langfuse traces"

It'll return an empty list at first (no traces yet) — that means the MCP is working. Once LangGraph cycles start emitting traces, they'll appear.

### Optional: Bump Docker Desktop's `.wslconfig`

Docker Desktop currently sees only 6 CPUs / 35 GB out of the Threadripper's 32C/64GB. The Langfuse stack added ~5 GB; the box is fine but won't realize the full hardware potential.

Edit `%UserProfile%\.wslconfig`:

```ini
[wsl2]
processors=24
memory=56GB
swap=8GB
```

Then `wsl --shutdown` and restart Docker Desktop.

## Smoke-tests

```bash
# Service health
curl -s http://localhost:3001/api/public/health   # {"status":"OK","version":"3.173.0"}
docker exec georag-clickhouse wget -qO- http://127.0.0.1:8123/ping   # Ok.

# Langfuse MCP (after API keys + Claude Code restart)
# In Claude Code: "show me recent Langfuse traces"

# LangGraph deps install + import (when you start the v1.15 refactor)
docker exec georag-fastapi pip install -e "/app[langgraph]"
docker exec georag-fastapi python -c "from langgraph.graph import StateGraph; from langfuse import Langfuse; print('OK')"
```

## What's archived

```
docker/_deprecated/
├── README.md                           ← swap rationale + reverse procedures
├── langsmith-mcp.Dockerfile            ← LangSmith MCP image (replaced by Langfuse)
├── mcp-langsmith-server.yaml           ← Docker MCP catalog descriptor (couldn't load)
├── mcp-custom-catalog.yaml             ← Docker MCP custom catalog wrapper (couldn't load)
└── compose.n8n.yml                     ← n8n service overlay (removed before going live)
```

Images removed: `georag/langsmith-mcp:latest` (354 MB), `mcp/n8n` (~110 MB). Image present: `georag/langfuse-mcp:latest` (275 MB).

## What I deliberately did NOT do

- **Did not run `uv sync --extra langgraph`.** The deps are declared but not installed. Run on the FastAPI container only when you start the actual LangGraph refactor — keeps the existing 622 tests stable until then.
- **Did not enable Langfuse tracing in the existing FastAPI.** `LANGFUSE_TRACING=false` default. Flip on per environment when ready (most sensible: only after the first LangGraph cycle ships, so you have something to trace).
- **Did not write `.claude/agents/langgraph-engineer.md`.** Per the original research: no canonical community version exists, and the right `.md` content depends on patterns you'll only know after building one or two LangGraph cycles. Use `backend-fastapi` until then.

## Files added or modified (cumulative across both rounds)

```
NEW    docker/langfuse-mcp.Dockerfile                     (Langfuse MCP wrapper image)
NEW    docker/compose.langfuse.yml                        (web + worker + ClickHouse + init)
NEW    docker/_deprecated/                                (LangSmith + n8n remnants archived)
EDIT   .env.example                                       (LANGFUSE_* added; LANGSMITH_* + N8N_* removed)
EDIT   src/fastapi/pyproject.toml                         (langfuse + langgraph deps in [langgraph] extra)
EDIT   ~/AppData/Roaming/Claude/claude_desktop_config.json (langfuse MCP wired, langsmith block removed)
```

Image churn:
```
+ georag/langfuse-mcp:latest      (275 MB)
- georag/langsmith-mcp:latest     (354 MB)
- mcp/n8n:latest                  (~110 MB)
```

## What's next (architecture work, not tooling)

This setup is the prerequisite for the v1.15 LangGraph refactor. The actual
work — writing the LangGraph state machine that replaces the current
`orchestrator.py` switch — is its own session. Suggested order:

1. **ADR-0002 orchestration boundaries** — formalise the THREE-system split
   (Horizon / Dagster / LangGraph) per georag-architecture.html §07b. n8n is
   no longer in scope.
2. **First LangGraph cycle** — port the simplest query class (`count` /
   `exists`) end-to-end. Validates the §07c streaming events + §04i guards
   in the new graph shape.
3. **Langfuse dashboard** — first golden-query run with tracing on so you
   have a baseline trace shape.
4. **Subagent** — by now the LangGraph patterns are concrete enough that
   `.claude/agents/langgraph-engineer.md` writes itself.
