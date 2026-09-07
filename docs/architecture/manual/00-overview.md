# Chapter 00 — System Overview

> **What this is.** A "car repair manual" for GeoRAG. Every chapter cites file
> paths so a new engineer can open the source and verify any claim. If the
> code and the manual disagree, the code wins — open an issue and fix the
> manual.
>
> **Reconciled 2026-09-07 against `main`.** This chapter describes the stack
> as it exists today. The manual was first written against the pre-July-2026
> stack, and several chapters still describe services that were removed
> between 2026-07-28 and 2026-08-23 (Neo4j, Dagster, Kestra, Caddy, the
> self-hosted vLLM server, the Prometheus/Grafana/Loki/Tempo stack, the
> backup agent). §7 below says which chapters have been brought up to date;
> the rest carry a dated notice at the top until they are rewritten.
>
> **Authoritative companion docs.** `georag-architecture.html` (the long-form
> spec, with dated *As built* notes wherever code and design differ),
> [CLAUDE.md](../../../CLAUDE.md) (the hard rules and the technology
> snapshot, read on every agent turn), `docs/adr/*` (decision records),
> `docs/RUNBOOK.md` (operator procedures that touch encrypted data or shared
> secrets), `ops/runbooks/azure-oncall.md` (production on-call) and
> `deploy/azure/README.md` (Container Apps topology). `docs/SERVICE_INVENTORY.md`
> was last refreshed 2026-05-14 and still lists 24 containers; treat it as
> history until it is redone.

## 1. What GeoRAG is

GeoRAG is a multi-tenant geological intelligence platform. Junior-mining and
exploration companies upload decades of fragmented exploration data — drill
logs, NI 43‑101 reports, geophysics surveys, GIS layers, geochemistry
spreadsheets — and ask questions in natural language. The system returns
cited answers, interactive map and section visualisations, and exports to
industry modelling tools.

The repo is a single monorepo containing:

- A **Laravel 13** application on **Octane/Swoole**
  ([composer.json](../../../composer.json), [CLAUDE.md §Technology snapshot](../../../CLAUDE.md))
  for everything user-facing: authentication (Sanctum), project CRUD,
  uploads, broadcast of WebSocket events (Reverb), and the Inertia/React
  pages. It runs as three processes — `laravel-octane`, `laravel-horizon`
  (two supervisors, `default` + `llm`) and `laravel-reverb`.
- A **FastAPI** Python 3.13 domain service
  ([src/fastapi/](../../../src/fastapi/), floor `fastapi>=0.136` in
  [pyproject.toml](../../../src/fastapi/pyproject.toml)) for the RAG
  pipeline, all asynchronous database I/O, the LangGraph agentic retrieval
  graph, hallucination prevention, citation enforcement, and SSE streaming
  back to Laravel.
- Three **model sidecars** built from the same FastAPI image
  ([docker-compose.yml](../../../docker-compose.yml) `reranker`, `embedding`,
  `sparse`): Qwen3-Reranker-0.6B on the one GPU, Qwen3-Embedding-0.6B and
  SPLADE++ on CPU. In production the dense embedder and reranker are Azure
  AI Foundry (Cohere Embed v4 / Rerank v4, [ADR-0021](../../adr/0021-foundry-embed-rerank-replace-self-hosted-models.md));
  SPLADE++ has no Foundry equivalent and stays self-hosted either way.
- A **Hatchet** workflow engine (`hatchet-lite`) with one merged Python
  worker (`hatchet-worker`, `WORKER_POOL=all`,
  [worker.py](../../../src/fastapi/app/hatchet_workflows/worker.py)) for
  durable file ingestion, every scheduled cron, outbox dispatch, embedding,
  audit verification and report generation — 51 registered workflows,
  inventoried in the architecture doc §07b.
- The **`georag_geoparsers`** package
  ([src/georag_geoparsers/](../../../src/georag_geoparsers/)) — the format
  parsers (Polars, GDAL via pyogrio/GeoPandas/rasterio, pyproj, lasio,
  openpyxl/xlrd, ezdxf, mdbtools, rapidfuzz) and the in-process §04p PDF
  stack (Ch 05).
- A **React 19 + Inertia 3** frontend ([resources/js/Pages/](../../../resources/js/Pages/),
  [package.json](../../../package.json)) with shadcn/ui, Tailwind v4,
  MapLibre GL 5 and Plotly. React Flow was removed 2026-08-28; there is no
  graph view.
- A row of **data stores**: PostgreSQL 18 + PostGIS 3.6 behind PgBouncer
  (transaction mode), Qdrant 1.17 for dense + sparse vector retrieval, Redis
  8 for sessions / queues / caches / rate limits, and an S3-compatible
  object store for bronze files — SeaweedFS in compose, Azure Blob in
  production, one `STORAGE_BACKEND` switch
  ([ADR-0001](../../adr/), [ADR-0020](../../adr/0020-azure-blob-replaces-seaweedfs-in-production.md)).
- **Azure AI Foundry** for the LLM (Cohere Command A+ via the OpenAI v1
  chat-completions surface; Anthropic Claude wired as optional fallback) and
  for scanned-page OCR (Cohere Parse v5,
  [ADR-0019](../../adr/)). There is no LLM container in the stack;
  `LLM_BACKEND=vllm` remains a supported value for operators pointing at
  their own OpenAI-compatible endpoint.
- A **Martin 1.11** tile server ([docker/martin/martin.yaml](../../../docker/martin/martin.yaml))
  that serves PostGIS function/table sources as Mapbox Vector Tiles to
  MapLibre.

Things you may find referenced in older chapters, comments or runbooks that
**do not exist**: Neo4j (knowledge graph, removed 2026-07-28 — hard rule 9),
Dagster (retired 2026-07-28, tree deleted 2026-08-28), Kestra, Caddy, Ofelia
and the backup agent, a self-hosted vLLM/Ollama server, Prometheus,
Alertmanager, Grafana, Loki, Promtail, Tempo, the OpenTelemetry collector,
RAGFlow, Docling, PaddleOCR, PyMuPDF, Azure Document Intelligence, DuckDB,
segyio and obspy. The `docker-compose.yml` header and the tombstone comments
inside it record when and why each went.

## 2. Dev topology — profiles and how the stack starts

One `docker-compose.yml`, 16 services, profile-driven. What actually
carries a `profiles:` key today (the header comment's profile guide was
corrected to match on 2026-09-07; [Ch 01](01-services.md) has every
service in detail):

| Profile | Services |
|---|---|
| *(none — always on)* | `postgresql`, `pgbouncer`, `redis`, `martin` |
| `dev-light` | `laravel-octane`, `laravel-horizon`, `laravel-reverb` |
| `dev-data` | `fastapi`, `reranker`, `embedding`, `sparse`, `qdrant`, `minio` (SeaweedFS), `minio-init`, `hatchet-lite`, `hatchet-worker` |
| `dev-full` | Everything above |

So `dev-light` alone brings up Laravel with no domain service behind it;
a working day needs `--profile dev-light --profile dev-data`. Note that
FastAPI sits in `dev-data`, not `dev-light`.

Images, from the compose file: `georag/postgres:18-ext` (built on
`postgis/postgis:18-3.6-alpine`, [docker/postgresql/Dockerfile](../../../docker/postgresql/Dockerfile)),
`edoburu/pgbouncer:v1.25.1-p0`, `redis:8.6.4-alpine`, `qdrant/qdrant:v1.17.1`,
`chrislusf/seaweedfs:4.35` (the service is still named `minio` for
compatibility — see ADR-0001), `minio/mc` for bucket provisioning,
`ghcr.io/hatchet-dev/hatchet/hatchet-lite:v0.86.12`,
`ghcr.io/maplibre/martin:1.11.0`, and the two local builds `georag/laravel`
and `georag/fastapi` ([docker/laravel.Dockerfile](../../../docker/laravel.Dockerfile),
[docker/fastapi.Dockerfile](../../../docker/fastapi.Dockerfile)).

## 3. Production topology — Azure Container Apps

Production is Azure Container Apps in resource group `georag`, no GPU
(architecture doc §07-azure, [deploy/azure/README.md](../../../deploy/azure/README.md)):

- Nine apps: `laravel-octane-cc`, `laravel-horizon-cc`, `laravel-reverb-cc`,
  `fastapi-cc`, `hatchet-cc`, `hatchet-worker-cc`, `qdrant-cc`, `redis-cc`,
  `martin-cc`.
- Azure Database for PostgreSQL Flexible Server `georag-pg-cc` (PgBouncer is
  compose-only), Azure Blob storage account `georagblobcc`, Azure AI Foundry
  `georag-foundry-cc` for LLM, embeddings, reranking and OCR.
- Two Container Apps Jobs, `shutdown-scheduler-cc` / `startup-scheduler-cc`,
  stop and start the stack nightly; their inline bodies are generated from
  [deploy/azure/containerapps/scripts/](../../../deploy/azure/containerapps/scripts/)
  and checked by `scripts/check_scheduler_job_parity.py`.
- CD ([.github/workflows/cd.yml](../../../.github/workflows/cd.yml)) builds
  the two images and runs `laravel-migrate-job`, nothing else. Everything
  under `deploy/azure/` is applied by hand.
- Monitoring is Azure Monitor + Log Analytics with a single email receiver
  (`georag-alerts-ag`). There are no latency alerts and no paging.

The on-prem / air-gapped target is the Helm chart at
[charts/georag/](../../../charts/georag/).

## 4. Request shape, end‑to‑end (dev ports)

```
┌──────────┐   HTTP(S)   ┌──────────────────────┐
│ Browser  │ ──────────▶ │ laravel-octane :80   │  ← Inertia HTML + JSON, /api/* (Sanctum)
│ React    │             │ (Swoole, 4 workers)  │
│ MapLibre │ ◀─ WS ───┐  └──────┬───────────────┘
└──────────┘          │         │ POST /internal/queries  (X-Service-Key: FASTAPI_SERVICE_KEY)
                      │         ▼
              ┌───────┴──────┐  SSE  status / bind / delta / citation / completed | failed
              │laravel-reverb│ ◀── re-broadcast as QueryStreamEvent on query.{queryId}
              │ :8085→8080   │
              └──────────────┘
                                ┌───────────────────────────┐
                                │ fastapi :8000             │ ← LangGraph agentic retrieval,
                                │ (uvicorn, 3 workers)      │   §04i guards, persist_node
                                └─┬────┬────┬────┬────┬─────┘
        ┌─────────────┬───────────┘    │    │    │    └──────────── Azure AI Foundry (LLM, OCR;
        ▼             ▼                ▼    ▼    ▼                    embed + rerank in prod)
  ┌──────────┐  ┌──────────┐  ┌───────┐ ┌───────┐ ┌────────────────────┐
  │pgbouncer │  │ qdrant   │  │ redis │ │ minio │ │ reranker/embedding/│ (dev only, each :8000)
  │  :6432   │  │:6333/6334│  │ :6379 │ │ :8333 │ │ sparse sidecars    │
  └────┬─────┘  └──────────┘  └───────┘ └───────┘ └────────────────────┘
       ▼
  ┌─────────────┐          ┌──────────────────┐        ┌───────────────┐
  │ postgresql  │ ◀────────│ martin :3002→3000│        │ hatchet-lite  │ ◀── Hatchet client (gRPC)
  │  :5432      │  direct  │ MVT for MapLibre │        │ :7077 gRPC    │     from fastapi; Laravel
  └─────────────┘          └──────────────────┘        │ :8889 UI      │     dispatches ingestion via
        ▲                                              └──────┬────────┘     POST /internal/v1/shadow/
        └──── hatchet-worker (WORKER_POOL=all, direct PG) ◀───┘              {workflow}/trigger
```

PgBouncer fronts the async application paths (asyncpg with
`statement_cache_size=0`); Martin, the Hatchet worker and migrations
connect to Postgres directly. FastAPI uses Redis db 2, isolated from
Laravel. Every internal hop carries `X-Service-Key`; both sides accept the
previous key during rotation (`ops/runbooks/secret-rotation.md`).

## 5. Glossary

| Term | Meaning |
|------|---------|
| **Bronze** | Immutable raw‑file archive: object-store bucket `bronze` (keys `category/project_id/…`, not workspace-prefixed) + Postgres `bronze.*` tables (`ingest_manifest`, `provenance`, `upload_files`). Tenant isolation for objects rests on `workspace_id` in `bronze.ingest_manifest` plus RLS. |
| **Silver** | Canonical, deduped, validated domain rows in `silver.*` (collars, surveys, lithology, samples, reports, spatial_features, well_log_curves, document_passages, …). |
| **Gold** | Plain tables written by the `promote_silver_to_gold` Hatchet workflow for fast read paths. The only materialised view in the schema is `silver.mv_collar_summary`. |
| **public_geo** | Government‑published reference layers (mines, mineral occurrences, bedrock geology, …). The schema is still named `public_geo`; the rename to `public_geoscience` was locked in design but never applied ([docker/martin/martin.yaml](../../../docker/martin/martin.yaml) header). Pulled by the `public_geoscience_pull` / `public_geo_sync` Hatchet workflows. |
| **OIUR** | "Observation / Interpretation / Uncertainty / Recommendation" — the structured answer envelope of the §04j answer architecture. Gated on `GEO_ANSWER_OIUR_ENABLED`, default **off** ([config.py](../../../src/fastapi/app/config.py)). |
| **Agentic Retrieval v2** | The §04j LangGraph that routes per intent and persists the answer run. Gated on `AGENTIC_RETRIEVAL_V2_ENABLED`, default **on**; the legacy `run_deterministic_rag` dispatches into it. |
| **§04p PDF stack** | The in-process PDF ingest stack in `georag_geoparsers` (replaced RAGFlow per [ADR-0002](../../adr/)). Scanned pages go to Cohere Parse v5 on Foundry ([ADR-0019](../../adr/)), Tesseract as last resort. See Ch 05. |
| **Workspace** | The tenancy unit. Every bronze/silver/gold write carries `workspace_id` (uuid); RLS policies on every table key off `current_setting('app.workspace_id', true)` ([app/Support/SetsWorkspaceRlsContext.php](../../../app/Support/SetsWorkspaceRlsContext.php)). New tables need `FORCE ROW LEVEL SECURITY` + a `tenant_isolation` policy. |
| **Audit ledger** | Append-only hash-chained audit log in `audit.*` ([docs/audit_ledger_hash_recipe.md](../../audit_ledger_hash_recipe.md)), verified by the `audit_ledger_verify` Hatchet cron at 02:00 UTC daily. |
| **Outbox** | `outbox.pending_propagations`, polled by the `outbox_dispatcher` Hatchet workflow to fan a silver write out to Qdrant, the object store and external webhooks. The `neo4j` target is a registered permanent no-op since 2026-07-28 so stale rows dead-letter instead of erroring. |
| **Hatchet pools** | `WORKER_POOL` selects the workflow set the single `hatchet-worker` registers: `ingestion`, `ai`, or `all` (default, and what both compose and production run). There is no separate ingestion or AI worker service. |
| **Reverb channels** | Laravel WebSocket channels ([routes/channels.php](../../../routes/channels.php)): `query.{queryId}` (answer streaming), `workspace.{workspaceId}.activity`, `project.{projectId}.ingestion`, and the `admin.*` channels. |
| **FASTAPI_SERVICE_KEY** | Shared secret on the `X-Service-Key` header between Laravel ↔ FastAPI and the Hatchet worker ↔ Laravel. Previous-key acceptance on both sides makes rotation zero-downtime. |
| **martin_readonly** | The read‑only Postgres role Martin uses in production (`deploy/azure/containerapps/rotate-martin-credential.sh`). In compose Martin connects as `georag_app` directly on 5432. |

## 6. The nine hard rules (from CLAUDE.md)

These are not style preferences. They are tripwires. Source:
[CLAUDE.md §Hard rules](../../../CLAUDE.md) — read the full text there;
this is the index.

1. **No Streamlit.** Frontend is React + Inertia + shadcn/ui + Tailwind.
2. **Async-native drivers only in FastAPI** — `asyncpg`, `redis.asyncio`, async Qdrant client.
3. **Octane-safe Laravel code.** No static state leaks between requests.
4. **Citations mandatory on every RAG response.** Every claim carries a `source_chunk_id` or the typed-output guard in the graph's `validate` node rejects it. `citation_mode` is always `posthoc_span_resolution`.
5. **§04i hallucination prevention.** Four guards run in `orchestrator_validators.py` (typed output, numbers, entities, constraints, plus advisory completeness); the retrieval gate is a reranker score floor. Restoring the missing layers is welcome; weakening the four is not.
6. **Schemas in §04e are contracts.** Don't invent fields; don't change enumerations without SME approval.
7. **No orchestration overlap.** Laravel queues = short user-triggered work; Hatchet = ingestion, every cron, anything needing durable retries. There is no Laravel scheduler.
8. **MapLibre GL, not Mapbox GL.** Licensing matters for on‑prem.
9. **No knowledge graph.** Neo4j was removed 2026-07-28. No graph store, driver or Cypher without a superseding ADR.

## 7. Reconciliation status of this manual

| Chapter | Status on 2026-09-07 |
|---|---|
| 00 Overview | **Reconciled** (this file). |
| 01 Services catalog | **Reconciled 2026-09-07** against the 16 compose services, with the removed-service table, the three overlays, and the stale compose comments listed for the next tidy. |
| 02 Data stores | **Reconciled 2026-09-07**: dev and Azure side by side for Postgres, Qdrant, Redis, object storage, Martin and Hatchet state; roles, namespaces and the as-built backup posture. |
| 03 Schemas | Mostly current; a handful of Neo4j/graph mentions. |
| 04 Ingestion flow | Current for the Hatchet path; drop the Dagster/graph steps when read. |
| 05 PDF stack | Current through ADR-0019 (2026-09-02); vLLM/Qwen-VL page verbalisation references are stale. |
| 06 Retrieval + agents | §10 reconciled 2026-09-07; §11 still names Neo4j. |
| 07 Orchestration | **Reconciled 2026-09-07**: Horizon's three jobs, the 51-workflow Hatchet registry with every cron, the trigger paths, the Azure and GitHub schedulers, and the 2026-08-21 review findings marked open or closed. |
| 08 LLM + ML | Foundry cutover recorded; some vLLM-era detail remains. |
| 09–11, 13, 15–17b | Light staleness (an odd Neo4j or Dagster mention); read with Ch 14 alongside. |
| 12 Observability | Pre-July: describes Prometheus/Grafana/Loki/Tempo, none of which exist. Production observability is Azure Monitor + Log Analytics + Laravel Pulse (local only). Next to be rewritten. |
| 14 Status matrix | Maintained through 2026-09-02 but still carries a Dagster-assets section. |
| 18 Model stack evolution | Cited by ADR-0016 and ADR-0021; current to 2026-09-02. |

Every chapter except this one opens with a dated reconciliation notice.
Remove the notice when a chapter is rewritten and add the chapter to the
"Reconciled" rows above.

The compose file's header profile guide was corrected on 2026-09-07 to
match the `profiles:` keys. Other comments inside the file are still
stale; [Ch 01 §8](01-services.md#8-stale-comments-inside-docker-composeyml)
lists them.

## 8. Reading order for this manual

1. [Ch 01 — Services catalog](01-services.md) — every container *(pending rewrite; use §2 above and `docker-compose.yml` meanwhile)*.
2. [Ch 02 — Data stores](02-data-stores.md) — Postgres, Qdrant, Redis, object storage *(pending rewrite)*.
3. [Ch 03 — Schemas and tables](03-schemas.md) — what lives where in Postgres.
4. [Ch 04 — Ingestion flow](04-ingestion-flow.md) — upload → bronze → silver → gold → index.
5. [Ch 05 — PDF stack §04p](05-pdf-stack.md) — the in-process parser stack.
6. [Ch 06 — Retrieval + agents](06-retrieval-and-agents.md) — LangGraph, OIUR, intents, tools, persistence.
7. [Ch 07 — Orchestration](07-orchestration.md) — Horizon vs Hatchet *(pending rewrite)*.
8. [Ch 08 — LLM + ML models](08-llm-and-ml.md) — Foundry, embedder, reranker, SPLADE++.
9. [Ch 09 — Martin + MapLibre](09-martin-and-maplibre.md) — tiles, MVT functions.
10. [Ch 10 — Frontend](10-frontend.md) — pages, components, broadcast channels.
11. [Ch 11 — Tenancy + RLS](11-tenancy-and-rls.md) — workspaces, GUC, JWTs.
12. [Ch 12 — Observability](12-observability.md) — Azure Monitor, Pulse *(pending rewrite)*.
13. [Ch 13 — Data hierarchy](13-data-hierarchy.md) — geologist-facing classification.
14. [Ch 14 — Status matrix](14-status-matrix.md) — "is this thing real today?"
15. [Ch 15 — Design docs index](15-design-docs-index.md) — plan intent vs shipped.
16. [Ch 16 — Algorithmic spines](16-algorithmic-spines.md) — canonical corpus consolidation.
17. [Ch 17](17-strategic-context.md) / [17b](17b-master-plan-deep-dive.md) — strategic context.
18. [Ch 18 — Model stack evolution](18-model-stack-evolution.md) — the 2026-06 audit wave onward.
