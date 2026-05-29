# Chapter 00 — System Overview

> **What this is.** A "car repair manual" for GeoRAG. Every chapter cites file
> paths and line numbers so a new engineer can open the source and verify any
> claim. If the code and the manual disagree, the code wins — open an issue and
> fix the manual.
>
> **Authoritative companion docs.** `georag-architecture.html` (the long-form
> spec), `docs/SERVICE_INVENTORY.md` (oncall service table), `docs/RUNBOOK.md`
> (operator procedures), `docs/adr/*` (decision records). Cross-reference, but
> the chapters under this directory are the structured reading path.

## 1. What GeoRAG is

GeoRAG is a multi-tenant geological intelligence platform. Junior-mining and
exploration companies upload decades of fragmented exploration data — drill
logs, NI 43‑101 reports, geophysics surveys, GIS layers, geochemistry
spreadsheets — and ask questions in natural language. The system returns
cited answers, interactive map and section visualisations, and exports to
industry modelling tools.

The repo is a single monorepo at `C:\Users\GeoRAG\Herd\georag` containing:

- A **Laravel 13** application on **Octane/Swoole** ([CLAUDE.md:64](../../CLAUDE.md))
  for everything user-facing: authentication, project CRUD, uploads, broadcast
  of WebSocket events, server-side rendering of Inertia/React pages.
- A **FastAPI 0.135** Python 3.13 domain service ([CLAUDE.md:66](../../CLAUDE.md))
  for the RAG pipeline, all asynchronous database I/O, Pydantic AI agents,
  hallucination prevention, citation enforcement, and SSE streaming back to
  Laravel.
- A **Hatchet** workflow engine ([docker-compose.yml:1861](../../../docker-compose.yml))
  with two Python worker pools (`ingestion`, `ai`) for durable file ingestion,
  outbox dispatch, embedding, scheduled audit verification, and per-document
  parsing.
- A **Dagster** project ([src/dagster/georag_dagster/](../../../src/dagster/georag_dagster/))
  containing bronze→silver→gold material‑isation assets, reranker‑label
  synthesis, and scheduled bulk pipelines.
- A **Kestra** orchestrator ([kestra/flows/georag/](../../../kestra/flows/georag/))
  for scheduled external feeds and integration‑edge work (public geoscience
  pulls, external notifications, support‑packet dispatch).
- A **React 19 + Inertia 3** frontend ([resources/js/Pages/](../../../resources/js/Pages/))
  with shadcn/ui, Tailwind v4, MapLibre GL, React Flow and Plotly.
- A row of **data stores**: PostgreSQL 18 + PostGIS 3.6 behind PgBouncer,
  Neo4j Community 2026.03 for the geological knowledge graph, Qdrant 1.17 for
  vector retrieval, Redis 8 for sessions/queues/caches/rate-limits,
  SeaweedFS 4.20 (S3‑compatible) for the bronze object store, ClickHouse for
  Langfuse, and Tempo + Loki for traces/logs.
- A **vLLM 0.21** inference server hosting `Qwen/Qwen3-14B-AWQ`
  ([docker-compose.yml:1476](../../../docker-compose.yml)).
- A **Martin 1.7** tile server ([docker/martin/martin.yaml](../../../docker/martin/martin.yaml))
  that converts PostGIS function/table sources into Mapbox Vector Tiles.

## 2. Profiles and how the stack starts

Compose is profile‑driven (see [docker-compose.yml:21](../../../docker-compose.yml)):

| Profile        | What it brings up                                                                                       |
|----------------|---------------------------------------------------------------------------------------------------------|
| *(default)*    | Postgres, PgBouncer, Redis — the always‑on substrate                                                    |
| `dev-light`    | Laravel Octane + Horizon + Reverb, Martin, FastAPI, Caddy                                              |
| `dev-data`     | Neo4j (+ warmup), Qdrant, SeaweedFS (+ minio-init), Hatchet Lite, Hatchet workers, Kestra, OTel, Tempo |
| `dev-ingest`   | Dagster daemon + webserver                                                                              |
| `dev-monitor`  | Prometheus, Alertmanager, exporters (redis/postgres/neo4j), Loki, Promtail, Grafana                    |
| `gpu-llm`      | vLLM + warmup sidecar (alias `gpu-llm-prod`)                                                            |
| `dev-full`     | Union of all the above                                                                                  |

A normal day on Kyle's workstation runs `dev-light` + `dev-data`. Add
`gpu-llm` when chatting. Add `dev-ingest` when working on Dagster.

## 3. Request shape, end‑to‑end

```
┌──────────┐      HTTP(S)        ┌────────────────┐
│ Browser  │  ─────────────────▶ │ Caddy (edge)   │  (only for /admin/integrations/kestra/* PAT path
│ React    │                     └─────┬──────────┘   and the :8443 TLS listener; main app
│ MapLibre │                           ▼              hits laravel-octane directly on :80)
└────┬─────┘                ┌──────────────────────┐
     │ WebSocket            │ laravel-octane :80   │  ← Inertia HTML + JSON
     │                      │ (Swoole, 4 workers)  │  ← /api/* JSON endpoints
     ▼                      └─────┬────────────────┘
┌──────────────┐                  │ HTTP + X-Service-Key (FASTAPI_SERVICE_KEY)
│ laravel-reverb│ ◀────────────── ┤ + Sanctum cookie OR PAT
│ :8085/:8080  │  broadcast       │
└──────────────┘                  ▼
                          ┌────────────────────┐
                          │ fastapi :8000      │ ← all RAG pipeline + Pydantic AI agents
                          │ (uvicorn, 6 wrkr)  │
                          └──┬───┬──┬──┬──┬────┘
   ┌──────────────┬─────────┘   │  │  │  └─── Hatchet client (gRPC) → hatchet-lite :7077
   ▼              ▼             ▼  ▼  ▼
┌──────────┐ ┌──────────┐ ┌─────────┐ ┌────────┐ ┌────────┐
│pgbouncer │ │ qdrant   │ │  neo4j  │ │ redis  │ │ minio  │  (SeaweedFS, S3 API on 8333)
│  :6432   │ │  :6333   │ │ :7687   │ │ :6379  │ │ :8333  │
└────┬─────┘ └──────────┘ └─────────┘ └────────┘ └────────┘
     │
     ▼
┌─────────────┐                 ┌──────────────────────────┐
│ postgresql  │ ◀─ Martin ──── │ /tiles/<source>/{z/x/y}  │ (MVT, direct PG conn)
│  :5432      │                 └──────────────────────────┘
└─────────────┘
```

## 4. Glossary

| Term | Meaning |
|------|---------|
| **Bronze** | Immutable raw‑file/raw‑row archive. SeaweedFS bucket `bronze` + Postgres `bronze.*` tables (ingest_manifest, provenance, upload_files). |
| **Silver** | Canonical, deduped, validated domain rows. PostgreSQL `silver.*` schema (collars, lithology_intervals, samples, assays_v2, reports, etc.). |
| **Gold** | Pre-computed materialisations for fast read paths (h3 density, visual cross-section panels, structure measurements, etc.). |
| **Public Geoscience / public_geo** | Government‑published reference layers (mines, mineral occurrences, bedrock geology, etc.) ingested by Kestra’s `public_geoscience_pull` flow. Schema rename to `public_geoscience` is in flight (see [docker/martin/martin.yaml:5](../../../docker/martin/martin.yaml)). |
| **OIUR** | "Observation/Interpretation/Uncertainty/Recommendation" — the structured answer envelope produced by the §04j answer architecture, gated on `GEO_ANSWER_OIUR_ENABLED`. |
| **Agentic Retrieval v2** | The §04j LangGraph that routes per intent. Gated on `AGENTIC_RETRIEVAL_V2_ENABLED` ([docker-compose.yml:999](../../../docker-compose.yml)). |
| **§04p PDF stack** | The in-process PDF ingest stack (qpdf, pypdfium2, pdfminer.six, pdfplumber, Docling, RapidOCR / Tesseract fallback, Qwen-VL on vLLM). Replaced RAGFlow per [ADR-0002](../../adr/). |
| **Workspace** | The tenancy unit. Every silver/gold/bronze write carries `workspace_id` (uuid). RLS policies on every table key off `current_setting('app.workspace_id', true)` ([app/Support/SetsWorkspaceRlsContext.php:10-44](../../../app/Support/SetsWorkspaceRlsContext.php)). |
| **Audit ledger** | Append-only hash-chained audit log in `audit.*`. See [docs/audit_ledger_hash_recipe.md](../../audit_ledger_hash_recipe.md) and the daily `audit_ledger_verify` Hatchet workflow. |
| **Outbox** | `outbox.pending_propagations` table polled by the `outbox_dispatcher` Hatchet workflow to fan a single silver write out to Qdrant + Neo4j + SeaweedFS. |
| **Hatchet pools** | `WORKER_POOL=ingestion` runs upload-triggered work; `WORKER_POOL=ai` runs GPU work (embedding, reranking, scoring, etc.). Both pools register the same worker entrypoint `app/hatchet_workflows/worker.py` and select tasks via the env var. |
| **Reverb channels** | Laravel-side WebSocket channels — `ingestion-progress.{workspace_id}`, `workspace-data-updated.{workspace_id}`, `query.streaming.{run_id}`, etc. |
| **FASTAPI_SERVICE_KEY** | Shared‑secret HMAC used on `X-Service-Key` headers between Laravel ↔ FastAPI and Hatchet workers ↔ Laravel. Distinct from per-flow Kestra JWTs (`KESTRA_FLOW_JWT_SECRET`). |
| **martin_ro** | Read‑only Postgres role Martin uses (planned; currently it connects as `georag_app` per [docker-compose.yml:812](../../../docker-compose.yml)). |

## 5. The nine hard rules (from CLAUDE.md)

These are not style preferences. They are tripwires. Source:
[CLAUDE.md:27-60](../../../CLAUDE.md).

1. **No Streamlit.** Frontend is React + Inertia + shadcn/ui + Tailwind.
2. **Async-native drivers only in FastAPI** — `asyncpg`, `redis.asyncio`, async Qdrant, async Neo4j.
3. **Octane-safe Laravel code.** No static state leaks.
4. **Citations mandatory.** Every RAG claim must carry a `source_chunk_id`. Pydantic AI rejects otherwise.
5. **Hallucination prevention §04i — six layers** must apply to anything touching the pipeline.
6. **Schemas in §04e are contracts.** Don’t invent fields.
7. **No orchestration overlap.** Laravel queues = user‑triggered; Dagster = scheduled bulk; Hatchet = durable per‑document; Kestra = integration edge.
8. **MapLibre GL, not Mapbox GL.** Licensing matters for on‑prem.
9. **Neo4j Community Edition only.** No Enterprise features.

## 6. Reading order for this manual

1. [Ch 01 — Services catalog](01-services.md) — every container.
2. [Ch 02 — Data stores](02-data-stores.md) — Postgres, Neo4j, Qdrant, Redis, SeaweedFS.
3. [Ch 03 — Schemas and tables](03-schemas.md) — what lives where in Postgres.
4. [Ch 04 — Ingestion flow](04-ingestion-flow.md) — upload → bronze → silver → gold → graph.
5. [Ch 05 — PDF stack §04p](05-pdf-stack.md) — the in-process parser stack.
6. [Ch 06 — Retrieval + agents](06-retrieval-and-agents.md) — LangGraph, OIUR, intents, tools.
7. [Ch 07 — Orchestration](07-orchestration.md) — Hatchet vs Dagster vs Kestra vs Horizon.
8. [Ch 08 — LLM + ML models](08-llm-and-ml.md) — vLLM, embedder, reranker, SPLADE++.
9. [Ch 09 — Martin + MapLibre](09-martin-and-maplibre.md) — tiles, MVT functions.
10. [Ch 10 — Frontend](10-frontend.md) — pages, components, broadcast channels.
11. [Ch 11 — Tenancy + RLS](11-tenancy-and-rls.md) — workspaces, GUC, JWTs.
12. [Ch 12 — Observability](12-observability.md) — Langfuse, Pulse, Prom/Grafana, Loki, Tempo.
