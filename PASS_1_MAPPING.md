# PASS 1 — Manifest → owning doc mapping + deduped skeletons

> Per reconciliation plan §2 Phase 1. **No prose, no judgment of canonical content
> yet.** Mechanical mapping + skeleton outlines only. STOP at the end for approval.

---

## 1. Default rule

Every manifest line gets **exactly one owning doc** from:

- **INDEX** — `HANDOVER_INDEX.md`
- **SAD** — `SAD.md`
- **DFS** — `DFS.md`
- **API** — `API_DOCUMENTATION.md`
- **CICD** — `CICD_PIPELINE.md`

Items needing treatment in two docs are flagged with a **split rule** that
defines what each doc covers — the rule prevents accretion-on-rebuild.

## 2. Routing principles (applied consistently)

- **API** owns anything a *caller* needs to invoke the system: HTTP routes,
  WebSocket channels, tile URLs, auth headers, refusal vocabulary,
  request/response contracts.
- **DFS** owns anything about *where data lives and how it moves*: schemas,
  collections, indexes, retention windows, ingestion paths, RAG paths,
  backup paths, export formats.
- **SAD** owns *what makes up the system*: components, topology, runtime
  config envelope, cross-cutting concerns (security headers, tenancy,
  observability), agent/LangGraph composition, frontend structure.
- **CICD** owns *how the system gets built, tested, deployed, and operated*:
  GitHub Actions, Dockerfiles, test gates, deploy gates, secrets/SOPS,
  worker pool selection at deploy time, Hatchet worker pool config,
  alertmanager routing, operator scripts.
- **INDEX** owns *navigation*: pointers to the four docs, pointers to the
  canonical `docs/architecture/` tree, consolidated Needs-Confirmation
  rollup, operator contact handoff.

## 3. Mapping table

Manifest section numbers from `HANDOVER_MANIFEST.md`.

| Manifest § | Section title | Owner | Split rule (if dual) |
|---|---|---|---|
| 1 | FastAPI routes (109 endpoints) | **API** | — |
| 1a | Router-prefix map | **API** | — |
| 1b | `main.py` include_router overrides | **API** | — |
| 2 | Pydantic AI agents (`@georag_agent`, 42 catalogue) | **SAD** | SPLIT — SAD lists the 42 agents + per-phase grouping + risk-tier semantics; **CICD** mentions only the Hatchet worker-pool that hosts them (which agents run in `ingestion` vs `ai` pool, see §4a/§4b) |
| 3 | LangGraph subgraphs + 8 intents | **SAD** | SPLIT — SAD documents the 3 subgraph components + 8 intents as a system component; **DFS §2 (RAG flow)** references "agentic_retrieval graph" by name and links to SAD |
| 4 | Hatchet workflow modules (45) | **DFS** | SPLIT — DFS owns each workflow's *data flow* (e.g. ingest_pdf → silver.reports + Qdrant + Neo4j); **CICD §6** owns the worker-pool selection + `WORKER_POOL` env + Hatchet engine compose envelope |
| 4a | Worker pool `ingestion` | **CICD** | — |
| 4b | Worker pool `ai` | **CICD** | — |
| 4c | Hatchet cron schedules (30) | **CICD** | — (operational schedule belongs with CD/ops) |
| 5 | Dagster assets (53 + 4) | **DFS** | — (assets = bronze→silver→gold→index data movement) |
| 5a | Dagster schedules + sensors | **CICD** | — (scheduled orchestrator runs belong with CD/ops) |
| 5b | Dagster asset checks (27) | **CICD** | SPLIT — CICD lists the file × check-count as a quality gate; **DFS** mentions that each silver table has DQ checks pointing back to `silver_*_dq.py` assets in §5 |
| 6 | Kestra flows (3) | **DFS** | SPLIT — DFS owns the *data flow* (external_notification HMAC envelope → FastAPI → Hatchet → silver; public_geoscience_pull → bronze; support_packet_dispatch → email/slack). **API §8** owns the inbound webhook contract (HMAC envelope schema) for `external_notification` |
| 7a | PG tables per schema (174 / 15) | **DFS** | — |
| 7b | PG functions (23) | **DFS** | SPLIT — DFS lists by schema + purpose; **API §7 (Tile API)** mentions the 8 silver + 8 public_geo MVT functions by name because Martin exposes them as endpoints |
| 7c | PG triggers (7) | **DFS** | — |
| 7d | PG materialized views (1) | **DFS** | — |
| 7e | PG extensions (15) | **DFS** | — |
| 7f | Neo4j labels + rels | **DFS** | — |
| 7g | Qdrant collections + payload indices | **DFS** | — |
| 7h | Redis logical DBs | **DFS** | — |
| 7i | ClickHouse | **DFS** | — |
| 7j | SeaweedFS buckets | **DFS** | — |
| 8a | Reverb channels (30 patterns) | **API** | — |
| 8b | Reverb event classes (11) | **API** | — |
| 9a | Laravel api.php (67 routes) | **API** | — |
| 9b | Laravel web.php (157 routes) | **API** | SPLIT — API lists the public/Inertia surface + the `/admin/*` operator surface as routes; **CICD §6** mentions `/admin/integrations/kestra/{path?}` as the SSO bridge entry only (forwards through Caddy) |
| 10 | Martin MVT function inventory | **API** | — (Martin is the tile API surface) |
| 11 | CI/CD workflow files (7) | **CICD** | — |
| 12 | Dockerfiles (5) | **CICD** | — |
| 13a | Compose services (33) | **SAD** | SPLIT — SAD lists services as system components in topology diagram; **CICD §4** covers each Dockerfile-built service's build pipeline; **DFS §5/§7** references storage services where data lives |
| 13b | Compose overlays (5) | **SAD** | — |
| 13c | Compose named volumes (23) | **DFS** | — (volumes = persistence artifacts) |
| 13d | Compose networks (`georag` bridge) | **SAD** | — |
| 14 | Laravel config files (20) | **SAD** | — (config = cross-cutting config envelope) |
| 15 | Env surface (.env, FastAPI Settings, feature flags) | **SAD** | SPLIT — SAD documents config envelope shape + key flag taxonomies; **CICD §6 (Secrets)** documents `.env.production.enc` (SOPS) provisioning + `O-01..O-07` preflight gates |
| 16a | `scripts/operator/` | **CICD** | — |
| 16b | `ops/setup/` | **CICD** | — |
| 16c | `ops/runbooks/` (38) | **INDEX** | SPLIT — INDEX lists the 38 runbooks as a pointer; **CICD §6** references specific runbooks (cold-start, secret-management, deploy-rollback) in operational procedures |
| 16d | `ops/baselines/` | **CICD** | — |
| 16e | `ops/audit/` | **CICD** | — |
| 17a | Prometheus jobs (12) | **SAD** | — (observability is cross-cutting in SAD) |
| 17b | Prometheus alert defs (64 / 13 files) | **SAD** | SPLIT — SAD lists per-file alert families; **CICD §6** documents alertmanager receiver routing (critical-webhook / warn-webhook / dev-null) |
| 17c | Grafana dashboards (17) | **SAD** | — |
| 17d | Loki / Tempo / OTel / Promtail | **SAD** | — |
| 18 | ADRs (12) | **INDEX** | SPLIT — INDEX gives the 12-row ADR ledger as a navigation pointer; **SAD §5 (Key decisions)** restates *only the architecturally-significant titles*, one line each, never the decision detail |
| 19 | Eloquent models | **SAD** | — (domain models = component-tier) |
| 20 | Inertia pages | **SAD** | — (frontend is a component) |
| 21 | Laravel controllers | **API** | — (controllers are route handlers) |
| 22 | Cross-service boundaries (13 sub-sections) | **SAD** | — (topology / orchestration = cross-cutting in SAD) |
| 23a | `docs/architecture/` existing files | **INDEX** | — |
| 23b | `docs/` top-level | **INDEX** | — |
| 23c | `docs/adr/` | **INDEX** | — (cross-reference to §18) |
| 23d | `docs/runbooks/` + `ops/runbooks/` | **INDEX** | — (cross-reference to §16c) |
| 23e | Other docs subdirs | **INDEX** | — |
| 24 | Confirmation-ledger harvest (43 items) | **INDEX** | — (Needs Confirmation rollup is exactly the INDEX's job per plan §3) |
| 25 | Inventory totals | **INDEX** | — (top-line counts belong with the navigation overlay) |

## 4. Anomaly resolutions (from PASS 0 flags)

| Anomaly | Resolution |
|---|---|
| Canonical tree subdirs (`manual/`, `data_dict/`, `appendix/`, `notes/INDEX.md`) referenced by reconciliation plan but **not on disk** | Add to **INDEX §3 (Canonical sources)** as a confirmation-ledger item: "Reconciliation plan §3 references `docs/architecture/manual/` etc. as canonical; these subdirs are not present on the live tree. Either they were removed, the plan describes a future state, or they live elsewhere. Until confirmed, the handover routes into the files that DO exist (`docs/architecture/*_spec.md`, `docs/adr/`, `ops/runbooks/`, `docs/RUNBOOK.md`, `docs/acceptance-criteria.md`, `docs/SERVICE_INVENTORY.md`, `georag-architecture.html`)." |
| CLAUDE.md tech-snapshot drift (Qwen line reverted to `Qwen3-30B-A3B-Instruct AWQ`) | Add to confirmation ledger: "Live model per docker-compose vLLM `command:` is `Qwen/Qwen3-14B-AWQ` (default). CLAUDE.md snapshot now lists `Qwen3-30B-A3B-Instruct AWQ`. Confirm which is the canonical production model — the handover will document whichever is in compose at build time." Treat compose as source of truth. |

## 5. Target outlines (deduped, reordered)

### 5.1 `HANDOVER_INDEX.md`

```
1. Purpose + provenance banner
2. The four documents (one line each: SAD / DFS / API / CICD)
3. Canonical sources
   3.1 georag-architecture.html (master spec)
   3.2 docs/adr/ (12 ADRs) — table of {#, title, status}
   3.3 docs/RUNBOOK.md, docs/OPERATOR-AFTERNOON.md, docs/acceptance-criteria.md
   3.4 docs/SERVICE_INVENTORY.md
   3.5 ops/runbooks/ (38 scenario runbooks)
   3.6 docs/architecture/*_spec.md + *_design.md files (per existing tree)
   3.7 docs/architecture/manual/ + data_dict/ + appendix/ + notes/ — FLAGGED (not on disk)
4. Inventory totals (the §25 top-line table from the manifest)
5. Needs Confirmation (consolidated rollup, deduped — the 43 items from manifest §24 + 2 PASS-0 anomalies)
6. Operator / contact handoff (hosting target, registry, secrets owner)
```
**Removed (vs current INDEX):** the pass-12 / pass-13 audit tables, the
60+ row topic-coverage audit table, all "pass N" language. Substance
absorbed into §3 + §5.

### 5.2 `SAD.md` — Solution Architecture Document

```
1. System overview
   1.1 Product summary
   1.2 Primary users
   1.3 Business purpose
2. Architecture summary + topology diagram
   2.1 Stack table (one row per layer)
   2.2 Mermaid topology diagram
   2.3 Profile-gated compose stack (dev-light / dev-data / dev-ingest / dev-monitor / dev-llm / dev-full)
3. Component architecture
   3.1 Frontend / Inertia / shared props / layouts / hooks / pages map
   3.2 Application / Laravel / Octane / routes / middleware / queues
   3.3 Domain service / FastAPI / routers / agents / LangGraph subgraphs
       3.3.1 Agent catalog (42 @georag_agent grouped by phase + risk-tier)
       3.3.2 LangGraph subgraphs (3) + intent labels (8)
   3.4 Ingestion (Dagster + Hatchet) — references DFS for data flows
   3.5 Database + storage layer — references DFS for schema detail
   3.6 Auth / session layer — references API for auth contracts
   3.7 External integrations — Anthropic, Logfire, GHCR, etc.
   3.8 File / media storage — references DFS
   3.9 Notifications / email
   3.10 Admin tools — Inertia Foundry + Admin page map
4. Cross-cutting concerns
   4.1 Security — headers/CSP, policies + form requests, gates
   4.2 Tenancy + RLS — app.workspace_id GUC, acquire_scoped contract
   4.3 Observability — Prometheus (12 jobs, 64 alerts, 13 rule files), Loki, Tempo, OTel, Pulse recorders, Grafana dashboards (17)
   4.4 Config envelope — config/ file inventory, env surface, feature flags
   4.5 Orchestration discipline — Laravel queues vs Hatchet vs Dagster vs Kestra boundary
5. Key decisions
   5.1 ADR table (one line each, pointer to docs/adr/)
   5.2 CLAUDE.md hard rules (9 rules, one line each)
6. Needs Confirmation
```
**Removed (vs current SAD):** the §6s1–§6s55 wall (collapses into §4),
§4g–§4k after §6 (restored to §4), §5a–§5g after §6 (restored to §4),
duplicate database content (DFS owns), every "pass N / earlier pass /
net-new / doc-phase" reference.

### 5.3 `DFS.md` — Data Flow Specification

```
1. Primary data domains (PII, drill data, reports, public-geoscience, audit, chat/answers, ingestion provenance, embeddings, KG)
2. End-to-end data flows
   2.1 Document ingestion (bulk — Dagster path)
   2.2 User upload (Laravel → Hatchet path)
   2.3 RAG query — two-phase handshake + SSE event vocabulary
   2.4 Map / visualization flow
   2.5 Real-time fan-out summary (channel matrix)
   2.6 Audit / PII handling
   2.7 W3C Trace Context propagation
3. Data classification
4. Persistence + database architecture
   4.1 Database inventory (6 DBs — pointer)
   4.2 PG schemas (12) — table count per schema, link to migrations
   4.3 PG extensions (15) — purpose per extension
   4.4 PG functions (23) — by schema, link to source files
   4.5 PG triggers (7)
   4.6 PG materialized views
   4.7 Neo4j labels + relationship types
   4.8 Qdrant collections + payload indices + tenancy filter pattern
   4.9 Redis logical DBs
   4.10 ClickHouse (Langfuse-only)
   4.11 Cross-DB invariants (PG = system of record; Qdrant/Neo4j = derived indices)
5. Storage / cache / pub-sub topology
   5.1 SeaweedFS buckets + Laravel filesystem disks
   5.2 Compose named volumes
   5.3 Cache store routing
6. Outbound integrations + export formats
   6.1 Kestra-mediated notification flow
   6.2 Export formats (10) + GenerateExportJob lockstep
   6.3 Anthropic, Logfire, GHCR (outbound endpoints)
7. Reliability — backup/restore, retention, ops flows
   7.1 Ofelia backup cadence
   7.2 Hatchet backup/restore workflows
   7.3 Telemetry retention windows (Pulse 7d, Loki 30d, Tempo 7d)
   7.4 Reliability ops workflows (stale_run_detector, mv_refresh_silver, etc.)
8. Needs Confirmation
```
**Removed (vs current DFS):** the two separate `§4` sections (merged
into §4), duplicate database content (now sole owner), every "pass N"
reference.

### 5.4 `API_DOCUMENTATION.md`

```
1. API surfaces at a glance (table: surface / base path / auth / audience)
2. Authentication
   2.1 SPA cookie flow (Sanctum stateful)
   2.2 Token flow (Sanctum bearer)
   2.3 Service-to-service (X-Service-Key + JWT, kid rotation)
   2.4 Rate limits (5 buckets with budgets)
3. Laravel public API — /api/v1/*
   3.1 Auth
   3.2 Projects + drill data
   3.3 RAG queries + chat
   3.4 Trust / interpretation / charts
   3.5 Ingestion + vendor profiles
   3.6 Public REST API breadth (§3.3 — 9 endpoints)
   3.7 Public Geoscience (§10 — 5 endpoints)
   3.8 Dashboard (§3-§4 — 17 endpoints)
   3.9 Citation feedback
   3.10 Admin surfaces (web.php /admin/*) — operator UI routes
   3.11 Inertia/SPA routes (/up, /metrics, /horizon, /pulse)
4. Laravel /internal/* — FastAPI→Laravel bridge (7 endpoints)
5. FastAPI domain service
   5.1 URL families (table)
   5.2 Per-router endpoint table (109 endpoints organised by router)
   5.3 OpenAPI gating (OPENAPI_DOCS_PUBLIC)
   5.4 Server-side guards (middleware + timeouts)
   5.5 slowapi rate limits
6. WebSocket / Reverb channels
   6.1 Channel pattern table (30 channels)
   6.2 Event class catalog (11 classes + broadcastAs strings)
   6.3 Caddy → Kestra SSO bridge endpoints
   6.4 Echo client config + server identity
7. Tile API (Martin)
   7.1 URL shape + port
   7.2 Silver MVT function inventory (8 per-project)
   7.3 Public-geo MVT function inventory (8 tiles fns)
   7.4 Cache-bust epoch + Reverb tile invalidation event
8. Contracts
   8.1 Refusal code vocabulary (25 guard error codes)
   8.2 External-notification HMAC envelope (canonical JSON shape)
   8.3 7-section Trust-Summary payload
   8.4 Form Request validation rules (StoreQueryRequest 12-field context envelope, StoreExportRequest 10 formats, charts/render 8 kinds)
   8.5 Security response headers (X-Frame, CSP, HSTS conditional)
9. Versioning + deprecation
10. Needs Confirmation
```
**Removed (vs current API):** the `§7→§8→§7a→§8a→§9` ordering;
duplicate route enumeration; every "pass N" reference.

### 5.5 `CICD_PIPELINE.md`

```
1. Workflow inventory (table: 7 GitHub Actions workflows with triggers + purpose)
2. CI pipeline (ci.yml)
   2.1 Triggers + concurrency + tool pins
   2.2 Job graph (Mermaid)
   2.3 Per-job detail (laravel, python-lint, qwen-config-drift, python-test, frontend, pgtap, hadolint, docker-build)
3. CD pipeline (cd.yml)
   3.1 Trigger + topology (check-ci → deploy_dev → deploy_staging → deploy_production)
   3.2 Per-stage steps (SOPS decrypt, SSH deploy, health check)
   3.3 GitHub Environments + per-environment secrets table
   3.4 continue-on-error debt
   3.5 Production deploy primitive (SSH+compose primary; Helm chart alternative)
4. Container build + image registry/lifecycle
   4.1 Dockerfile internals (fastapi, laravel, dagster, backup-agent, langfuse-mcp)
   4.2 Image registry (GHCR), tag strategy, multi-arch, Trivy + SBOM
5. Test posture
   5.1 Test environment (phpunit.xml vs phpunit.pgsql.xml, PHPStan, Pint, Playwright, Vitest)
   5.2 Test inventory (Laravel feature/unit, pgTAP files, FastAPI markers, Dagster markers)
   5.3 Quality gate matrix (blocking vs warn-only)
6. Operations
   6.1 Secrets management (SOPS + age, .env.production.enc, operator preflight O-01..O-07)
   6.2 Database init sequence (cold-start) + migration connection contract (pgsql_migrations)
   6.3 Hatchet worker-pool selection (WORKER_POOL=ingestion|ai|all)
   6.4 Hatchet engine compose env (12 SERVER_* vars) + production hardening checklist
   6.5 Dagster schedules + sensors (6 schedules + 1 sensor)
   6.6 Ofelia backup cron labels (4 jobs)
   6.7 Hatchet cron-triggered workflows (30 declarations — table)
   6.8 Dagster asset checks (27 across 6 files — table)
   6.9 Alert routing (alertmanager 3 receivers; production swap-in template)
   6.10 Dependabot ecosystems (5)
   6.11 Operator scripts (scripts/operator/ + ops/setup/)
   6.12 Evidence artifacts (ops/baselines/ + ops/audit/)
7. Local / dev workflow (composer dev, npm dev, octane:reload caveat)
8. Needs Confirmation
```
**Removed (vs current CICD):** duplicate `§4`, `§3a`/`§3b` interleaved
after `§4`, `§6a0` before `§6a`, every "pass N" reference.

## 6. Mapping coverage check

Every line in `HANDOVER_MANIFEST.md` §1–§25 has a row in §3 above.
Manifest sub-sections (e.g. 1a, 1b, 4a, 4b, 5a, 5b, 7a–7j, 8a, 8b,
9a, 9b, 13a–13d, 16a–16e, 17a–17d, 23a–23e) are listed individually.

Items needing split-rule treatment: **12 of 51 mapped lines** carry an
explicit split rule preventing accretion on rebuild.

## 7. What happens next (per plan)

When you approve this mapping + the 5 outlines, PASS 2 begins:

1. Harvest existing-doc "Missing / Needs Confirmation" sections into
   the consolidated ledger.
2. Rebuild each doc from its approved skeleton in order:
   `HANDOVER_INDEX` → `SAD` → `DFS` → `API_DOCUMENTATION` → `CICD_PIPELINE`.
3. Tick `COVERAGE.md` (derived from manifest §1–§25) as items land.
4. Verify with grep: no orphan `§NsM` sections, no duplicate top-level
   section numbers, no `pass [0-9]` / `net-new` / `earlier pass`
   strings.

**STOP. Approve mapping + outlines before any rewrite.**
