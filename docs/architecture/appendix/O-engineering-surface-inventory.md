# Appendix O — Engineering Surface Inventory

Status: **Draft.** Closes the long-tail gaps the previous passes left.
Where M + N cataloged agents / ML, this appendix catalogs **everything
else with a code surface**: Laravel app/, FastAPI peripherals, frontend
components, tests, CI/CD, Dockerfiles, Claude Code project setup,
configs, and the remaining docs.

> Goal: nothing significant in the repo should be invisible from the
> manual. If you can't find something, this appendix should point you
> at it.

---

## 1. Laravel `app/` surface

`composer.json` defines the autoload root `App\\` → `app/`.

| Path | Count | Groups | Catalog |
|---|---:|---|---|
| `app/Http/Controllers/` | **101** | `Admin/`, `Api/`, `Dashboard/`, `Foundry/`, `Internal/`, `PublicGeoscience/`, plus 7 top-level (CitationFeedback, ChartsGallery, InterpretationWorkspace, OAuthIngest, Onboarding, PublicGeoscience, base `Controller`) | Grouped by product surface; one controller per route family |
| `app/Services/` | **38** | Bridges to FastAPI, Hatchet client, Kestra basic-auth, NI 43-101 builder, citation lifecycle, etc. | Includes `FastApiJwtMinter`, `LaravelBridge`, `HatchetClient`, `KestraSsoController` helpers |
| `app/Models/` | **34** | Eloquent models | See §1.1 |
| `app/Events/` | **11** | Reverb broadcast events | See §1.2 |
| `app/Jobs/` | **3** | Horizon queue jobs (uploads, embeddings, exports) | |
| `app/Listeners/` | 0 | (none — listeners aren't currently used; uses Reverb events directly) | |
| `app/Providers/` | **3** | `AppServiceProvider`, `BroadcastServiceProvider`, `RouteServiceProvider` | Rate limiters defined in RouteServiceProvider |
| `app/Console/Commands/` | **6** | Artisan commands (data backfill, smoke runs, ingest helpers) | |
| `app/Enums/` | **4** | TitleCase enum keys per CLAUDE.md PHP rule | |
| `app/Policies/` | **1** | Workspace authorization policy | |
| `app/Support/` | **3** | `SetsWorkspaceRlsContext` (the GUC writer — see [Ch 11](../manual/11-tenancy-and-rls.md)), plus 2 trait/helper |
| `app/Pulse/` | (varies) | Custom Pulse recorders | [Ch 12 §5](../manual/12-observability.md) |

### 1.1 Eloquent models

| Model | Source table |
|---|---|
| `Alteration` | `silver.alterations` |
| `ChatConversation` / `ChatMessage` | `silver.chat_conversations` / `silver.chat_messages` |
| `Collar` | `silver.collars` |
| `ColumnMapping` | `silver.column_mappings` (CSV header → canonical) |
| `Export` | `public.exports` |
| `Geochemistry` | `silver.geochemistry` |
| `LithologyLog` | `silver.lithology_logs` (legacy — coexists with `silver.lithology`) |
| `Project` | `silver.projects` |
| `QueryAuditLog` | `audit.query_audit_log` |
| `Report` | `silver.reports` |
| `Sample` | `silver.samples` |
| `SavedMapView` | `silver.saved_map_views` |
| `Structure` | `silver.structures` |
| `Survey` | `silver.surveys` |
| `User` | `public.users` (Sanctum-bound) |
| `VendorProfile` | `silver.vendor_profiles` |
| `WellLogCurve` | `silver.well_log_curves` |

Plus ~17 more in subdirectories (Bronze, Silver, Gold, Audit groupings).
Convention per CLAUDE.md: prefer descriptive method names
(`isRegisteredForDiscounts`, not `discount()`).

### 1.2 Reverb broadcast events

| Event | Channel pattern | Producer | Consumer |
|---|---|---|---|
| `QueryStreamEvent` (QueryToken/Citation/Complete/Refusal) | `query.streaming.{run_id}` | FastAPI → Laravel via internal endpoint | Chat.tsx |
| `IngestionProgressBroadcast` | `ingestion-progress.{workspace_id}` | Hatchet workers + Dagster `commit_ingestion_run` | IngestionRuns.tsx, DrillReview.tsx |
| `WorkspaceDataUpdated` | `workspace-data-updated.{workspace_id}` | Multi-source | Every page that re-fetches on data-change |
| 8 more | (see `routes/channels.php` 178 lines) | | |

See [Appendix B §1](B-event-payloads.md) for the exact JSON shapes.

### 1.3 Laravel `config/` (20 files)

| Config | Notes |
|---|---|
| `app.php` | App name, env, key, URL, locale, providers |
| `ai.php` | **Laravel AI SDK** (`laravel/ai` v0) config — model providers, fallback chain |
| `auth.php` | Sanctum guard |
| `broadcasting.php` | Reverb driver |
| `cache.php` | Redis driver |
| `cors.php` | SPA CORS |
| `dashboard.php` | Foundry dashboard layout |
| `database.php` | Three PG connections: `pgsql` (runtime), `pgsql_migrations` (DDL), `pgsql_hatchet`, `pgsql_kestra` (read-only views) |
| `filesystems.php` | SeaweedFS S3 driver |
| `horizon.php` | Queue supervisors (see [Ch 07 §1](../manual/07-orchestration.md)) |
| `inertia.php` | SSR + version pinning |
| `logging.php` | Loki channel + authz_audit-* tail |
| `mail.php` | Notification mailer |
| `octane.php` | Swoole worker config |
| `pulse.php` | Pulse recorder config |
| `queue.php` | Default queues |
| `reverb.php` | Reverb WS server config |
| `sanctum.php` | Stateful domains, PAT expiry |
| `services.php` | Anthropic + other 3rd-party keys |
| `session.php` | Session lifetime |

---

## 2. FastAPI peripheral surface

Beyond `app/agent/`, `app/agents/`, `app/services/`, `app/routers/`,
and `app/hatchet_workflows/`:

| Path | Count | Notes |
|---|---:|---|
| `src/fastapi/app/models/` | **14** | Pydantic models — `rag.py` (GeoRAGResponse, GuardErrorCode), `query.py`, `evidence.py`, `citation.py`, `agent_request.py`, etc. |
| `src/fastapi/app/audit/` | **5** | Audit-ledger emit helpers + chain hash recompute callable from Python |
| `src/fastapi/app/ocr/` | **14** | OCR helpers (docling backend, rapidocr backend, tesseract fallback, page-level confidence scorer, language detector) |
| `src/fastapi/app/middleware/` | 0 (now-inlined into `main.py`) | The `request_id` + `trace_id` + workspace-GUC middleware moved into `app/main.py` lifespan |
| `src/fastapi/scripts/` | **23** | One-shot operator scripts (eval runners, dataset prep, golden-question loaders) |
| `src/fastapi/app/services/ingest/` | **11** | Ingest-side helpers (PDF, LAS, XLSX, TIFF→PDF, Cameco-log ingester) — see [Ch 04](../manual/04-ingestion-flow.md) |
| `src/fastapi/app/services/publicgeo/` | **1** | Public-geoscience-specific helpers |
| `src/fastapi/app/services/geological_ontology/` | **4** | Ontology lookup + alias expansion |
| `src/fastapi/app/services/_archived/` | 2 | (deprecated — kept for rollback) |

---

## 3. Frontend depth

| Path | Count | Notes |
|---|---:|---|
| `resources/js/Pages/` | **96** | Inertia pages — [Ch 10 §2](../manual/10-frontend.md) catalogs the highest-value ones; rest follow the same convention |
| `resources/js/Components/` | **129** | Shared UI — see §3.1 |
| `resources/js/Components/Map/` | (within Components) | MapLibre wrappers + layer helpers |
| `resources/js/Hooks/` | **13** | Custom React hooks |
| `resources/js/Lib/` | **14** | Utilities (echo client, citation parser, date formatters) |
| `resources/js/Layouts/` | **5** | Inertia layout components (Foundry, PublicGeoscience, Auth, Admin, Onboarding) |

### 3.1 Components (top-level)

Groups: `Admin/`, `Analytics/`, `Foundry/`, `GuardError/`, `HoleAnalysis/`,
`PublicGeoscience/`, `chat/`, `ui/` (shadcn primitives) + top-level
files. Highlights from the top level:

`ChatMessage`, `CoverageTableCard`, `DataQualityFlagsBadge`,
`DrillHoleBrowser`, `DrillTrace3D`, `ErrorBoundary`,
`EvidencePacketBadge`, `ExperienceModeToggle`, `GeoPlot`,
`HoleDetailSheet`, `InlineViz`, `KnowledgeGraph`, `MapView`,
`ProjectContextBanner`, `ProjectSelector`, `ResolutionPreviewChip`,
`StereonetCard`, `StripLogViewer`, `TimelineCard`.

See [Appendix I](I-frontend-specs.md) for per-page acceptance specs;
component contracts are documented inside each component file's
docstring.

---

## 4. Tests surface

| Suite | Count / size | Where |
|---|---|---|
| **PHPUnit Feature** | many | `tests/Feature/` |
| **PHPUnit Unit** | many | `tests/Unit/` |
| **PHPUnit shared** | — | `tests/TestCase.php`, `tests/bootstrap.php`, `tests/Concerns/` |
| **Playwright e2e** | — | `tests/e2e/`, `playwright.config.ts` |
| **Fixtures** | — | `tests/fixtures/`, `tests/golden_questions/` (golden YAML), `tests/load_k6/` |
| **FastAPI pytest** | **234 test files** | `src/fastapi/tests/` |
| **Dagster pytest** | **68 test files** | `src/dagster/tests/` |
| **pgTAP** | **12 SQL test files** | `database/tests/pgtap/` — see §4.1 |
| **Test-DB seeders** | many | `database/seeders/` — see §4.2 |
| **Factories** | many | `database/factories/` |

### 4.1 pgTAP test files

| File | Coverage |
|---|---|
| `01_core_schema.sql` | Schema presence + key tables |
| `02_evidence_model.sql` | evidence_items + answer_runs lineage |
| `03_rls_baseline.sql` | Baseline RLS coverage |
| `08_silver_mvt_functions.sql` | Every `silver.pg_*_by_project` function signature |
| `09_public_geoscience_mvt_functions.sql` | Public geo MVT wrappers |
| `10_golden_mvt_snapshots.sql` | Bit-identical MVT byte snapshots |
| `11_rls_workspace_isolation.sql` | Cross-workspace fence test |
| `12_phase3_ocr_confidence.sql` | OCR quality table contract |

Plus `golden/` subdir for snapshot baselines.

### 4.2 Notable seeders

- `GoldenQuestionsSeeder` — seeds `eval.golden_questions` from the
  golden YAML.
- `CgiVocabSeeder` — CGI ontology vocabulary.
- `GeologicalOntologyMechanicalSeeder` — auto-derived ontology terms.
- `Phase0AgentTimeoutsSeeder` — default `workspace.agent_timeouts`.
- `DemoUserSeeder` / `DemoHoleAnalysisSeeder` / `DatabaseSeeder`.

---

## 5. CI/CD — GitHub Actions (7 workflows)

`.github/workflows/`:

| Workflow | Trigger | What it runs |
|---|---|---|
| `ci.yml` | PR + push to main | PHP composer + pint + phpstan + phpunit; JS typecheck + eslint + vitest + playwright smoke; pgTAP; pytest unit |
| `cd.yml` | push to main | Build + publish images; deploy to staging |
| `chaos.yml` | weekly | Toxiproxy-driven failure injection ([Appendix J §2.10](J-testing-matrix.md)) |
| `e2e.yml` | nightly | Full Playwright suite + RAG golden eval + backup restore smoke |
| `perf-baseline.yml` | weekly | k6 load suite (chat, ingest, tiles) — baseline drift |
| `release-rehearsal.yml` | manual / pre-release | Dry-run a tagged release end-to-end |
| `tenant-isolation-auditor.yml` | nightly | The Phase 0 agent (Appendix M §2) runs in CI to catch RLS coverage regressions |

---

## 6. Dockerfiles (7)

| Path | Service | Notes |
|---|---|---|
| `docker/laravel.Dockerfile` | laravel-octane/horizon/reverb | PHP 8.5 + Swoole + Composer + Node for Vite build |
| `docker/fastapi.Dockerfile` | fastapi + hatchet-worker-{ingestion,ai} | Python 3.13 + uv + system deps for parsers (qpdf, tesseract, etc.) |
| `docker/dagster.Dockerfile` | dagster-daemon/webserver | Python 3.13 + dagster-postgres |
| `docker/postgresql/Dockerfile` | postgresql | `postgis/postgis:18-3.6-alpine` base + h3, hypopg, pg_stat_kcache, pg_partman, pg_repack, pg_ivm |
| `docker/neo4j-exporter/Dockerfile` | neo4j_exporter | Python 3.13-slim + JMX-over-Bolt poller |
| `docker/backup-agent/Dockerfile` | backup-agent | Cron + aioboto3 (WAL upload to SeaweedFS) |
| `docker/langfuse-mcp.Dockerfile` | (optional) | Langfuse MCP server build |

Plus override compose files documented in [Ch 01 §"Override compose files"](../manual/01-services.md).

---

## 7. Build / dev tooling

| File | Purpose |
|---|---|
| `phpstan.neon` + `phpstan-baseline.neon` | PHP static analysis (level 9 per `php-pro` skill) |
| `phpunit.xml` + `phpunit.pgsql.xml` | PHPUnit configs — default (sqlite) and PG-backed |
| `pint.json` | Laravel Pint formatter rules |
| `tsconfig.json` | TypeScript compiler config (React 19 + Inertia) |
| `vite.config.ts` | Vite bundler config; Inertia plugin pinning |
| `vitest.config.ts` | Vitest test runner config |
| `playwright.config.ts` | Playwright browser-test config |
| `boost.json` | **Laravel Boost MCP** config — pinned package versions consumed by the Boost tooling |
| `.mcp.json` | Repo-scoped MCP server registrations |
| `openspec/config.yaml` | OpenSpec workflow config (the experimental artifact-driven change workflow) |

---

## 8. `.claude/` — Claude Code project setup

This is meta-architecture but operationally critical for anyone using
Claude Code on the repo.

### 8.1 Subagents (`.claude/agents/`)

Per-agent system prompts loaded by Claude Code's task-dispatch system:

| Agent | Role |
|---|---|
| `senior-reviewer.md` | Opus-tier architectural review (milestone gates only) |
| `backend-laravel.md` | All Laravel work |
| `backend-fastapi.md` | FastAPI + Pydantic AI |
| `data-engineer.md` | Ingestion pipeline + PostGIS + format parsers |
| `graph-engineer.md` | Neo4j + Cypher |
| `frontend-engineer.md` | React + Inertia + shadcn + visualisations |
| `devops-engineer.md` | Docker Compose + deployment + DB tuning |
| `test-engineer.md` | Test writing + golden query sets + snapshot tests |
| `boilerplate-writer.md` | Migrations + scaffolding + docstrings |
| `README.md` | How to invoke + budget guidance |

### 8.2 Skills (`.claude/skills/` — 36 skills)

Domain-specific instruction packs. Notable:

**GeoRAG-specific:**
- `georag-context` — repo orientation
- `georag-octane-bridge` — Laravel↔FastAPI HTTP bridge patterns
- `georag-rag-citations` — citation-first enforcement
- `georag-schema-contracts` — §04e/§04f schema enforcement
- `agent-wrapper` — `@georag_agent` contract authoring
- `audit-emit` — `audit.audit_ledger` emit helpers
- `hatchet-workflow` — Hatchet workflow authoring
- `phase-verify` — per-phase acceptance tests

**Workflow:**
- `adr-template` — ADR-0001-style template
- `commit-and-pr` — Conventional Commits + PR body template
- `openspec-{new,apply,continue,explore,ff,onboard,sync,verify,archive,bulk-archive}-change` — OpenSpec change workflow

**Framework / language:**
- `ai-sdk-development` — Laravel AI SDK (`laravel/ai` v0)
- `configuring-horizon`, `pulse-development`, `tailwindcss-development`,
  `inertia-react-development`, `laravel-{11-12-app-guidelines,
  best-practices, mcp, patterns, security, specialist, tdd,
  verification}`, `php-{best-practices, pro}`
- `postgres-migration` — GeoRAG-specific migration conventions

### 8.3 Other `.claude/` paths

- `.claude/commands/` — slash commands.
- `.claude/memory/` — the legacy per-developer memory dir (replaced by
  the checked-in `docs/architecture/notes/INDEX.md`).
- `.claude/settings.local.json` — local Claude Code settings.
- `.claude/launch.json` — launch configurations.
- `.claude/scheduled_tasks.lock` — the local cron lock (separate from
  the remote scheduled tasks under `C:\Users\GeoRAG\.claude\scheduled-tasks\`).

### 8.4 Root MCP config

`.mcp.json` lists the MCP servers registered repo-wide. The skills
above reference them.

---

## 9. Database fixtures / factories / seeders

`database/factories/`:
- Top-level: `CollarFactory`, `ColumnMappingFactory`, `ProjectFactory`,
  `SavedMapViewFactory`, `SurveyFactory`, `UserFactory`.
- Subdirs: `Eval/`, `Ops/`, `Silver/`, `Targeting/`.

`database/seeders/` (highlights — also see §4.2):
- `DatabaseSeeder` — orchestrator.
- `GoldenQuestionsSeeder` — `eval.golden_questions` from YAML.
- `CgiVocabSeeder` + `GeologicalOntologyMechanicalSeeder` —
  `silver.geological_ontology_*` seed.
- `Phase0AgentTimeoutsSeeder` — `workspace.agent_timeouts`.
- `PublicGeoscience/` subdir — public-geo lookup tables.
- `VendorProfiles/` subdir — drilling-vendor profile templates.

`database/fixtures/dashboard/` — Dashboard layout JSON fixtures.

---

## 10. Docs still un-indexed

Beyond the source-of-truth pointers in [MANUAL.md](../MANUAL.md) and the
[notes index](../notes/INDEX.md), these docs exist and have value:

### 10.1 Master-plan scope proposals (Sections 5–12)

`docs/master_plan_section{5,6,7,8,9,10,11,12}_scope_proposal.md` — the
master plan proposals that fed into the implementation. Use as the
"why was this built" reference.

### 10.2 Phase handoff packets

`docs/phase{0,100-105}_handoff.md` — phase boundary handoff documents.
Phase 0 is foundational; phases 100+ are the autonomous-run series.

### 10.3 Audit + review artifacts

- `docs/architecture_review_for_sonnet_2026_05_22.md` — Sonnet-tier
  architecture review checkpoint.
- `docs/audit_ledger_hash_recipe.md` — hash-chain recipe (already
  linked from Ch 03 + Ch 12).
- `docs/audits/` — dated audit reports.
- `docs/handoff-migration-status.md` — migration status.
- `docs/kyle-decisions.md` — SME decisions log.

### 10.4 Operational

- `docs/RUNBOOK.md` — primary operator handbook.
- `docs/SERVICE_INVENTORY.md` — on-call quick-ref.
- `docs/OPERATOR-AFTERNOON.md` — daily operator checklist.
- `docs/acceptance-criteria.md` — milestone acceptance gates.

### 10.5 Contracts / specs

- `docs/04f-public-geoscience-addendum.md` — §04f public-geo contract.
- `docs/chart_export_contract_spec.md` — chart export contract.
- `docs/mvt-nullable-numeric-convention.md` — MVT null trap (linked from
  [Ch 09 §4](../manual/09-martin-and-maplibre.md)).
- `docs/field-inventory-sk-tier2-tier3.md` — SK Tier 2/3 field inventory.
- `docs/model_migration.md` — model migration playbook.

### 10.6 Misc

- `docs/consultation_package_scoping.md`
- `docs/mining_hub_carl_meeting_brief.md`
- `docs/module-6-chunk-2-design.md`
- `docs/georag-claude-code-setup.md` — Claude Code project setup.
- `docs/langfuse-langgraph-tooling-setup.md`
- `docs/cc01_partial_items_kickoff.md` + `cc01_cc03_cc04_handoff/followups`
  — change-control packet history.

### 10.7 Logs (operational artifacts, not docs)

- `docs/eval_rerun_120.log`, `docs/lithology_derive_*.log`,
  `docs/overnight_*.log`, `docs/overnight_finalize*.log`,
  `docs/overnight_ingest.log`, `docs/overnight_ingestion_manifest.json`,
  `docs/overnight_ingestion_progress.jsonl`,
  `docs/overnight_ingestion_report.md`. Treat as audit trail; don't
  cite for design.

---

## 11. Routes recap

| File | Lines | Purpose |
|---|---:|---|
| `routes/api.php` | 324 | Versioned API (`/api/v1/*`) + internal Service-Key endpoints — [Appendix D](D-api-contract.md) |
| `routes/web.php` | 752 | Inertia page routes + admin surfaces + Pulse / Horizon dashboards |
| `routes/channels.php` | 178 | Reverb private-channel authorization — [Appendix B §1](B-event-payloads.md) |
| `routes/console.php` | 10 | Artisan command bindings |

---

## 12. Scripts (`scripts/` — 264 files)

`scripts/` holds operator + recovery + overnight-sweep scripts. Two
high-traffic groups:

- `_p17_*` / `_p18_*` / `_p19_*` / `_p20_*` / `_p21_*` — phase-numbered
  overnight-run sweeps (one-shot, mostly archived).
- `_archived/` — the cemetery.

Live scripts include `phase0_apply_extensions.sh`,
`overnight_uranium_ingest.sh`, `phase3_jwt_rotate.sh`, plus the LoRA
training entry points referenced in [Appendix M §10.1](M-agents-and-ml-catalog.md).

---

## 13. Top Laravel controllers (load-bearing routes)

Of the 101 controllers, the ~20 that route the load-bearing user flows.
Foundry product surface (one controller per Inertia page, per the
convention):

| Controller | Page / endpoint | Backs |
|---|---|---|
| `Foundry/ChatController` | `/foundry/projects/{p}/chat` | Chat (calls FastAPI `/v1/query`) |
| `Foundry/DrillReviewController` | `/foundry/projects/{p}/drill-review` | DrillReview (silver.review_queue) |
| `Foundry/IngestionRunsController` | `/foundry/projects/{p}/ingestion-runs` | IngestionRuns |
| `Foundry/IngestQualityController` | `/foundry/projects/{p}/ingest-quality` | IngestQuality dashboard |
| `Foundry/LakehouseController` | `/foundry/projects/{p}/lakehouse` | Lakehouse map+table view |
| `Foundry/DrillholeDetailController` | `/foundry/projects/{p}/holes/{h}` | Drillhole detail |
| `Foundry/HoleCompareController` | `/foundry/projects/{p}/holes/compare` | Hole comparison |
| `Foundry/ProjectAnalyticsController` | `/foundry/projects/{p}/analytics` | Plotly analytics |
| `Foundry/RetrievalInspectorController` | `/foundry/projects/{p}/retrieval-inspector` | Debug per-query retrieval traces |
| `Foundry/InvestigationsController` | `/foundry/projects/{p}/investigations` | Multi-turn investigation lineage |
| `Foundry/AuditLogController` | `/foundry/projects/{p}/audit-log` | `audit.audit_ledger` tail |
| `Foundry/CorpusController` + `Foundry/SourcesController` | `/foundry/projects/{p}/corpus|sources` | Sources inventory |
| `Foundry/OverviewController` | `/foundry/projects/{p}` | Project home dashboard |
| `Foundry/PortfolioController` | `/foundry/portfolio` | Cross-project portfolio |
| `Foundry/AssessmentSummaryController` | `/foundry/projects/{p}/assessment-summary` | NI 43-101 assessment summarisation |

Internal / Service-Key:

| Controller | Endpoint | Producer | Consumer |
|---|---|---|---|
| `Internal/IngestProgressBroadcastController` | `POST /api/internal/v1/ingest-progress/broadcast` | FastAPI / Hatchet / Dagster | Reverb `ingestion-progress.{ws}` |
| `Internal/WorkspaceDataUpdatedBroadcastController` | `POST /api/internal/v1/workspace-data-updated/broadcast` | Multi-source | Reverb `workspace-data-updated.{ws}` |
| `Internal/ReOcrTriggerController` | `POST /api/internal/v1/re-ocr` | Admin UI | Hatchet `re_ocr_page` |

Tiles:

| Controller | Endpoint | Backs |
|---|---|---|
| `Tiles/PublicGeoController` | `GET /tiles/public-geoscience/{src}/{z}/{x}/{y}` | Martin proxy (public_geo layers) |
| `Tiles/SilverController` | `GET /tiles/silver/{src}/{ws}/{z}/{x}/{y}` | Workspace-scoped Martin proxy (sets `app.workspace_id` GUC) |

Admin:

| Controller | Endpoint | Backs |
|---|---|---|
| `Admin/HatchetWorkersController` | `/admin/integrations/hatchet` | Worker dashboard (reads `pgsql_hatchet` connection) |
| `Admin/KestraSsoController` | `/admin/integrations/kestra/{path?}` | Kestra reverse-proxy with Sanctum auth |
| `Admin/MlTrainingController` | `/admin/ml/training-runs` | ML training admin surface ([Appendix M §11](M-agents-and-ml-catalog.md)) |

## 14. Laravel AI SDK (`config/ai.php`)

GeoRAG ships `laravel/ai` v0 (Boost-pinned). Configuration at
[config/ai.php](../../../config/ai.php). Worth its own subsection
because it's the "Laravel-side AI provider routing" layer — distinct
from the FastAPI-side `LLM_BACKEND` env (which routes the RAG path).

### Provider defaults

| Env var | Default | Used by |
|---|---|---|
| `AI_PROVIDER` | `openai` (points at vLLM via `OPENAI_URL`) | Non-RAG Laravel-side AI calls (admin tools, internal helpers) |
| `AI_IMAGE_PROVIDER` | `gemini` | Image generation |
| `AI_AUDIO_PROVIDER` | `openai` | Audio generation |
| `AI_TRANSCRIPTION_PROVIDER` | `openai` | STT |
| `AI_EMBEDDING_PROVIDER` | `openai` | If Laravel needs to embed for a non-RAG path |

### Hard rule

**Production RAG LLM calls happen in FastAPI / Pydantic AI, NOT
Laravel.** The Laravel AI SDK defaults are for non-critical admin /
internal helper paths only. Anyone considering a chat-path use of the
Laravel AI SDK should route it through FastAPI instead.

### Migration history

The Ollama → vLLM migration on 2026-05-10
([docs/model_migration.md](../../model_migration.md)) flipped the
default from `ollama` → `openai` (which points at vLLM via
`OPENAI_URL`). The vendored `ollama` provider entry stays registered
as a fallback option until the next cleanup pass.

## 15. FastAPI Pydantic models (`app/models/` — 13 files)

These are the canonical inter-process data shapes. Every wire-level
payload across the Service-Key boundary should round-trip through one.

| File | Models defined | Used by |
|---|---|---|
| [rag.py](../../../src/fastapi/app/models/rag.py) | `GeoRAGResponse`, `GuardErrorCode`, `RetrievalProfile`, `Intent`, `EvidenceItem` | The whole chat path |
| [answer_run.py](../../../src/fastapi/app/models/answer_run.py) | `AnswerRunRow`, `AnswerRunCreate` | Persistence shape for `silver.answer_runs` |
| [evidence.py](../../../src/fastapi/app/models/evidence.py) | `EvidenceItem`, `EvidenceRef`, `Citation` | Citation binding (`agent/citation_binding.py`) |
| [pdf.py](../../../src/fastapi/app/models/pdf.py) | `PdfPage`, `PdfFigure`, `PdfTable`, `ParseResult` | `ingest_pdf` workflow IO |
| [decomposition.py](../../../src/fastapi/app/models/decomposition.py) | `DecomposedQuery`, `Subquery` | `agent/decomposer.py` |
| [feedback.py](../../../src/fastapi/app/models/feedback.py) | `MessageFeedback` | `silver.message_feedback` writes |
| [conversation_state.py](../../../src/fastapi/app/models/conversation_state.py) | `ConversationState`, `Turn` | Multi-turn resolver state |
| [collaboration.py](../../../src/fastapi/app/models/collaboration.py) | `CollabAnchor`, `CollabComment` | `silver.collab_*` tables |
| [geological.py](../../../src/fastapi/app/models/geological.py) | `Formation`, `RockUnit`, `MineralOccurrence`, `Anomaly`, `Hypothesis` | Graph entity transport |
| [lineage.py](../../../src/fastapi/app/models/lineage.py) | `LineageRow`, `LineageGraph` | Lineage Reporter agent output |
| [review_queue.py](../../../src/fastapi/app/models/review_queue.py) | `ReviewQueueItem`, `ReviewDecision` | `silver.review_queue` IO |
| [retrieval_cache.py](../../../src/fastapi/app/models/retrieval_cache.py) | `CachedRetrieval` | Run cache for `agent/orchestrator/run_cache.py` |
| [assessment_summary.py](../../../src/fastapi/app/models/assessment_summary.py) | `AssessmentSummary` | Assessment Summary page payload |

## 16. Claude Code skills index (36 skills)

`.claude/skills/`. Each is a self-contained instruction pack that
Claude Code dispatches when its trigger conditions match the user's
prompt. Grouping for navigability:

### 16.1 GeoRAG-specific (8 skills)

| Skill | Trigger / use |
|---|---|
| `georag-context` | Repo orientation — invoke at session start |
| `georag-octane-bridge` | Laravel↔FastAPI HTTP bridge patterns (when writing `Http::client` calls, FastApiJwtMinter, chunked-transfer to Reverb) |
| `georag-rag-citations` | Citation-first enforcement (query controllers, answer_runs persistence, refusal-path UX) |
| `georag-schema-contracts` | §04e/§04f schema enforcement (Eloquent models, migrations, FormRequests for geological domain) |
| `agent-wrapper` | `@georag_agent` contract authoring |
| `audit-emit` | `audit.audit_ledger` emit helpers (canonical action_type list) |
| `hatchet-workflow` | Hatchet workflow authoring |
| `phase-verify` | Per-phase acceptance tests (mentioned in "Phase 0", "acceptance test", "Definition of done") |

### 16.2 Workflow (11 skills)

| Skill | Use |
|---|---|
| `adr-template` | ADR-0001-style template for new architecture decisions |
| `commit-and-pr` | Conventional Commits + GeoRAG PR body template |
| `openspec-new-change` | Start a new OpenSpec change |
| `openspec-explore` | Thinking-partner mode for early ideas |
| `openspec-continue-change` | Next artifact in an in-flight change |
| `openspec-ff-change` | Fast-forward through all artifacts |
| `openspec-apply-change` | Implement an OpenSpec change |
| `openspec-verify-change` | Validate implementation vs artifacts |
| `openspec-sync-specs` | Sync delta specs to main specs |
| `openspec-archive-change` | Finalize + archive a completed change |
| `openspec-bulk-archive-change` | Archive multiple parallel changes |
| `openspec-onboard` | Guided onboarding for OpenSpec |

### 16.3 Framework / language (17 skills)

| Skill | Use |
|---|---|
| `ai-sdk-development` | Laravel AI SDK work (the `laravel/ai` v0 package — see §14) |
| `configuring-horizon` | Laravel Horizon lifecycle (install / configure / troubleshoot) |
| `pulse-development` | Laravel Pulse setup + custom card development |
| `tailwindcss-development` | Tailwind v3/v4 utility-class work |
| `inertia-react-development` | Inertia.js v2 React patterns |
| `laravel-11-12-app-guidelines` | Laravel 11/12 stack guidelines |
| `laravel-best-practices` | Laravel 13 conventions |
| `laravel-mcp` | Laravel MCP server development |
| `laravel-patterns` | Laravel architecture patterns |
| `laravel-security` | Authn/authz, validation, CSRF, etc. |
| `laravel-specialist` | Laravel 10+ Eloquent, Sanctum, Horizon, Livewire |
| `laravel-tdd` | TDD with PHPUnit + Pest |
| `laravel-verification` | Verification loop: env / lint / static / test / security |
| `php-best-practices` | PHP 8.x patterns + PSR + SOLID |
| `php-pro` | PHP 8.3+ + Laravel/Symfony + PHPStan level 9 |
| `postgres-migration` | GeoRAG-specific migration conventions (RLS, pg_partman, MVT funcs, role grants) |

### 16.4 How they are invoked

Two paths:
1. **Auto-trigger** — Claude Code matches the user's prompt against each
   skill's `triggers:` block. Strong matches auto-activate.
2. **Slash command** — user explicitly types `/<skill-name>`.

Skills don't have state and don't persist between invocations. They're
instruction packs only.

## 17. What is STILL not enumerated

Honest residue:

- **Per-controller request/response signatures** — would need to read
  every controller; left for OpenAPI generation ([Appendix D §8](D-api-contract.md)).
- **Per-component prop contract** — 129 components; documented inside
  each component's docstring. Catalog left to Storybook (planned, not
  yet wired).
- **Per-pgTAP assertion list** — 12 SQL files; assertions are
  enumerated inside each file.
- **Per-skill detail** — 36 skills; each one's description is in its
  own SKILL.md.
- **Per-master-plan-section detail** — 8 scope proposals; read directly.

Generators in [Appendix F §1](F-data-dictionary.md) (data dict) and
[Appendix D §8](D-api-contract.md) (OpenAPI union) would auto-close
the first two without manual catalog work.
