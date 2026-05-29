# Claude Code MCP Migration Runbook

**Date authored:** 2026-05-10
**Status:** Active
**Related:** `docs/georag-claude-code-setup.md` · `.mcp.json` · `~/.claude.json` · `~/AppData/Roaming/Claude/claude_desktop_config.json`

## The actual architecture on this machine

The setup doc treats Claude Code as one thing. On Windows it's actually
**three layers** that each read different config files. Knowing which
layer reads which file matters because adding an MCP entry to the wrong
file means the entry is silently ignored.

### Layer 1 — Claude Desktop GUI (the window you interact with)

- **Binary:** `C:\Program Files\WindowsApps\Claude_1.6608.2.0_x64...\app\Claude.exe`
- **MCP config:** `%APPDATA%\Claude\claude_desktop_config.json`
- **Currently has:** `MCP_DOCKER` (Docker MCP gateway), `langfuse` (Langfuse tracing — direct `docker run`)
- **Plus DXT extensions** (Claude's packaged extension format) installed via the GUI's extension UI:
  - `Filesystem` (Anthropic-bundled `secure-filesystem-server v0.2.0`)
  - `Shadcn UI`
  - `Sentry` (also surfaces as `mcp__sentry__*`)
  - `Cloudflare Developer Platform`
  - `Postman`, `Microsoft 365`, `Microsoft Learn`, `Hugging Face`, `Google Drive`
  - Catalog of these is in `extensions-installations.json`

### Layer 2 — Claude Code CLI agent (what actually runs your conversation)

- **Binary:** `C:\Users\GeoRAG\AppData\Roaming\Claude\claude-code\2.1.128\claude.exe`
- **MCP config:** `~/.claude.json` (the `mcpServers` block at line 447ish)
- **Currently has:** `MCP_DOCKER`, `laravel-boost`, `postgres`, `qdrant`, `sentry`, `context7`
- **Spawning model:** Desktop GUI launches this process for every agent turn, passing the conversation context

### Layer 3 — Project-scope `.mcp.json`

- **File:** `<repo-root>/.mcp.json`
- **Read by:** Claude Code CLI agent **when the session is rooted in the project directory**
- **NOT read by** the Desktop GUI directly
- **Currently has:** `laravel-boost` (project-only — needs `php artisan` to run from the repo)

## Why most "fix it via npx" recipes fail on Windows

The setup doc's `claude mcp add ... -- npx -y @some/mcp-server` pattern
assumes Node.js is on the Windows PATH. **On this machine it isn't.**
Native Node.js / npx is not installed; only WSL has them
(`/usr/bin/node` v22, `/usr/bin/npx` v10).

Effect: any MCP entry whose `command` is `npx` fails to spawn — Claude
silently skips it. Look at the working entries in `~/.claude.json` and
`claude_desktop_config.json`: every one uses `docker`, `wsl.exe`, or
`php`. None use bare `npx`.

### Three working patterns

```jsonc
// Pattern A: docker container (preferred when an official image exists)
"langfuse": {
  "command": "docker",
  "args": ["run", "--rm", "-i", "...", "georag/langfuse-mcp:latest", "--read-only"],
  "env": { ... }
}

// Pattern B: WSL wrapper script (sources .env, can use npx inside WSL)
"postgres": {
  "command": "wsl.exe",
  "args": ["-d", "Ubuntu", "/home/georag/projects/georag/scripts/mcp/start-postgres.sh"]
}

// Pattern C: WSL invocation of npx directly (no wrapper script needed)
"context7": {
  "command": "wsl.exe",
  "args": ["-d", "Ubuntu", "/usr/bin/npx", "-y", "@upstash/context7-mcp"]
}

// Pattern D: project-local PHP artisan (no Node needed)
"laravel-boost": {
  "command": "php",
  "args": ["artisan", "boost:mcp"]
}
```

## What's NOT useful to add

### GitHub MCP

The Docker MCP Toolkit's `MCP_DOCKER` gateway already exposes GitHub
tools (`mcp__MCP_DOCKER__create_pull_request`, `add_issue_comment`,
`get_file_contents`, `merge_pull_request`, `request_copilot_review`,
+30 others). Adding a separate `@modelcontextprotocol/server-github`
entry in `.mcp.json` would:

1. Fail to spawn (no native npx — see above)
2. Duplicate tools that already work through `MCP_DOCKER`

If you want a dedicated GitHub MCP outside the Docker gateway, install
Node.js natively first (`winget install OpenJS.NodeJS`) and then add via
the Pattern A (Docker) or Pattern C (WSL npx) shape — not bare `npx`.

### Filesystem MCP

Anthropic's bundled `secure-filesystem-server v0.2.0` ships as a Claude
Desktop DXT extension and is already installed (visible in the MCP log
on every startup as `[Filesystem] Server started and connected
successfully`). Adding `@modelcontextprotocol/server-filesystem` via npx
would duplicate what's already there.

### Native Node.js install

If you decide you want native `npx`-based MCPs to work without WSL
indirection: `winget install OpenJS.NodeJS` installs Node + npm + npx
on PATH. This is a one-line install but it spreads Node further across
the system; the WSL-wrapper pattern above keeps the Node footprint
inside WSL only.

## Current state (2026-05-10)

| MCP | Layer | Config file | Pattern | Status |
|---|---|---|---|---|
| `MCP_DOCKER` | Both | `claude_desktop_config.json` + `~/.claude.json` | Docker gateway | ✅ working |
| `langfuse` | Desktop | `claude_desktop_config.json` | Docker run | ✅ working |
| `Filesystem` | Desktop DXT | `extensions-installations.json` | Bundled | ✅ working |
| `Shadcn UI` | Desktop DXT | `extensions-installations.json` | Bundled | ✅ working |
| `laravel-boost` | Project + CLI | `.mcp.json` + `~/.claude.json` | PHP artisan | ✅ working when CWD is repo |
| `postgres` | CLI | `~/.claude.json` | WSL wrapper script | ✅ working |
| `qdrant` | CLI | `~/.claude.json` | WSL wrapper script | ✅ working |
| `sentry` | CLI | `~/.claude.json` | WSL wrapper script | ✅ working |
| `context7` | CLI | `~/.claude.json` | WSL `npx` (Pattern C) | 🔄 added 2026-05-10; needs Claude Code session restart to pick up |

## Adding new MCPs going forward

Decision tree:

```
Is there an existing Docker MCP catalog entry?
├── Yes  → Enable it in Docker Desktop's MCP Toolkit UI; tools surface via MCP_DOCKER gateway. Done.
└── No   → Does it have an official Docker image?
          ├── Yes  → Pattern A (docker run block in claude_desktop_config.json or ~/.claude.json)
          └── No   → Use WSL: write a wrapper script in scripts/mcp/start-<name>.sh and add Pattern B/C entry
```

When you add a new entry, restart Claude Code (close fully + reopen)
for it to pick up the config change.

## Verification after a config change

```powershell
# 1. Tail the MCP startup log — successful starts log here:
Get-Content "$env:APPDATA\Claude\logs\mcp.log" -Tail 30 |
    Select-String -Pattern 'Server started|connected successfully|exited|stderr|fail'

# 2. From a fresh Claude Code session in the repo, ToolSearch the new namespace:
#    e.g. "context7 library docs" should surface mcp__context7__* tools.
```

If the new MCP doesn't appear in the log AT ALL (not even an error),
the most likely cause is `command:` referencing something not on PATH —
verify with `where.exe <command>`.

## Removed from `.mcp.json` 2026-05-10

I had earlier added `github` and `filesystem` entries via npx. Both
removed because:

- `github` — fails to spawn (no native npx) AND already covered by
  `MCP_DOCKER` gateway
- `filesystem` — fails to spawn (no native npx) AND already covered by
  the Anthropic-bundled `secure-filesystem-server` DXT

`.mcp.json` now only contains `laravel-boost` (project-scope PHP — no
Node dependency).

## Future MCP additions (per setup doc roadmap)

| Phase | MCP | Where it should live | Pattern |
|---|---|---|---|
| Phase 0 step 8 | `playwright` | `~/.claude.json` (CLI level) | Pattern C (WSL npx) or check Docker MCP catalog first |
| Phase 1 | `neo4j` | `~/.claude.json` | Pattern B (WSL wrapper script — neo4j needs URI/credentials, follow the postgres/qdrant pattern) |
| Phase 6 | possibly `qdrant` upgrade | `~/.claude.json` | already there |
