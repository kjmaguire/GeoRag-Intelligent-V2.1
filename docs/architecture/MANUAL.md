# GeoRAG Architecture Manual

> A "car repair manual" companion to `georag-architecture.html`. Every chapter
> cites file paths and line numbers; if the code and the manual disagree, the
> code wins — open an issue and fix the manual.

Read [Chapter 00](manual/00-overview.md) first if you're new — it explains the
layout and glossary the other chapters rely on. The
[Appendices](#appendices) carry the implementation-grade contracts; the
[Notes index](notes/INDEX.md) replaces the old per-developer memory paths.

## Status legend

Every chapter, table, workflow, page, and agent below carries a status
marker. The exhaustive matrix is in
[Ch 14 — Status Matrix](manual/14-status-matrix.md).

- **Live** — production-ready on main.
- **Live (dev-only)** — wired and used in dev; not yet hardened for prod.
- **Partial** — shipping with gaps documented in-chapter.
- **Planned** — designed; not yet implemented.
- **Deprecated** — replaced; retained for rollback.
- **Stub** — exists but no-ops.
- **Experimental** — behind a feature flag, opt-in.

## Reading order

| # | Chapter | Status | What it covers |
|--:|---------|--------|----------------|
| 00 | [Overview + glossary](manual/00-overview.md) | Live | What GeoRAG is, profiles, request shape, hard rules, glossary |
| 01 | [Services catalog](manual/01-services.md) | Live | Every container with image/port/role/healthcheck and file:line |
| 02 | [Data stores](manual/02-data-stores.md) | Live (security items tracked) | PG+extensions, Neo4j, Qdrant, Redis, SeaweedFS, ClickHouse, Martin DB path, role posture |
| 03 | [Schemas and tables](manual/03-schemas.md) | Live | Every schema, every architecturally important table, every trigger |
| 04 | [Ingestion flow](manual/04-ingestion-flow.md) | Live | Upload → bronze → silver → gold → graph → embeddings; outbox; ingest workflows |
| 05 | [PDF stack §04p](manual/05-pdf-stack.md) | Live | 7-stage in-process PDF parser, env knobs |
| 06 | [Retrieval + agents](manual/06-retrieval-and-agents.md) | Live (persist_node partial) | §04j LangGraph, intent classifier, dispatcher, retrieval tools, hallucination layers, OIUR |
| 07 | [Orchestration](manual/07-orchestration.md) | Live | Horizon / Hatchet / Dagster / Kestra ownership + cron |
| 08 | [LLM and ML models](manual/08-llm-and-ml.md) | Live (vLLM dev-only) | vLLM, bge-small, bge-reranker, SPLADE++, rule-based classifiers |
| 09 | [Martin + MapLibre](manual/09-martin-and-maplibre.md) | Live (martin_ro planned) | Tile server config, every MVT function, table sources, MVT-null trap |
| 10 | [Frontend](manual/10-frontend.md) | Live | Inertia + React pages, Reverb channels, Vite/Octane reload gotcha |
| 11 | [Tenancy + RLS](manual/11-tenancy-and-rls.md) | Live (georag split planned) | Workspaces, GUC, role split, policy patterns, coverage migrations, service-to-service auth |
| 12 | [Observability](manual/12-observability.md) | Live | Prometheus + exporters, Loki + Promtail, OTel + Tempo, Langfuse, Pulse, audit ledger, `silver.query_traces` |
| 13 | [Data Hierarchy](manual/13-data-hierarchy.md) | Partial | Geologist-facing classification: Reports / Geology / Geochemistry / Geophysics + multi-category model |
| 14 | [Status Matrix](manual/14-status-matrix.md) | Live | Per-component status — single page of truth |
| 15 | [Design Docs Index](manual/15-design-docs-index.md) | Live | Index of the **14** planning artifacts in `docs/architecture/` with promotion paths (7 original + 7 added in Pass 4 ADR-0009/0010 era) |
| 16 | [Algorithmic Spines + Canonical Chunked Corpus](manual/16-algorithmic-spines.md) | Live | ADR-0009 Spines A (context-prep) + B (repair loop) + ADR-0010 `silver.document_passages` canonical + new Qdrant `georag_chunks`. New silver tables (query_traces, data_quality_flags, document_versions, entity_aliases/gaps). New Hatchet workflow `repair_shadow_aggregate`. |
| 17 | [Strategic Context (Master Plan + Phase Timeline)](manual/17-strategic-context.md) | Live | **The "why"** — master plan §§5–12 scope proposals with one-paragraph intent each + completion %; phase 0 + doc-phase 100–105 handoffs; cumulative master-plan completion table; reading order for new contributors; the 9 hard rules as strategic non-negotiables. |
| **17b** | [**Master Plan Deep Dive (§§5–12)**](manual/17b-master-plan-deep-dive.md) | **Live (new)** | Per-§ deep summary of all 8 scope proposals: **goal** (verbatim SME pitch) + **deliverables** (master-plan-numbered list) + **done test** (acceptance criterion) + **status as of Pass 4** + **where to look** (manual cross-refs) + a cross-section showing exactly where each section's code lives in the repo. The "why what got built, got built" reference. |
| **18** | [**Model Stack Evolution + the 2026-06 Audit Wave**](manual/18-model-stack-evolution.md) | **Live (new)** | The recent-changes chapter. **Qwen3 model swap** (embedding → Qwen3-Embedding-0.6B 1024-dim, reranker → Qwen3-Reranker-0.6B) with the **config/runtime split** + the **🔴 live Dagster 384-dim re-index hazard**. ADRs 0011–0017 roll-up. §04p OCR/VL upgrades (PaddleOCR 3.7, Tesseract 5.5.2 from source, Qwen3-VL-8B gated). ADR-0012 structured-to-NL retrieval corpus. Contextual retrieval (`contextualized_content`). Answer-quality LLM-as-judge scoring. **Project lifecycle states** (CC-03 Item 8 landed: active/hibernated/archived/past_due). New tenancy/observability schema. RLS sentinel third+fourth sweeps. |

## Appendices

| ID | Appendix | Status | Purpose |
|---|---|---|---|
| A | [Medallion Contract](appendix/A-medallion-contract.md) | Draft | Bronze/Silver/Gold table contract, lineage fields, QA gates, fan-out, deletion semantics, test envelope |
| B | [Event Payloads](appendix/B-event-payloads.md) | Draft | Exact JSON shapes for every Reverb / SSE / Hatchet / Dagster / Kestra / outbox event |
| C | [Security Posture](appendix/C-security-posture.md) | Draft (open items tracked) | Trust boundaries, threat model, tenant isolation, prompt injection, tool abuse, LLM egress, rotation, RPO/RTO |
| D | [API Contract](appendix/D-api-contract.md) | Draft | Laravel + FastAPI endpoint inventory; OpenAPI generator design |
| E | [Ingestion Format Matrix](appendix/E-ingestion-format-matrix.md) | Draft | 19-row per-format end-to-end contract |
| F | [Data Dictionary + ERD](appendix/F-data-dictionary.md) | Draft | Per-table template + ERD groupings + generator design + CI drift guard |
| G | [RAG Retrieval Contract](appendix/G-rag-retrieval-contract.md) | Draft | Chunking, embedding, Qdrant payload, fusion, reranker, citation binding, numeric verification, confidence formula; ADR-0010 `georag_chunks` callout |
| H | [Knowledge Graph Schema](appendix/H-knowledge-graph-schema.md) | Draft | Neo4j node labels + relationships + workspace fence + upsert / conflict / deletion rules |
| I | [Frontend Workflow Specs](appendix/I-frontend-specs.md) | Draft | Per-page acceptance specs for the 9 highest-value pages + cross-cutting rules |
| J | [Testing + Evaluation Matrix](appendix/J-testing-matrix.md) | Draft | Per-feature pass/fail + golden RAG suite + CI orchestration |
| K | [Deployment + Operations](appendix/K-deployment-operations.md) | Draft | Install / backup / restore / scaling / sizing / playbooks / RPO-RTO (**Docker Compose path**) |
| L | [Kubernetes, Helm Chart, Air-Gap](appendix/L-kubernetes-and-airgap.md) | Draft | Helm chart at `charts/georag/`, raw manifests at `kubernetes/manifests/`, single-command air-gap installer at `airgap/install.sh`, 37 ops runbooks index |
| **M** | [**Agents & ML Catalog**](appendix/M-agents-and-ml-catalog.md) | **Draft** | All ~41 Pydantic-AI agents (Phase 0/5/6/7/8/9/10) enumerated by file + job, 6 classifiers, 5 ML training pipelines (bge-reranker LoRA bake — dataset / trainer / eval / serving / locked-decision regression test; source-trust + target-scoring trainers; SPLADE++ batch encoder; continuous learning loop), `@georag_agent` runtime contract, full model registry |
| **N** | [**Agentic & Retrieval Module Catalog**](appendix/N-agentic-and-retrieval-catalog.md) | **Draft** | The "**3 LangGraphs, not 1**" surprise: chat-path `agentic_retrieval` + targeting `target_recommendation` + ops `llm_incident_diagnosis`. All 50 modules under `app/agent/` enumerated (Spine A/B / hallucination / tools / classifiers / answer assembly / lineage / pricing / log_safe). 19 prompt templates indexed by family. 13 agent-shaped services subdirs. 2 dispatchers. **Total agent-shaped surfaces across M + N: ~150.** |
| **O** | [**Engineering Surface Inventory**](appendix/O-engineering-surface-inventory.md) | **Draft (new)** | The everything-else inventory: **101 Laravel controllers**, **38 PHP services**, **34 Eloquent models**, **20 config files**, **129 React components**, **234 FastAPI tests** + **68 Dagster tests** + **12 pgTAP files**, **7 GitHub Actions workflows**, **7 Dockerfiles**, **9 Claude Code subagents**, **36 Claude skills**, build/dev tooling, OpenSpec workflow, all root-level docs (master plan §5-12 scopes, phase handoffs, audit/review artifacts). Closes the meta-architecture + long-tail code surface gaps. |
| Z | [Roadmap](appendix/Z-roadmap.md) | Live | Closure status — what shipped and what remains |

## Auxiliary references

- [Data dictionary skeleton](data_dict/INDEX.md) — per-schema stubs + full-column `_core_tables.md`
- [Notes index](notes/INDEX.md) — incident + decision precis, one entry per legacy memory note

## Closure summary

Four review passes shipped:

- **Pass 1.** All 14 base chapters written.
- **Pass 2.** Appendices A–K + Z written; status matrix; notes index;
  direct inconsistencies fixed.
- **Pass 3.** Design-doc validation; 4 ADRs added; new Appendix L
  (Kubernetes, Helm, air-gap) integrating the ops/runbooks; 14
  data_dict files + `_core_tables.md`.
- **Pass 4 (2026-05-29).** ADR-0009 + ADR-0010 integrated. New Ch 16
  (Algorithmic Spines + Canonical Chunked Corpus). 14 new migrations
  documented (`silver.query_traces`, `silver.data_quality_flags`,
  `silver.document_versions`, `silver.entity_aliases`,
  `silver.entity_gaps`, column extensions on `silver.reports` and
  `silver.document_passages` and `silver.query_traces`). 7 new design
  docs indexed. 5 new agent modules + 1 new Hatchet workflow + 10+
  new Dagster assets. Qdrant `georag_chunks` collection. Z roadmap:
  9 of 26 items done.
- **Pass 4b — Agents & ML (on-demand 2026-05-29).** Added new
  [Appendix M](appendix/M-agents-and-ml-catalog.md): enumerates every
  one of the ~41 Pydantic-AI agents (Phase 0 / 5 / 6 / 7 / 8 / 9 / 10)
  with file + job + writes, all 6 classifiers, the full bge-reranker
  **LoRA fine-tune pipeline** (dataset / trainer / eval / production
  serving / canonical-corpus contract test), source-trust + target-
  scoring training workflows, SPLADE++ batch encoder, continuous
  learning loop, `@georag_agent` runtime contract (idempotency,
  circuit breaker, audit ledger emit order), model registry. Closes
  the "have you got every agent + LoRA?" gap.
- **Pass 5 — 2026-06 audit wave + model swap (on-demand, Opus 4.8).**
  Caught up the manual to the state of the repo as of 2026-06-26 after
  a real wave of work landed. **7 new ADRs (0011–0017)** integrated.
  New **[Ch 18 — Model Stack Evolution](manual/18-model-stack-evolution.md)**.
  Corrected two pieces of now-wrong documentation: **Ch 02** (pgvector
  is NOT installed per ADR-0013 — was listed as optional) and **Ch 08**
  (embedding + reranker swapped to **Qwen3-Embedding-0.6B 1024-dim** +
  **Qwen3-Reranker-0.6B** on 2026-06-03 — the chapter still said
  bge-small/bge-reranker). Documented the **config/runtime split**
  (production env-driven vs stale code defaults) and the **🔴 live
  Dagster 384-dim re-index hazard**. Added §04p OCR/VL upgrades
  (PaddleOCR 3.7, Tesseract 5.5.2 from source, Qwen3-VL-8B gated), the
  ADR-0012 structured-to-NL retrieval corpus (4 new Dagster assets),
  contextual retrieval, answer-quality LLM-as-judge scoring,
  **project lifecycle states** (CC-03 Item 8 finally landed), new
  tenancy/observability tables (`silver.tenant_isolation_audit`,
  `silver.archive_ingest_runs`), and the third+fourth RLS sentinel
  sweeps. Updated Ch 14, Appendix M model registry. **Appendix F
  generator actually shipped** (was Draft, now Live). Verified the
  main synthesizer LLM is unchanged (Qwen3-14B-AWQ).
- **Pass 4f — Master plan deep dive (on-demand 2026-05-29).** Added
  **Ch 17b — Master Plan Deep Dive (§§5–12)** with per-section deep
  summaries: §5 spatial pipeline + drillhole visuals (~90 %), §6
  PublicGeo + MapLibre layer packs (Tier 1 live), §7 Reporting +
  dashboards (Phase 7 agents live; 22-dashboard suite + report builder
  in flight), §8 Target Recommendation (live — TRG LangGraph + 11
  agents), §9 Geological Reasoning + Decision Intelligence
  (engineering done; SME ontology pass pending), §10 Eval + Cockpit
  (partial — eval live, cockpit + Phase 10 agents live; golden YAML
  loader owed), §11 DR + deployment + perf (autonomous-safe slice
  done; signed-bundle GPG chain in flight), §12 XGBoost + source
  trust (scaffolding 85 %; awaits `target_outcomes` data). Each §
  has: goal verbatim, deliverables list, done test, current status,
  manual cross-refs, code locations. Closes the "what does the master
  plan actually want?" gap.
- **Pass 4e — Strategic context + skills + AI SDK + Pydantic schemas
  + top controllers (on-demand 2026-05-29).** Added **Ch 17 —
  Strategic Context** with master plan §§5–12 scope proposals
  summarised (one-paragraph intent + status per section), Phase 0 +
  doc-phase 100–105 handoff packets, cumulative completion table,
  new-contributor reading order. Extended [Appendix O](appendix/O-engineering-surface-inventory.md)
  with **§13 top 20+ Laravel controllers** (Foundry / Internal /
  Tiles / Admin route surface), **§14 Laravel AI SDK** detail
  (`config/ai.php` provider routing — distinct from FastAPI
  `LLM_BACKEND`), **§15 FastAPI Pydantic models** (13 canonical
  inter-process data shapes with file + classes + consumer), **§16
  Claude Code skills index** (36 skills grouped by GeoRAG-specific /
  workflow / framework, including the 8 GeoRAG-specific ones like
  `agent-wrapper`, `audit-emit`, `hatchet-workflow`, `phase-verify`).
  Captures the "why" + the meta-tooling layer.
- **Pass 4d — Full engineering surface audit (on-demand 2026-05-29).**
  Added new [Appendix O](appendix/O-engineering-surface-inventory.md)
  closing the long-tail gaps: **101 Laravel controllers + 38 services
  + 34 Eloquent models + 20 configs + 11 events**, **129 React
  components + 13 hooks + 14 lib utilities + 5 layouts**, **234
  FastAPI tests + 68 Dagster tests + 12 pgTAP files**, **7 GitHub
  Actions workflows**, **7 Dockerfiles**, **9 Claude Code subagents +
  36 skills** (the meta-architecture critical for any Claude Code user
  on this repo — `senior-reviewer`, `backend-laravel`, `backend-fastapi`,
  `data-engineer`, `graph-engineer`, `frontend-engineer`, `devops-
  engineer`, `test-engineer`, `boilerplate-writer` + the
  `georag-*` skills + OpenSpec workflow). Indexed all root-level docs
  (master plan §5-12 scopes, phase 0/100-105 handoffs, audit/review
  artifacts, contracts/specs, operational docs). Closes the
  "anything else missing?" gap.
- **Pass 4c — Full agentic catalog (on-demand 2026-05-29).** Added new
  [Appendix N](appendix/N-agentic-and-retrieval-catalog.md): the
  companion to M for **everything else with agent-shaped logic**.
  Surfaced that there are **3 LangGraphs, not 1** —
  `agentic_retrieval` (chat), `target_recommendation` (Phase 8
  targeting), `llm_incident_diagnosis` (Phase 0 ops). Enumerated all
  **50 modules** in `src/fastapi/app/agent/` by group (LangGraph node
  hooks / retrieval tools / classifiers / Spine A / Spine B / answer
  + validation / eval). Indexed **19 prompt templates** by family.
  Cataloged **13 agent-shaped services subdirs** (target_recommendation,
  decision_intelligence, source_trust, target_scoring_ml, shadow_diff,
  tool_gateway, geological_reasoning, report_builder, visualizations,
  support_cockpit, eval, targeting, llm_incident_diagnosis). 2
  dispatchers (kestra, pagerduty). **Total agent-shaped surfaces
  across M + N: ~150.**

Outstanding work is implementation. The [26-item execution priority](appendix/Z-roadmap.md#execution-priority--implementation-side-work-that-remains)
is the canonical TODO; 9 items done as of Pass 4.

## Source-of-truth pointers

| Topic | Primary source |
|---|---|
| Architecture decisions | [docs/adr/](../adr/) — **17 ADRs**. 0001-0010 (foundational; see [Ch 16](manual/16-algorithmic-spines.md)) + **0011** reranker domain adaptation (Proposed/dormant), **0012** structured-to-NL summary corpus (Proposed), **0013** no-pgvector (Accepted), **0014** two-phase workspace scoping (Proposed), **0015** Qwen3-VL-8B migration (Proposed/gated), **0016** PaddleOCR 3.x migration (Accepted Ph1), **0017** Tesseract 5.5 from source (Accepted). 0011-0017 rolled up in [Ch 18](manual/18-model-stack-evolution.md). |
| Long-form spec | [`georag-architecture.html`](../../georag-architecture.html) |
| Operator procedures | [`docs/RUNBOOK.md`](../RUNBOOK.md) + 37 runbooks under [`ops/runbooks/`](../../ops/runbooks/) (indexed in Appendix L §9) |
| On-call quick-ref | [`docs/SERVICE_INVENTORY.md`](../SERVICE_INVENTORY.md) |
| Hash-chain audit recipe | [`docs/audit_ledger_hash_recipe.md`](../audit_ledger_hash_recipe.md) |
| MVT null convention | [`docs/mvt-nullable-numeric-convention.md`](../mvt-nullable-numeric-convention.md) |
| Phase-by-phase kickoffs | `docs/*kickoff*.md`, `docs/*handoff*.md` |
| Phase acceptance tests | run the `phase-verify` skill |
