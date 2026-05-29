# Appendix C — Security Posture and Threat Model

Status: **Draft — implementation tracked here is partial.**

This appendix is the security charter for GeoRAG. Every item below has a
**state** (Live / Partial / Planned / Open issue) and a **next action**.
Use it as the single page to brief a new operator or auditor.

## 1. Trust boundaries

```
Browser
  │  Sanctum cookie / PAT
  ▼
Caddy (TLS edge — :8443 internal CA or ACME)
  │  forward_auth(laravel-octane)
  ▼
laravel-octane  ←── shared X-Service-Key + per-flow JWT
  │
  ├── pgbouncer (transaction pool) ── postgresql (georag_app role; RLS applies)
  ├── redis (auth password)
  ├── fastapi (X-Service-Key shared HMAC + per-request trace_id)
  └── laravel-reverb (websocket auth via Sanctum channel.php)

fastapi
  ├── postgresql (direct, georag_app or georag for the migration channel only)
  ├── neo4j (bolt auth)
  ├── qdrant (auth: off in dev — REQUIRED in prod)
  ├── seaweedfs (S3 credentials)
  ├── vllm (open in-network; no auth)
  └── anthropic (TLS + bearer; egress trust boundary)

hatchet-worker-{ingestion,ai}
  └── hatchet-lite (HATCHET_CLIENT_TOKEN over gRPC; insecure in dev)

kestra ←── HMAC-signed inbound webhooks (EXTERNAL_NOTIFICATION_HMAC_SECRET)
  └── fastapi (per-flow JWT; KESTRA_FLOW_JWT_SECRET + encrypted per-flow key)
```

## 2. Tenant isolation

| Risk | State | Mitigation | Next action |
|---|---|---|---|
| RLS bypass via SUPERUSER `georag` | **Open** | Runtime traffic uses `georag_app` (`NOSUPERUSER NOBYPASSRLS`); startup self-check refuses if it flips ([phase1/10-georag-app-role.sql:104-107](../../../database/raw/phase1/10-georag-app-role.sql)) | Split `georag` → `georag_owner` + `georag_migrator` (both `NOSUPERUSER`); track at [Ch 02 §1.1](../manual/02-data-stores.md#11-known-security-issue--georag-role-is-superuser) |
| Pooled GUC leak (PgBouncer transaction mode) | Live | `SET LOCAL app.workspace_id` inside explicit txns only; documented in [Ch 11 §14](../manual/11-tenancy-and-rls.md) | Add lint that flags any `SET app.workspace_id` without `LOCAL` |
| Forgetting `FORCE ROW LEVEL SECURITY` | Live | `WorkspaceRlsCoverageTest` ([tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php](../../../tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php)) | Keep `EXEMPT_TEST_DB_ONLY_TABLES` empty |
| Cross-workspace FK leak | Partial | `silver.cross_workspace_audit` table + nightly check | Add `CHECK (silver row's workspace_id = referenced silver row's workspace_id)` as triggers where feasible |
| Martin tile fence | **Open** | Laravel proxy sets `app.workspace_id` per request before calling Martin | Switch Martin from `georag_app` to `martin_readonly` (Ch 02 §1.2) |

## 3. Prompt injection (from uploaded documents)

| Risk | State | Mitigation | Next action |
|---|---|---|---|
| Adversarial markdown in PDF reaches LLM context | Partial | Layer 4 entity resolution + Layer 5 chunk provenance reject ungrounded claims; OIUR validator (Layer 2) enforces evidence anchors | Add an inline sanitiser that strips role-bait prefixes (`Ignore previous instructions…`) at chunk-creation time |
| Tool-result injection (a tool emits text the LLM treats as instruction) | Partial | Pydantic AI typed-output keeps tool results in structured fields | Wrap every tool-result text in `<tool_result>…</tool_result>` delimiters at the LLM call site |
| Citation marker forgery | Live | Layer 5 provenance: every `[ev:xxxxxxxx]` must resolve to a real `silver.evidence_items.evidence_id` (8-char hex prefix); unresolved markers fail validation | none |

## 4. Tool abuse

| Risk | State | Mitigation | Next action |
|---|---|---|---|
| LLM calls a tool with attacker-controlled args | Partial | Per-tool Pydantic input schemas; argument validation precedes call; per-workspace `workspace.agent_timeouts` cap runtime | Document each tool's input contract in a per-tool spec sheet (planned in [appendix Z](Z-roadmap.md)) |
| Tool dispatcher drops `RunContext` and ignores tenant fence | Live | Introspection regression test pins each tool's signature ([notes/INDEX.md#project_agentic_dispatcher_ctx_fix_2026_05_25](../notes/INDEX.md#project_agentic_dispatcher_ctx_fix_2026_05_25)) | none |
| Cost-bomb via runaway tool loop | Live | `cost_burn_watcher` Hatchet workflow (15-min cron) reads `usage.workspace_cost_quotas`; agent loop max depth in `RetrievalProfile` | Per-tool circuit breaker at the dispatcher level |

## 5. External LLM data egress

| Risk | State | Mitigation | Next action |
|---|---|---|---|
| Workspace data sent to Anthropic when `LLM_BACKEND=anthropic` | **Open** | Env flag controls the backend, but no per-workspace gating | Profile-gate `LLM_BACKEND=anthropic` so on-prem profiles refuse the flag; per-workspace policy `workspace.workspace_settings.allow_external_llm: bool` checked at call site |
| Anthropic prompt caching includes evidence text | Live | `ANTHROPIC_ENABLE_PROMPT_CACHING=true` is a perf knob; the cached prompt is the same evidence the user supplied | Surface this in the Settings page so operators see what gets cached |
| Cross-workspace prompt cache reuse | Partial | Prompt cache key includes workspace_id via prompt prefix | Add explicit cache-key salt = `workspace_id` |

## 6. Qdrant access control

| Risk | State | Next action |
|---|---|---|
| Qdrant `:6333` exposed via Docker | Live in dev (no auth) | Profile-gated override that sets `QDRANT__SERVICE__API_KEY=${QDRANT_API_KEY:?required}` for the `prod` profile (Ch 02 §3) |
| Empty-string auth trap | Live | Documented inline in compose; no empty default | none |

## 7. Object storage access

| Risk | State | Mitigation | Next action |
|---|---|---|---|
| Anonymous reads on SeaweedFS S3 | Live | Bucket policies deny anon access | Verify on prod profile |
| Cross-workspace key listing | Partial | Key prefix encodes workspace_id; aioboto3 callers always prefix-scope `ListObjectsV2` | Add an aws_iam-style bucket policy that mandates `${aws:userid}` membership (workspace-bound S3 user planned) |
| Signed URLs for browser downloads | **Planned** | Today downloads stream via Laravel; signed URLs reduce latency + offload to SeaweedFS | Implement `aioboto3.generate_presigned_url(ExpiresIn=300)` on the `/api/exports/{id}/download` route |

## 8. Admin panel access

| Risk | State | Mitigation |
|---|---|---|
| `/admin/integrations/hatchet`, `/admin/integrations/kestra` | Live | Gate via `Gate::define('viewAdmin', …)` (Sanctum auth + role check) |
| `/pulse` | Live | `Gate::define('viewPulse', …)` |
| Horizon `/horizon` | Live | `Gate::define('viewHorizon', …)` |

## 9. Secret rotation

| Secret | Rotation tool | Cadence |
|---|---|---|
| `APP_KEY` | `php artisan key:generate` + RUNBOOK § "APP_KEY rotation" | Annual |
| `FASTAPI_SERVICE_KEY` | Manual; restart all dependent containers | Quarterly |
| `KESTRA_FLOW_JWT_SECRET` | `scripts/phase3_jwt_rotate.sh` + Kestra KV write | Quarterly |
| Per-flow JWT private keys | `flow_jwt_key_reaper` Hatchet workflow (weekly cron) | Weekly automatic |
| `EXTERNAL_NOTIFICATION_HMAC_SECRET` | Manual + sender re-issue | On compromise |
| `AUDIT_ENCRYPTION_KEY` | Manual + pgcrypto re-encrypt | On compromise only — rotation requires re-encrypting `workflow.flow_registry` |
| Postgres `POSTGRES_PASSWORD` | Manual + dependent container restarts | Annual |
| Neo4j `NEO4J_PASSWORD` | RUNBOOK § "Neo4j auth migration" | Annual |
| Redis `REDIS_PASSWORD` | Manual | Annual |
| SeaweedFS S3 keys | Manual + dependent container restarts | Annual |
| Anthropic API key | Manual via Settings | On compromise |

## 10. Backup encryption

| Backup | Encryption at rest | Encryption in transit |
|---|---|---|
| Postgres base + WAL | At-rest: SeaweedFS volume encryption (host-disk LUKS in prod) | In-transit: in-network only (TLS via prod profile) |
| Neo4j dumps | Same | Same |
| Qdrant snapshots | Same | Same |
| SeaweedFS cross-region | **Planned** | TLS + signed payload hashes |

## 11. Break-glass access

- Per-environment `break_glass` Postgres role: `LOGIN NOSUPERUSER` with
  explicit GRANT on `audit.*` for read-only chain inspection. Not yet
  created. **Planned.**
- Operator can connect to FastAPI as a special audit user; every query
  by this user is double-logged to both the regular `audit.audit_ledger`
  AND a separate `audit.break_glass_log`.
- All break-glass usage triggers a page via Alertmanager.

## 12. Audit requirements

- Every state-changing event writes to `audit.audit_ledger` via the
  hash-chain trigger ([phase0/90-audit-hash-chain-trigger.sql:71](../../../database/raw/phase0/90-audit-hash-chain-trigger.sql)).
- Daily verifier (`audit_ledger_verify` Hatchet workflow) replays chain;
  forks → `audit.audit_ledger_chain_fork_quarantine` + Alertmanager page.
- 24-month retention (pg_partman policy).

## 13. Data export controls

- Workspace export (`workspace_export` Hatchet workflow) produces a
  single tarball under `exports/<workspace_id>/<export_id>/`.
- Export emits an audit ledger row with the full file manifest.
- Tier-3 workspaces require operator approval before export — gated by
  `workspace.approval_requirements`.
- External recipient (email) for an export is captured in
  `silver.support_packets` and audit-logged.

## 14. Threat-model summary table

| Asset | Threat | Likelihood | Impact | Posture |
|---|---|---|---|---|
| Workspace data | Cross-tenant leak via missing FORCE RLS | Low (test backstop) | Severe | Live + tested |
| Workspace data | Cross-tenant leak via `georag` SUPERUSER misuse | Low (operational) | Severe | Open structural |
| Citation truth | Prompt injection produces fake citations | Medium | High | Partial — sanitiser planned |
| LLM cost | Cost bomb | Medium | Med | Live (`cost_burn_watcher`) |
| External LLM | Workspace text egresses to Anthropic without consent | Medium | High | Open (profile gate planned) |
| Backups | Restore-time tampering | Low | Severe | Partial — encryption + chain |
| Object storage | Adversarial workspace lists peer keys | Low | Med | Partial |
| Tile data | Martin queries past workspace fence | Low | High | Open (`martin_ro` planned) |
| Audit chain | Chain fork unnoticed | Low | Severe | Live (daily verifier + alert) |
| Secrets | Key rotation gap | Med | High | Partial — rotation runbooks exist, not all automated |
