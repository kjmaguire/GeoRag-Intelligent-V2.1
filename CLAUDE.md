# GeoRAG — Claude Code Project Context

This file is loaded into every Claude Code session in this project. It defines
the rules and conventions that apply to ALL work, regardless of which agent is
active. Keep this file short and high-signal — it gets read every turn.

## What this project is

GeoRAG is a geological intelligence platform that ingests decades of fragmented
exploration data (drill logs, NI 43-101 reports, geophysics, GIS layers) and
lets geologists query it in natural language with cited answers, interactive
visualizations, and export to industry modeling tools. It targets junior mining
and exploration companies with private cloud or on-premise deployment.

## The source of truth

**`georag-architecture.html`** is the complete architecture reference. It
contains every technology decision, data schema, interface contract, deployment
detail, performance tuning, and acceptance criterion. **Read the relevant
section before starting any task.** When code and the architecture doc
disagree, the doc is correct and the code needs fixing (or the doc needs an
explicit, deliberate update with a reason).

New to this project? Start with Section 00 (README) inside the architecture
doc for the reading order.

Since v1.52 (2026-09-06) the doc distinguishes **target state** from **as
built**: sections describe the intended architecture, and a dated *As built*
note says exactly what exists where the code has not caught up. A *Corrected*
note records what a section used to claim. Read the as-built note before
building on a section. `docs/architecture/manual/` is the file-cited
companion; `tests/Unit/ArchitectureDocSchemaParityTest.php` fails CI if the
doc names a `schema.table` no migration creates.

**As built 2026-09-24** — three of the pieces this note used to call
design-only now have a chat-adjacent UI: **feedback UI** (§10p —
`Components/FeedbackControls.tsx` + the new `POST
/api/v1/answer-runs/{id}/feedback` Laravel route), **evidence inspector**
(§10s — `Components/EvidenceInspector.tsx`, a Sheet opened from a citation
chip, built on the existing `citations/resolve` route rather than the
still-unwired `GET /v1/evidence/{id}`), and **refusal panels** (§10u —
`Components/RefusalPanel.tsx`, off the `failed` frame's `error`/`code` or
the `completed` frame's `refusal_payload`). *Corrected 2026-09-06
(superseded in part):* "feedback UI, follow-up chips, evidence inspector,
conflict/freshness UX, refusal panels, Lakehouse, row-level drill review
are all design-only today." **Follow-up chips, conflict/freshness UX,
Lakehouse, and row-level drill review are still design-only** — follow-up
chips because FastAPI generates no suggestions to render (§10q as-built
note); the other three untouched by this pass. See each section's own
as-built note for exact detail.

## Hard rules — never violate

1. **No Streamlit.** Streamlit is permanently rejected. The frontend is React +
   Inertia.js + shadcn/ui + Tailwind. If you see Streamlit referenced anywhere
   in external examples, translate it to our stack.

2. **Async-native drivers only in FastAPI.** `asyncpg` for PostgreSQL,
   `redis.asyncio` (aioredis) for Redis, async Qdrant client. Synchronous
   drivers in async handlers are a blocker-level bug.

3. **Octane-safe Laravel code.** The Laravel app boots once and stays in
   memory. No static state leaks between requests. No singletons holding
   request data.

4. **Citations are mandatory on every RAG response.** Every claim the LLM
   makes must include a `source_chunk_id` or be rejected by the typed
   output validation in the graph's `validate` node
   (`app/agent/hallucination/layer2_typed_output.py`). Pydantic AI itself is
   vestigial — the guards live in `orchestrator_validators.py`. There is no
   "best-effort" citation mode; `citation_mode` is always
   `posthoc_span_resolution`.

5. **Follow Section 04i hallucination prevention.** The six-layer design is
   the contract for any code touching the RAG pipeline: retrieval quality
   gate → typed output validation → numerical claim verification → entity
   resolution → chunk provenance → geological constraint rules. As built,
   six guards run across `orchestrator_validators.py`,
   `layer1_retrieval.py` and `layer5_provenance.py` (typed output, numbers,
   entities, constraints, plus advisory completeness, plus retrieval
   quality and chunk provenance — restored 2026-09-24). The retrieval gate
   is no longer only the flat reranker score floor
   (`RERANKER_SCORE_THRESHOLD_HOSTED`/`RERANKER_SCORE_THRESHOLD`, still
   applied per-chunk in `app/agent/tools.py:search_documents`): a
   query-level Layer 1 check in `app/agent/hallucination/layer1_retrieval.py`
   now hard-refuses (before the LLM is ever called, from `assemble_node`)
   when nothing cleared the floor from ANY store, and flags "weak"
   marginal retrieval as an advisory warning. Provenance is no longer only
   enrichment either: `app/agent/hallucination/layer5_provenance.py`'s
   `gate_citation_provenance` (called from `validate_node`, before
   `enrich_provenance`) rejects any document-chunk citation whose
   `source_chunk_id` does not resolve to a chunk actually retrieved for
   that query, or that carries no document id — the rejected citation is
   dropped and `should_retry` is forced, floor-and-banner, the same as a
   genuine Layer 3/4/6 finding. Both gates default ON
   (`RETRIEVAL_QUALITY_GATE_ENABLED`, `CHUNK_PROVENANCE_GATE_ENABLED`).
   Weakening the four pre-existing guards is not welcome; neither is
   weakening these two now that they exist.

6. **Schemas in Section 04e are contracts.** Don't invent fields. Don't skip
   constraints. Don't change enumeration values without SME approval.

7. **Don't duplicate orchestration.** Laravel queues handle short
   user-triggered async work. Hatchet handles ingestion, scheduled crons and
   anything needing durable retries. Never overlap. There is no Laravel
   scheduler — `routes/console.php` registers no scheduled tasks, so every
   recurrence is a Hatchet cron, a GitHub Actions cron, or an EventBridge
   schedule.

8. **MapLibre GL, not Mapbox GL.** Licensing matters for on-prem deployments.

9. **No knowledge graph.** Neo4j was removed on 2026-07-28 and the sync
   workflow deleted with it. The graph half of hallucination Layer 4 is
   permanently fail-open. Don't add a graph store, a driver, or a Cypher
   query without an ADR that supersedes this.

## Technology snapshot

- **Frontend**: React 19 + Inertia.js v3, shadcn/ui + Tailwind v4, MapLibre GL 5, Plotly (`react-plotly.js`). React Flow (`@xyflow/react`) was removed 2026-08-28; no graph view exists.
- **Application**: Laravel 13.32 on Octane (Swoole), Horizon (two supervisors: `default` + `llm`), Reverb, Sanctum, Pulse (local-only — no `viewPulse` gate). Three Horizon jobs total; everything else is Hatchet.
- **Domain Service**: FastAPI 0.141 (floor 0.136) on Python 3.13, LangGraph 1.x, Pydantic AI 2.x (vestigial), asyncpg, redis.asyncio, async Qdrant client. Streaming is SSE (`status/bind/delta/citation/completed/failed`) from `POST /internal/queries`; Laravel re-broadcasts frames as `QueryStreamEvent` on Reverb.
- **Data Stores**: PostgreSQL 18 + PostGIS 3.6 (`postgis/postgis:18-3.6-alpine`, no patch pin; RDS for PostgreSQL 18 in production) behind PgBouncer edoburu 1.25.1 (transaction mode), Qdrant v1.19.1 (`georag_chunks`, 1024-dim dense + SPLADE++ sparse), Redis 8.6.4 (8.10 in production, on EFS with AOF on since ADR-0022), SeaweedFS 4.35 in compose / AWS S3 in production, both through `STORAGE_BACKEND=s3_compatible` — the only value left; `azure_blob` is a loud error since ADR-0022 (ADR-0001 covers the compose half; ADR-0022 supersedes ADR-0020), Martin 1.11.0 for MVT tiles. Gold is plain tables written by `promote_silver_to_gold`; the only materialized view is `silver.mv_collar_summary`. PgBouncer is the compose topology only — **the AWS deployment has no pooler**, so transaction-mode constraints do not apply in production and the RLS session GUC survives. A line here used to say eight core silver tables (`projects`, `collars`, `surveys`, `lithology_logs`, `samples`, `reports`, `spatial_features`, `well_log_curves`) have no versioned CREATE; that was checked on 2026-09-16 and is false — all eight are created by `database/migrations/2026_04_09_1800*` and `2026_04_10_1201*`, and a fresh database gets them from `php artisan migrate` alone.
- **Workflow**: one merged `hatchet-worker` (`WORKER_POOL=all`, 51 registered workflows, inventoried in §07b) on `hatchet-lite` with a Postgres-backed queue. No `hatchet-worker-ingestion` / `hatchet-worker-ai` services exist. Per-store `backup_*` workflows were deleted 2026-08-23; production relies on RDS PITR (35 days) for Postgres and S3 versioning for object storage.
- **Ingestion**: Hatchet workflows `ingest_pdf` / `ingest_tabular` / `ingest_spatial` / `ingest_well_logs` / `tiff_normalize` / `ingest_zip_archive`, dispatched through `POST /internal/v1/shadow/{workflow}/trigger`. Parsers live in the local `georag_geoparsers` package (Polars, GDAL via pyogrio/GeoPandas/rasterio, pyproj, lasio, openpyxl/xlrd, ezdxf, mdbtools, rapidfuzz — no DuckDB, segyio or obspy). In-process PDF stack (§04p — replaces RAGFlow per ADR-0002; scanned-page OCR is Cohere Parse 5 (`parse-v5.0`), on **Cohere's own API** since ADR-0023 — `POST {COHERE_BASE_URL}/v2/parse`, keyed by `COHERE_API_KEY` (ADR-0019 chose the model, 2026-09-02; ADR-0022 briefly put it on a Bedrock Marketplace endpoint), Tesseract 5.5.2 from source as last resort — Azure Document Intelligence, PaddleOCR, docling and PyMuPDF are gone). Upload cap is 512 MB (`GEORAG_MAX_UPLOAD_BYTES`). Dagster was retired 2026-07-28 and its tree deleted 2026-08-28.
- **Deployment**: `docker-compose.yml` (16 services behind `dev-data` / `dev-full` profiles) for dev; **Amazon ECS Fargate** for production since 2026-09-08 (ADR-0022): ten services, no GPU, nightly stop/start via EventBridge Scheduler, alerts to one SNS email receiver. All of it is Terraform in `deploy/aws/terraform/` — unlike Azure, which had no IaC at all. Caddy, Kestra, Prometheus, Grafana, Loki and Tempo are defined nowhere in the repo. Helm: `charts/georag/` only (the stale `ops/charts/georag/` skeleton, which still provisioned Neo4j, was deleted 2026-09-06).
- **LLM**: **Cohere's own API**, Cohere Command A+ (`command-a-plus-05-2026`; dev + prod) over `POST {COHERE_BASE_URL}/v2/chat` with `COHERE_API_KEY`. `LLM_BACKEND` is `cohere` (default) | `bedrock` | `vllm` | `anthropic`; `azure` is a hard startup error naming the replacement. Chat lives in `app/agent/llm_cohere.py`, a sibling of `llm_bedrock.py` and of the Anthropic path rather than a branch of the OpenAI-compatible client; the pieces that are neither host's are in `app/agent/llm_common.py`. Anthropic Claude (`claude-opus-4-8`, prompt caching on) is wired as optional fallback. **ADR-0023 (2026-09-15) took the default off `bedrock` one week after ADR-0022 set it**, because Command A+ is an AWS *Marketplace* SageMaker package rather than a Bedrock model: A100/H100 instances that bill whether or not anything calls them, since a Marketplace endpoint has no idle state. `bedrock` stays selectable for an operator who deploys one anyway.
- **Embedding / rerank / OCR**: Bedrock in production, the three compose sidecars in dev (`embedding` Qwen3-Embedding-0.6B and `sparse` SPLADE++ on CPU, `reranker` Qwen3-Reranker-0.6B on the one GPU). `EMBEDDING_BACKEND=bedrock` → Cohere Embed v4 at 1024 dims (matches `georag_chunks`, so no Qdrant migration; switching backends still needs a full re-embed via `scripts/reset_embeddings_for_reencode.py`). `RERANKER_BACKEND=bedrock` → Cohere **Rerank 3.5**, NOT v4 — Bedrock does not serve v4, and `RERANKER_SCORE_THRESHOLD_HOSTED` (0.2, renamed from `_FOUNDRY`) was measured against v4 and is **carried over unvalidated**; it is the only retrieval-quality gate in the system. Re-measuring it is NOT a golden-set job — `tests/golden_questions/seed_template.yaml` is a skeleton with no chunk-level relevance labels, so it is blocked on SME labelling as well as on a corpus and credentials. `app/services/reranker.py` records the route that needs no labels. `OCR_ENGINE=cohere_parse` → Cohere Parse 5 on Cohere's own API, sharing `COHERE_API_KEY` with chat. `EMBEDDING_BACKEND` and `RERANKER_BACKEND` both default to `bedrock` in code and in compose, so an unset value selects the hosted backend rather than a model host that does not exist in production; `.env.example` sets `local` / `cross_encoder` explicitly for the sidecars. Set both identically on the query AND ingest paths — a mismatch writes one vector space and queries another.
- **Unverified wire shapes**: every model adapter says so at the top, on both hosts. The Foundry path had three behaviours confirmed by a live call on 2026-07-30 (JSON `response_format`, reasoning in a sibling field, Cohere `<|START_TEXT|>`/`<|END_TEXT|>` sentinels) and NONE of them carries over by assumption; Cohere Parse's shape has never been verified on any of the three hosts. The contracts are data — `app/services/bedrock_wire.py` (embed, rerank, the bedrock chat path) and `app/services/cohere_wire.py` (chat, parse) — so the first credentialed run is a diff rather than a discovery. **Two probes, and neither covers the other's models:** `ops/validation/bedrock_probe.py` (Embed v4, Rerank 3.5) and `ops/validation/cohere_probe.py` (Command A+, Parse 5). `aws-preflight.sh` A-11 fails until BOTH reports are committed. `ops/validation/tests/fake_cohere.py` lets the Cohere probe be exercised without a key — it found three real defects the first time it ran, including one live in the Bedrock probe.
- **No Marketplace endpoints, deliberately** — they bill while they exist, with no idle state, which is why ADR-0023 moved chat and OCR to Cohere's API. The nightly sweeps now touch only ECS and RDS; `BEDROCK_ENDPOINT_NOT_INSERVICE` and its Sev 1 alarm are gone. Embed v4 and Rerank 3.5 stay on Bedrock and are serverless — nothing accrues at rest. `aws-preflight.sh` A-09 fails if any SageMaker endpoint is found running.
- **The egress gate covers non-contracted providers, not every external host.** `app/agent/egress_gate.py` is default-deny on `allow_external_llm` and only `_call_anthropic_llm` calls it. **Kyle decided on 2026-09-15 (ADR-0023) that Cohere is inside the contracted set**, like Bedrock — same vendor, same commercial agreement, reached directly instead of through AWS's resale. So workspace text and page images DO leave AWS on the normal path, ungated, and that rests on no client contract requiring data residency. Do not wire the gate onto `llm_cohere.py` on general principle; if residency ever becomes a requirement, that is the mechanism, and it needs a migration defaulting the flag to true or every query refuses.
- **SPLADE++ has no hosted equivalent anywhere** — not on Bedrock, not on Cohere's own API. It runs as the `sparse` service in production, which is new: without it the sparse leg of hybrid retrieval does not exist.
- **Also present, worth knowing about (found by the 2026-09-16 stack audit, not previously documented here):**
  `livewire/livewire` v4 is a real installed composer dependency, transitive via `laravel/pulse`'s own dashboard — nothing in `resources/js` uses it, the frontend is still React + Inertia only. `kubernetes/manifests/` holds three raw K8s YAML files (`airgap.yaml`, `k3s.yaml`, `vanilla.yaml`, regenerated by `scripts/regenerate_k8s_manifests.sh`) as a second, lower-level on-prem deployment artifact alongside `charts/georag/` — the Helm chart is still the primary on-prem path. `scripts/operator/bootstrap-secrets.sh` + `preflight.sh` carry SOPS 3.9.4 + age encryption for the pre-ADR-0022 SSH-host/on-prem deploy model; it is not the AWS cutover gate (`aws-preflight.sh` is) and is kept because `charts/georag/` still targets on-prem/k3s. `tests/load_k6/*.k6.js` are Grafana k6 load-test scripts — the only plain `.js` in an otherwise all-TypeScript frontend. `src/fastapi/app/services/public_geo/registry.py` polls live provincial/federal government ArcGIS + WFS open-data endpoints (BC, SK, plus AB/MB/Federal license references) weekly via the `public_geo_sync` Hatchet cron — a real external vendor surface, not covered by the egress gate (government open data, not an LLM call). A `.codex/` directory (OpenAI Codex CLI config, mirroring several `openspec-*` skills) coexists with `.claude/`; it is inert to Claude Code and left as-is.

## Agent delegation

This project has specialized Claude Code subagents in `.claude/agents/`. Use
them for focused work — each has its own context window and domain expertise.
There are two families and the split matters: **layer agents write the code,
domain experts judge whether it is right.** When both apply, the layer agent
writes and the expert reviews.

**Layer agents**

- **`senior-reviewer`** (Opus) — architectural review at milestone gates ONLY. Read-only. Sparingly.
- **`backend-laravel`** (Sonnet) — routine Laravel feature work
- **`backend-fastapi`** (Sonnet) — routine FastAPI work
- **`data-engineer`** (Sonnet) — ingestion pipeline, PostGIS schemas, format parsers
- **`frontend-engineer`** (Sonnet) — routine React components
- **`devops-engineer`** (Sonnet) — docker-compose, the Helm chart, database tuning
- **`test-engineer`** (Sonnet) — all test writing, golden query sets
- **`boilerplate-writer`** (Haiku) — migrations, scaffolding, docstrings, simple docs

**Domain experts** (all Sonnet — the expertise is in the brief, not the tier)

- **`rag-expert`** — retrieval quality, citations, the six hallucination layers, refusals. *Read-only.*
- **`agentic-ai-expert`** — the LangGraph loop, guard chain, tool dispatch, budgets. *Read-only.*
- **`chat-expert`** — SSE → Reverb → Echo → React, every terminal path. *Read-only.*
- **`cohere-expert`** — Command A+, Parse 5, Embed v4, Rerank 3.5, and which host serves which
- **`aws-expert`** — ECS/RDS/Terraform, the power switch, the nightly sweeps, cost
- **`hatchet-expert`** — the 51 workflows, `on_crons`, durable retries, idempotency
- **`postgres-gis-expert`** — schemas, RLS and tenant isolation, GIST, PgBouncer, RDS
- **`gis-expert`** — CRS and datums, dip/azimuth, desurveying, Martin/MapLibre
- **`ingestion-gis-expert`** — the parsers, the PDF/OCR stack, medallion, provenance
- **`laravel-expert`** — Octane safety, Horizon, framework judgement calls
- **`react-expert`** — React 19 + Inertia v3 depth, streaming render performance
- **`stack-inventory-auditor`** — every language, package, base image,
  infrastructure resource, and external vendor call actually present in the
  repo, checked against what CLAUDE.md/the architecture doc/manual claim.
  *Read-only.* Inventories and flags drift; does not judge whether a
  technology choice is right.

`.claude/agents/README.md` has the full "which agent for which question" table
and the boundary rules where two agents overlap.

Claude Code will auto-delegate based on agent descriptions. You can also
invoke explicitly with `@agent-name` in a prompt.

## Budget discipline

Opus is rate-limited on Max plans and shared with other work. Use it only for:
- Initial architecture decisions
- Milestone gate reviews (via `senior-reviewer`)
- Hallucination prevention design discussions
- Interface contract authoring

Everything else goes to Sonnet agents or Haiku for boilerplate.

## Code style

- **Python**: Ruff for linting, Black formatting, type hints everywhere. Pydantic for data models. Use `async def` for anything touching I/O.
- **PHP**: Laravel Pint for formatting. PSR-12 style. Type declarations on all function signatures.
- **TypeScript/React**: Prettier formatting, ESLint. Functional components with hooks. No class components.
- **SQL**: Uppercase keywords, lowercase identifiers, explicit column lists (no `SELECT *` in production code). New tables need `FORCE ROW LEVEL SECURITY` + a `tenant_isolation` policy on `workspace_id` (§06b).

## Commit convention

Conventional commits:
- `feat:` new feature
- `fix:` bug fix
- `refactor:` no behavior change
- `docs:` documentation only
- `test:` test only
- `chore:` tooling, deps, config

Reference the architecture doc section when the commit relates to a specific
part of the spec: `feat(ingestion): implement CRS detection per Section 04b`

## Testing requirements

Every PR should have tests. Golden query tests and hallucination failure tests
are milestone gates — they must pass before a milestone is accepted. The
LLM-dependent ones run only in the nightly `eval-gate.yml` (LLM and embeddings
stubbed), not per PR; the blocking integration set is the allow-list in
`src/fastapi/tests/integration_ci_manifest.txt`. There is no snapshot-test
tier. See `test-engineer` agent for patterns and §07e for what each tier
actually enforces.

## When you're stuck

- **Architecture unclear?** Re-read the relevant section in `georag-architecture.html`.
- **Still unclear?** Ask the user (Kyle, the SME). Do not infer geological decisions.
- **Cross-cutting concern?** Invoke `senior-reviewer` for a checkpoint review.
- **Not sure which agent?** Start in the main session and let Claude Code delegate.
- **Operator-style task?** (secret rotation, PII decryption, APP_KEY
  rotation, test-env gotchas) — check `docs/RUNBOOK.md` first. It's the
  single source of truth for procedures that touch encrypted data,
  shared secrets, or reversible ops state.

===

<laravel-boost-guidelines>
=== foundation rules ===

# Laravel Boost Guidelines

The Laravel Boost guidelines are specifically curated by Laravel maintainers for this application. These guidelines should be followed closely to ensure the best experience when building Laravel applications.

## Foundational Context

This application is a Laravel application and its main Laravel ecosystems package & versions are below. You are an expert with them all. Ensure you abide by these specific packages & versions.

- php - 8.5
- inertiajs/inertia-laravel (INERTIA_LARAVEL) - v3
- laravel/framework (LARAVEL) - v13
- laravel/horizon (HORIZON) - v5
- laravel/octane (OCTANE) - v2
- laravel/prompts (PROMPTS) - v0
- laravel/pulse (PULSE) - v1
- laravel/reverb (REVERB) - v1
- laravel/sanctum (SANCTUM) - v4
- livewire/livewire (LIVEWIRE) - v4
- laravel/boost (BOOST) - v2
- laravel/mcp (MCP) - v1
- laravel/pail (PAIL) - v1
- laravel/pint (PINT) - v1
- phpunit/phpunit (PHPUNIT) - v12
- @inertiajs/react (INERTIA_REACT) - v2
- laravel-echo (ECHO) - v2
- react (REACT) - v19
- tailwindcss (TAILWINDCSS) - v4

## Skills Activation

This project has domain-specific skills available in `**/skills/**`. You MUST activate the relevant skill whenever you work in that domain—don't wait until you're stuck.

## Conventions

- You must follow all existing code conventions used in this application. When creating or editing a file, check sibling files for the correct structure, approach, and naming.
- Use descriptive names for variables and methods. For example, `isRegisteredForDiscounts`, not `discount()`.
- Check for existing components to reuse before writing a new one.

## Verification Scripts

- Do not create verification scripts or tinker when tests cover that functionality and prove they work. Unit and feature tests are more important.

## Application Structure & Architecture

- Stick to existing directory structure; don't create new base folders without approval.
- Do not change the application's dependencies without approval.

## Frontend Bundling

- If the user doesn't see a frontend change reflected in the UI, it could mean they need to run `npm run build`, `npm run dev`, or `composer run dev`. Ask them.

## Documentation Files

- You must only create documentation files if explicitly requested by the user.

## Replies

- Be concise in your explanations - focus on what's important rather than explaining obvious details.

=== boost rules ===

# Laravel Boost

## Tools

- Laravel Boost is an MCP server with tools designed specifically for this application. Prefer Boost tools over manual alternatives like shell commands or file reads.
- Use `database-query` to run read-only queries against the database instead of writing raw SQL in tinker.
- Use `database-schema` to inspect table structure before writing migrations or models.
- Use `get-absolute-url` to resolve the correct scheme, domain, and port for project URLs. Always use this before sharing a URL with the user.
- Use `browser-logs` to read browser logs, errors, and exceptions. Only recent logs are useful, ignore old entries.

## Searching Documentation (IMPORTANT)

- Always use `search-docs` before making code changes. Do not skip this step. It returns version-specific docs based on installed packages automatically.
- Pass a `packages` array to scope results when you know which packages are relevant.
- Use multiple broad, topic-based queries: `['rate limiting', 'routing rate limiting', 'routing']`. Expect the most relevant results first.
- Do not add package names to queries because package info is already shared. Use `test resource table`, not `filament 4 test resource table`.

### Search Syntax

1. Use words for auto-stemmed AND logic: `rate limit` matches both "rate" AND "limit".
2. Use `"quoted phrases"` for exact position matching: `"infinite scroll"` requires adjacent words in order.
3. Combine words and phrases for mixed queries: `middleware "rate limit"`.
4. Use multiple queries for OR logic: `queries=["authentication", "middleware"]`.

## Artisan

- Run Artisan commands directly via the command line (e.g., `php artisan route:list`). Use `php artisan list` to discover available commands and `php artisan [command] --help` to check parameters.
- Inspect routes with `php artisan route:list`. Filter with: `--method=GET`, `--name=users`, `--path=api`, `--except-vendor`, `--only-vendor`.
- Read configuration values using dot notation: `php artisan config:show app.name`, `php artisan config:show database.default`. Or read config files directly from the `config/` directory.
- To check environment variables, read the `.env` file directly.

## Tinker

- Execute PHP in app context for debugging and testing code. Do not create models without user approval, prefer tests with factories instead. Prefer existing Artisan commands over custom tinker code.
- Always use single quotes to prevent shell expansion: `php artisan tinker --execute 'Your::code();'`
  - Double quotes for PHP strings inside: `php artisan tinker --execute 'User::where("active", true)->count();'`

=== php rules ===

# PHP

- Always use curly braces for control structures, even for single-line bodies.
- Use PHP 8 constructor property promotion: `public function __construct(public GitHub $github) { }`. Do not leave empty zero-parameter `__construct()` methods unless the constructor is private.
- Use explicit return type declarations and type hints for all method parameters: `function isAccessible(User $user, ?string $path = null): bool`
- Use TitleCase for Enum keys: `FavoritePerson`, `BestLake`, `Monthly`.
- Prefer PHPDoc blocks over inline comments. Only add inline comments for exceptionally complex logic.
- Use array shape type definitions in PHPDoc blocks.

=== deployments rules ===

# Deployment

- Laravel can be deployed using [Laravel Cloud](https://cloud.laravel.com/), which is the fastest way to deploy and scale production Laravel applications.

=== tests rules ===

# Test Enforcement

- Every change must be programmatically tested. Write a new test or update an existing test, then run the affected tests to make sure they pass.
- Run the minimum number of tests needed to ensure code quality and speed. Use `php artisan test --compact` with a specific filename or filter.

=== inertia-laravel/core rules ===

# Inertia

- Inertia creates fully client-side rendered SPAs without modern SPA complexity, leveraging existing server-side patterns.
- Components live in `resources/js/Pages` (unless specified in `vite.config.js`). Use `Inertia::render()` for server-side routing instead of Blade views.
- ALWAYS use `search-docs` tool for version-specific Inertia documentation and updated code examples.
- IMPORTANT: Activate `inertia-react-development` when working with Inertia client-side patterns.

# Inertia v3

- Use all Inertia features from v1, v2, and v3. Check the documentation before making changes to ensure the correct approach.
- New v3 features: standalone HTTP requests (`useHttp` hook), optimistic updates with automatic rollback, layout props (`useLayoutProps` hook), instant visits, simplified SSR via `@inertiajs/vite` plugin, custom exception handling for error pages.
- Carried over from v2: deferred props, infinite scroll, merging props, polling, prefetching, once props, flash data.
- When using deferred props, add an empty state with a pulsing or animated skeleton.
- Axios has been removed. Use the built-in XHR client with interceptors, or install Axios separately if needed.
- `Inertia::lazy()` / `LazyProp` has been removed. Use `Inertia::optional()` instead.
- Prop types (`Inertia::optional()`, `Inertia::defer()`, `Inertia::merge()`) work inside nested arrays with dot-notation paths.
- SSR works automatically in Vite dev mode with `@inertiajs/vite` - no separate Node.js server needed during development.
- Event renames: `invalid` is now `httpException`, `exception` is now `networkError`.
- `router.cancel()` replaced by `router.cancelAll()`.
- The `future` configuration namespace has been removed - all v2 future options are now always enabled.

=== laravel/core rules ===

# Do Things the Laravel Way

- Use `php artisan make:` commands to create new files (i.e. migrations, controllers, models, etc.). You can list available Artisan commands using `php artisan list` and check their parameters with `php artisan [command] --help`.
- If you're creating a generic PHP class, use `php artisan make:class`.
- Pass `--no-interaction` to all Artisan commands to ensure they work without user input. You should also pass the correct `--options` to ensure correct behavior.

### Model Creation

- When creating new models, create useful factories and seeders for them too. Ask the user if they need any other things, using `php artisan make:model --help` to check the available options.

## APIs & Eloquent Resources

- For APIs, default to using Eloquent API Resources and API versioning unless existing API routes do not, then you should follow existing application convention.

## URL Generation

- When generating links to other pages, prefer named routes and the `route()` function.

## Testing

- When creating models for tests, use the factories for the models. Check if the factory has custom states that can be used before manually setting up the model.
- Faker: Use methods such as `$this->faker->word()` or `fake()->randomDigit()`. Follow existing conventions whether to use `$this->faker` or `fake()`.
- When creating tests, make use of `php artisan make:test [options] {name}` to create a feature test, and pass `--unit` to create a unit test. Most tests should be feature tests.

## Vite Error

- If you receive an "Illuminate\Foundation\ViteException: Unable to locate file in Vite manifest" error, you can run `npm run build` or ask the user to run `npm run dev` or `composer run dev`.

=== octane/core rules ===

# Octane

- Octane boots the application once and reuses it across requests, so singletons persist between requests.
- The Laravel container's `scoped` method may be used as a safe alternative to `singleton`.
- Never inject the container, request, or config repository into a singleton's constructor; use a resolver closure or `bind()` instead:

```php
// Bad
$this->app->singleton(Service::class, fn (Application $app) => new Service($app['request']));

// Good
$this->app->singleton(Service::class, fn () => new Service(fn () => request()));
```

- Never append to static properties, as they accumulate in memory across requests.

=== pint/core rules ===

# Laravel Pint Code Formatter

- If you have modified any PHP files, you must run `vendor/bin/pint --dirty --format agent` before finalizing changes to ensure your code matches the project's expected style.
- Do not run `vendor/bin/pint --test --format agent`, simply run `vendor/bin/pint --format agent` to fix any formatting issues.

=== phpunit/core rules ===

# PHPUnit

- This application uses PHPUnit for testing. All tests must be written as PHPUnit classes. Use `php artisan make:test --phpunit {name}` to create a new test.
- If you see a test using "Pest", convert it to PHPUnit.
- Every time a test has been updated, run that singular test.
- When the tests relating to your feature are passing, ask the user if they would like to also run the entire test suite to make sure everything is still passing.
- Tests should cover all happy paths, failure paths, and edge cases.
- You must not remove any tests or test files from the tests directory without approval. These are not temporary or helper files; these are core to the application.

## Running Tests

- Run the minimal number of tests, using an appropriate filter, before finalizing.
- To run all tests: `php artisan test --compact`.
- To run all tests in a file: `php artisan test --compact tests/Feature/ExampleTest.php`.
- To filter on a particular test name: `php artisan test --compact --filter=testName` (recommended after making a change to a related file).

=== inertia-react/core rules ===

# Inertia + React

- IMPORTANT: Activate `inertia-react-development` when working with Inertia React client-side patterns.

</laravel-boost-guidelines>
