# GeoRAG Intelligence V2.1

GeoRAG is a geological intelligence platform that ingests decades of fragmented exploration data (drill logs, NI 43-101 reports, geophysics, GIS layers) and lets geologists query it in natural language with cited answers, interactive visualizations, and export to industry modeling tools. Designed for junior mining and exploration companies with private-cloud or on-premise deployment.

## Status

**V1 production-hardened — engineering scope closed.** All 10 modules and the
23-item V1.5 follow-up backlog are complete. Hallucination prevention is
layered across six gates.

Test and RLS counts are a dated snapshot, not maintained here: at engineering
close (2026-04-27, [`docs/acceptance-criteria.md`](docs/acceptance-criteria.md))
there were ~1,500 automated assertions passing (199 pgTAP, ~622 FastAPI, 217
Laravel feature, 500 vitest, 14 tracing round-trip), RLS on 11 silver tables and
no active leak primitives. The suites have grown since.

Production runs on **Amazon ECS Fargate**, provisioned by Terraform in
[`deploy/aws/terraform/`](deploy/aws/terraform/) (ADR-0022, 2026-09-08). Start
at [`deploy/aws/README.md`](deploy/aws/README.md); the cutover gate is
`AWS_REGION=<region> bash scripts/operator/aws-preflight.sh`. The older
[`docs/OPERATOR-AFTERNOON.md`](docs/OPERATOR-AFTERNOON.md) /
`scripts/operator/preflight.sh` (SOPS + age) path predates ADR-0022 and now
applies only to the on-prem Helm chart (`charts/georag/`).

The full ship-readiness checklist lives at
[`docs/acceptance-criteria.md`](docs/acceptance-criteria.md).

## Architecture Reference

**[`georag-architecture.html`](georag-architecture.html)** is the complete specification. It contains every technology decision, data schema, interface contract, deployment detail, performance tuning, and acceptance criterion. Read the relevant section before starting any task.

**[`CLAUDE.md`](CLAUDE.md)** documents project rules, agent delegation, code style, and commit conventions. Start here if you're contributing.

## Technology Stack

- **Frontend**: React 19 + Inertia.js v3, shadcn/ui + Tailwind v4, MapLibre GL, Plotly (no graph view — React Flow was removed 2026-08-28)
- **Application**: Laravel 13 on Octane (Swoole), Horizon, Reverb, Sanctum, Pulse (local-only)
- **Domain Service**: FastAPI 0.141 on Python 3.13, LangGraph, asyncpg, redis.asyncio, async Qdrant client
- **Data Stores**: PostgreSQL 18 + PostGIS 3.6 (PgBouncer edoburu 1.25.1 in compose only; RDS for PostgreSQL 18 in production), Qdrant v1.19, Redis 8.6 (8.10 in production), SeaweedFS in compose / AWS S3 in production (both S3-compatible). No knowledge graph — Neo4j was removed 2026-07-28.
- **Ingestion**: Hatchet workflows (`ingest_pdf`, `ingest_tabular`, `ingest_spatial`, `ingest_well_logs`, `ingest_geophysics`, …) on one `hatchet-worker`; parsers in the local `georag_geoparsers` package (Polars, GDAL via pyogrio/GeoPandas/rasterio, lasio, …); scanned-page OCR is Cohere Parse 5 with Tesseract as last resort
- **LLM**: Cohere Command A+ on Cohere's own API (`LLM_BACKEND=cohere`, the default); `bedrock`, `vllm` and `anthropic` (Claude, optional fallback) are selectable
- **Embedding / rerank**: Cohere Embed 5 Pro (`embed-v5.0-pro`, 1024 dims) on Cohere's own API in production since 2026-10-05 (ADR-0025; Embed v4 on Bedrock is the rollback until 2026-10-19), Cohere Rerank 3.5 on Bedrock; in dev, local Qwen3 embedding/reranker + SPLADE++ sparse sidecars
- **Production**: Amazon ECS Fargate, Terraform in `deploy/aws/terraform/` (ADR-0022)

## Getting Started (Development)

### 1. Clone and configure

```bash
git clone <repo> .
cp .env.example .env
```

Update `.env` with:
- `APP_KEY`: Generate with `php artisan key:generate --show` (run in container later)
- `FASTAPI_SERVICE_KEY`: Generate with `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`
- `COHERE_API_KEY`: required for the default `LLM_BACKEND=cohere` (chat) and Cohere Parse OCR
- `ANTHROPIC_API_KEY` only if using `LLM_BACKEND=anthropic`

### 2. Start infrastructure

```bash
docker compose --profile dev-light --profile dev-data up -d
```

PostgreSQL, PgBouncer, Redis and Martin have no profile and always start.
The profiles (the `profiles:` key on each service in `docker-compose.yml` is
the truth):
- `--profile dev-light`: Laravel Octane (`APP_PORT`, 8888 in `.env.example`), Horizon, and Reverb (port 8085)
- `--profile dev-data`: FastAPI (port 8000), the `embedding` / `sparse` / `reranker` sidecars (the reranker wants the GPU), Qdrant, SeaweedFS (service `minio`) + `minio-init`, `hatchet-lite` + `hatchet-worker`
- `--profile dev-full`: Everything (full integration test)

A working day is `dev-light` + `dev-data`.

### 3. Run migrations and seed data

```bash
docker exec georag-laravel-octane php artisan migrate --database=pgsql_migrations
docker exec georag-laravel-octane php artisan db:seed
```

### 4. Open the app

- **Frontend**: http://localhost:8888
- **Laravel Octane**: http://localhost:8888/api
- **FastAPI docs**: http://localhost:8000/docs
- **Qdrant (if dev-data)**: http://localhost:6333/dashboard
- **SeaweedFS S3 API (if dev-data)**: http://localhost:8333
- **Hatchet (if dev-data)**: http://localhost:8889
- **Martin tiles**: http://localhost:3002

## Running Tests

### Laravel

```bash
docker exec georag-laravel-octane php artisan test
```

On Windows WSL, you may need to specify the shell:

```bash
docker exec -u www-data georag-laravel-octane bash -c 'php artisan test'
```

### FastAPI

```bash
docker exec georag-fastapi pytest
```

### Frontend (React)

```bash
npm run test
```

Run with coverage:

```bash
npm run test -- --coverage
```

## Project Layout

```
.
├── app/                      # Laravel application (HTTP, models, jobs)
├── src/
│   ├── fastapi/             # Python domain service (orchestration, LLM, retrieval, Hatchet workflows)
│   ├── georag_geoparsers/   # Format parsers used by the ingestion workflows
│   └── georag_object_storage/ # S3-compatible object-storage client
├── resources/js/            # React + Inertia.js frontend
├── tests/                   # Laravel feature + unit tests
├── docs/
│   ├── RUNBOOK.md          # Operator procedures (PII handling, secrets)
│   └── ...                 # Deployment, tuning, troubleshooting
├── ops/
│   ├── runbooks/            # 4 current runbooks (aws-oncall, ...) + 40 archived compose-era ones
│   ├── audit/               # Module security/observability audit reports
│   ├── baselines/           # API latency + capacity-planning baselines
│   └── backlog/             # V1.5 follow-up tracker (engineering-closed 2026-04-26)
├── scripts/operator/        # First-deploy bootstrap + GitHub Secrets + preflight
├── openspec/                # OpenSpec change-workflow config (config.yaml)
├── docker/                  # Dockerfile build contexts + service configs (postgresql, martin, seaweedfs)
├── deploy/aws/terraform/    # Production infrastructure (ECS Fargate, RDS, ...)
├── charts/georag/           # Helm chart for on-prem deployment
├── docker-compose.yml       # Service definitions + profiles
├── .env.example             # Template environment variables (dev defaults)
├── .env.production.example  # Production template (secrets as CHANGE_ME placeholders)
├── CLAUDE.md                # Project rules + agent delegation
└── georag-architecture.html # Complete spec (schema, design, acceptance)
```

## Key Documentation

- [**CLAUDE.md**](CLAUDE.md) — Project context, hard rules, agent responsibilities, code style, commit convention
- [**georag-architecture.html**](georag-architecture.html) — Complete spec: Section 00 (README) → Section 04 (schemas + pipelines) → Section 05-06 (deployment + tuning)
- [**docs/acceptance-criteria.md**](docs/acceptance-criteria.md) — Canonical "is V1 done?" checklist, 21/22 ✅ at engineering close
- [**deploy/aws/README.md**](deploy/aws/README.md) — Production deployment on AWS ECS Fargate (Terraform, preflight, first-deploy steps)
- [**docs/OPERATOR-AFTERNOON.md**](docs/OPERATOR-AFTERNOON.md) — Pre-ADR-0022 first-deploy checklist (SOPS bootstrap, GitHub Secrets, cold-start); now relevant to the on-prem Helm path only
- [**docs/RUNBOOK.md**](docs/RUNBOOK.md) — Operator procedures for PII decryption, secret rotation, database maintenance
- [**ops/runbooks/**](ops/runbooks/) — four runbooks (`aws-oncall`, `secret-rotation`, `refusal-rate-spike`, `raw-sql-layer` — the last three still carry Azure-era procedures in places, and say so) plus 40 archived compose-era ones under `_archived/`
- [**ops/backlog/v1.5-followups.md**](ops/backlog/v1.5-followups.md) — V1.5 follow-up tracker with per-item close-out evidence

## Contributing

1. Read [CLAUDE.md](CLAUDE.md) for agent delegation and code style
2. Use conventional commits: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`
3. Include architecture doc section references in commit bodies: `feat(rag): per Section 04i`
4. Golden query tests and hallucination failure tests must pass before milestone acceptance
5. See **`test-engineer`** agent for testing patterns

## License

No license file is published with this repository. All rights reserved by
the copyright holder. Source is shared for review and collaboration only;
no permission is granted for redistribution or commercial use without
written agreement.

Third-party dependencies are kept to free and permissive licenses
(MIT, BSD, Apache 2.0, MPL-2.0), no GPL. The hosted model APIs (Cohere,
AWS Bedrock, optional Anthropic) are paid services under their own terms.

---

For questions on architecture, geology domain decisions, or agent responsibilities, see [CLAUDE.md](CLAUDE.md).
