# GeoRAG Intelligence V1.0

GeoRAG is a geological intelligence platform that ingests decades of fragmented exploration data (drill logs, NI 43-101 reports, geophysics, GIS layers) and lets geologists query it in natural language with cited answers, interactive visualizations, and export to industry modeling tools. Designed for junior mining and exploration companies with private-cloud or on-premise deployment.

## Status

**V1 production-hardened — engineering scope closed.** All 10 modules and the
23-item V1.5 follow-up backlog are complete. ~1,500 automated assertions
passing (199 pgTAP, ~622 FastAPI, 217 Laravel feature, 500 vitest, 14 tracing
round-trip). 9/9 Prometheus targets UP. 28 operational runbooks. Zero active
leak primitives, hallucination prevention layered across six gates, RLS on 11
silver tables.

First production deployment is gated on a one-afternoon operator setup
documented in [`docs/OPERATOR-AFTERNOON.md`](docs/OPERATOR-AFTERNOON.md) — SOPS
key generation, GitHub Secrets provisioning, and the cold-start procedure.
Run `bash scripts/operator/preflight.sh` to see the current state of the
seven O-01..O-07 gates.

The full ship-readiness checklist lives at
[`docs/acceptance-criteria.md`](docs/acceptance-criteria.md).

## Architecture Reference

**[`georag-architecture.html`](georag-architecture.html)** is the complete specification. It contains every technology decision, data schema, interface contract, deployment detail, performance tuning, and acceptance criterion. Read the relevant section before starting any task.

**[`CLAUDE.md`](CLAUDE.md)** documents project rules, agent delegation, code style, and commit conventions. Start here if you're contributing.

## Technology Stack

- **Frontend**: React + Inertia.js, shadcn/ui + Tailwind, MapLibre GL, React Flow, Plotly
- **Application**: Laravel 13 on Octane (Swoole/RoadRunner), Horizon, Reverb, Sanctum, Pulse
- **Domain Service**: FastAPI 0.135.x on Python 3.13, Pydantic AI, asyncpg, aioredis
- **Data Stores**: PostgreSQL 18.3 + PostGIS 3.6.3 (PgBouncer edoburu 1.25), Neo4j Community 2026.03, Qdrant v1.17, Redis 8.6, SeaweedFS (S3-compatible)
- **Ingestion**: Dagster, Polars, DuckDB, GDAL/GeoPandas, lasio/segyio/obspy, RAGFlow
- **LLM**: Ollama + DeepSeek distills (dev), vLLM + DeepSeek V3 (prod), Claude/GPT-4 API (optional fallback)

## Getting Started (Development)

### 1. Clone and configure

```bash
git clone <repo> .
cp .env.example .env
```

Update `.env` with:
- `APP_KEY`: Generate with `php artisan key:generate --show` (run in container later)
- `FASTAPI_SERVICE_KEY`: Generate with `python3 -c 'import secrets; print(secrets.token_urlsafe(48))'`
- LLM API keys if using `LLM_BACKEND=anthropic` (Claude, GPT-4)
- `OLLAMA_KEEP_ALIVE`: Dev default is `30m`; change to `5m` if VRAM is constrained

### 2. Start infrastructure

```bash
docker compose --profile dev-light up -d
```

This starts PostgreSQL, Redis, Laravel Octane (port 8888), FastAPI (port 8000), and Reverb (port 8085) for WebSocket streaming.

Optional profiles:
- `--profile dev-data`: Adds Neo4j, Qdrant, MinIO
- `--profile dev-llm`: Adds Ollama (requires `docker run --gpus all` or NVIDIA Container Toolkit)
- `--profile dev-ingest`: Adds Dagster, RAGFlow
- `--profile dev-full`: Everything (full integration test)

### 3. Run migrations and seed data

```bash
docker exec georag-laravel-octane php artisan migrate
docker exec georag-laravel-octane php artisan db:seed
```

### 4. Open the app

- **Frontend**: http://localhost:8888
- **Laravel Octane**: http://localhost:8888/api
- **FastAPI docs**: http://localhost:8000/docs
- **Neo4j (if dev-data)**: http://localhost:7474
- **Qdrant (if dev-data)**: http://localhost:6333/docs
- **MinIO (if dev-data)**: http://localhost:9001
- **Ollama (if dev-llm)**: http://localhost:11434

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
│   ├── fastapi/             # Python domain service (orchestration, LLM, retrieval)
│   └── dagster/             # Ingestion pipeline orchestration
├── resources/js/            # React + Inertia.js frontend
├── tests/                   # Laravel feature + unit tests
├── docs/
│   ├── RUNBOOK.md          # Operator procedures (PII handling, secrets)
│   └── ...                 # Deployment, tuning, troubleshooting
├── ops/
│   ├── runbooks/            # 28 operational runbooks (deploy, rollback, on-call, ...)
│   ├── audit/               # Module security/observability audit reports
│   ├── baselines/           # API latency + capacity-planning baselines
│   └── backlog/             # V1.5 follow-up tracker (engineering-closed 2026-04-26)
├── scripts/operator/        # First-deploy bootstrap + GitHub Secrets + preflight
├── openspec/                # OpenAPI / AsyncAPI specifications
├── docker/                  # Dockerfile build contexts + Prometheus/Grafana/Loki configs
├── docker-compose.yml       # Service definitions + profiles
├── .env.example             # Template environment variables (dev defaults)
├── .env.production.example  # Production template (143 keys, secrets as CHANGE_ME placeholders)
├── CLAUDE.md                # Project rules + agent delegation
└── georag-architecture.html # Complete spec (schema, design, acceptance)
```

## Key Documentation

- [**CLAUDE.md**](CLAUDE.md) — Project context, hard rules, agent responsibilities, code style, commit convention
- [**georag-architecture.html**](georag-architecture.html) — Complete spec: Section 00 (README) → Section 04 (schemas + pipelines) → Section 05-06 (deployment + tuning)
- [**docs/acceptance-criteria.md**](docs/acceptance-criteria.md) — Canonical "is V1 done?" checklist, 21/22 ✅ at engineering close
- [**docs/OPERATOR-AFTERNOON.md**](docs/OPERATOR-AFTERNOON.md) — One-afternoon checklist for first production deploy (SOPS bootstrap, GitHub Secrets, cold-start)
- [**docs/RUNBOOK.md**](docs/RUNBOOK.md) — Operator procedures for PII decryption, secret rotation, database maintenance
- [**ops/runbooks/**](ops/runbooks/) — 28 scenario-specific runbooks (deploy-rollback, on-call, authz-audit-triage, refusal-rate-spike, llm-model-swap, …)
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

All third-party dependencies are restricted to free and permissive licenses
(MIT, BSD, Apache 2.0, MPL-2.0). No GPL, no paid SaaS — see CLAUDE.md
"Free licensing only" rule.

---

For questions on architecture, geology domain decisions, or agent responsibilities, see [CLAUDE.md](CLAUDE.md).
