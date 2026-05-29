# Module 10 Auth-Bypass Sweep
<!-- Produced by: devops-engineer agent (Claude Sonnet 4.6) -->
<!-- Date: 2026-04-19 -->
<!-- Sweep scope: src/ and app/ — all .py, .php, .ts, .tsx, .yml, .yaml files -->
<!-- Exclusions: .venv/, node_modules/, vendor/ (third-party package internals) -->

Patterns searched:
- `auth_enabled\s*=\s*False`
- `auth\s*=\s*None`
- `verify\s*=\s*False`
- `skip_auth`, `noauth`, `no_auth`, `allow_anonymous`
- `DEBUG\s*=\s*True`, `APP_DEBUG.*true`
- `trust_remote_code`
- `ignore_verify`, `insecure`, `allow_anonymous`

---

## Findings Table

| File | Line | Pattern | Classification | Notes |
|---|---|---|---|---|
| `src/fastapi/tmp_g.py` | 12 | `auth=None` (historical) | CLOSED | Was patched in Module 2 Phase B (N4J-01 fix) to use `settings.NEO4J_USER / settings.NEO4J_PASSWORD`. File subsequently moved to `scripts/verify_graph_entity_cache.py` in Module 2 Phase B cleanup. |
| `src/dagster/georag_dagster/definitions.py` | 548 | `auth_enabled=False` (historical) | CLOSED | Patched to `auth_enabled=True` + `EnvVar` credentials in Module 2 Phase B (N4J-01 fix). Confirmed live via Python introspection. |
| `.env.example` | 34 | `APP_DEBUG=true` | MEDIUM (dev-only) | Intentional — `.env.example` is a developer template. The code path (`config/app.php:42`) defaults to `false` and reads from `APP_DEBUG` env var. The docker-compose.yml services all use `APP_DEBUG: ${APP_DEBUG:-true}` meaning the default is `true` in compose as well. This is acceptable for dev profile but must be explicitly set to `false` in any staging/production `.env`. No code change needed; document in deployment runbook. |
| `docker-compose.yml` | 441, 518, 585 | `APP_DEBUG: ${APP_DEBUG:-true}` | MEDIUM (dev-only) | Default `true` in three Laravel service definitions (octane, horizon, reverb). Sourced from env var so overridable at deploy time. The `:-true` fallback means any deploy that does not set `APP_DEBUG=false` will expose stack traces. Add `APP_DEBUG=false` to any prod/staging `.env`. No code fix needed in this sweep; flag for Module 9 / deployment hardening. |
| `.env.example` | 100 | `# Optional password — leave empty for no-auth (dev only, NOT for production)` | LOW (docs/comment) | Redis password comment. This is guidance text, not executable config. REDIS_PASSWORD itself is blank by default in the example file — acceptable for single-developer local dev. Harmless as a comment. |
| `.env.example` | 130-133 | `NEO4J_AUTH=${NEO4J_USERNAME}/${NEO4J_PASSWORD}` | HIGH — **RESOLVED 2026-04-19** | Updated to show `NEO4J_USERNAME=neo4j` + blank `NEO4J_PASSWORD=` + constructed `NEO4J_AUTH`. Comment explicitly rejects `none` as unsupported. Paired with `docker-compose.yml:811` comment fix. |
| `.env.example` | 157 | `# Leave empty to disable auth (dev — network isolated)` | MEDIUM (docs/comment) | Qdrant API key comment. Qdrant has no API key by default in dev. Acceptable for Docker-internal network dev; not acceptable if Qdrant port 6333 is exposed externally. Verify Qdrant port is not published in docker-compose.yml for prod profiles. |
| `docker-compose.yml` | 811 | `# ... Authenticated (NEO4J_AUTH=${NEO4J_USERNAME}/${NEO4J_PASSWORD}) — Module 2 Phase B N4J-01.` | LOW — **RESOLVED 2026-04-19** | Stale "auth disabled" comment replaced with accurate authenticated-state comment. |

---

## Summary by Classification

| Classification | Count | Items |
|---|---|---|
| **HIGH** | **0** | _(1 resolved 2026-04-19)_ |
| **MEDIUM** | **2** | `.env.example:34` APP_DEBUG=true; `docker-compose.yml:441,518,585` APP_DEBUG default true |
| **LOW / docs-only** | **1** | Redis no-auth comment _(Neo4j stale compose comment resolved 2026-04-19)_ |
| **CLOSED** | **4** | `tmp_g.py` auth=None; `definitions.py` auth_enabled=False; `.env.example:132` NEO4J_AUTH=none; `docker-compose.yml:811` stale comment |

---

## HIGH Finding Detail

### .env.example line 132: NEO4J_AUTH=none

The `.env.example` still shows `NEO4J_AUTH=none`. Any developer who clones the repo and uses `.env.example` as their starting point will launch Neo4j without authentication. The docker-compose.yml now requires `NEO4J_PASSWORD` to be set (it uses `${NEO4J_PASSWORD:?NEO4J_PASSWORD must be set in .env}`), so the compose itself will refuse to start without the password — but the `.env.example` is still misleading and sets `NEO4J_AUTH=none` which contradicts the compose expectation.

**Required fix (future session):**
1. Update `.env.example` lines around 132 to show:
   ```
   NEO4J_USERNAME=neo4j
   NEO4J_PASSWORD=changeme-use-a-strong-password
   NEO4J_AUTH=${NEO4J_USERNAME}/${NEO4J_PASSWORD}
   ```
2. Remove or update the comment on line 811 of `docker-compose.yml` from `Auth disabled for local dev (NEO4J_AUTH=none)` to reflect that auth is now required.

**Owner:** devops-engineer / backend-fastapi  
**Module:** Module 9 (Security/RBAC) or nearest available session

---

*This file is the authoritative record for the Module 10 auth-bypass sweep. Append new findings here as they are discovered. Do not delete CLOSED rows — they document the history.*
