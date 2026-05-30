# CI/CD + Operations — GeoRAG Intelligence

> Build / test / deploy / operate. Routes into the live workflow files
> (`.github/workflows/`), Dockerfiles (`docker/`), operator scripts
> (`scripts/operator/`, `ops/setup/`), and operational runbooks
> (`ops/runbooks/`).
>
> System composition: [`SAD.md`](SAD.md).
> Data persistence: [`DFS.md`](DFS.md).
> Caller-facing contracts: [`API_DOCUMENTATION.md`](API_DOCUMENTATION.md).

---

## 1. Workflow inventory (`.github/workflows/`)

7 GitHub Actions workflows.

| File | Name | Triggers | Purpose |
|---|---|---|---|
| `ci.yml` | GeoRAG CI | push (`main`/`develop`), PR (`main`) | Primary CI gate — lint + tests + docker build + scan + SBOM + pgTAP |
| `cd.yml` | CD — Deploy | `workflow_run` on CI success (`main`), `workflow_dispatch` | Three-stage SSH deploy (dev → staging → production) |
| `e2e.yml` | E2E tests | per file (not enumerated — flagged) | Playwright + integration suite |
| `chaos.yml` | Chaos / resilience | cron weekly | FastAPI resilience under failure injection |
| `perf-baseline.yml` | Perf baseline | cron / dispatch | API latency + capacity baseline measurement |
| `release-rehearsal.yml` | Release rehearsal | tag push / dispatch | Bucket 3 stack-dependent gates (integration / golden / hallucination / live markers) |
| `tenant-isolation-auditor.yml` | Tenant isolation auditor | cron / dispatch | RLS / workspace-isolation regression auditor |

**Dependabot** (`.github/dependabot.yml`) — 5 ecosystems:

| Ecosystem | Directory | Cadence |
|---|---|---|
| `composer` | `/` | Weekly, Monday |
| `npm` | `/` | Weekly, Monday |
| `pip` | `/src/fastapi` | Weekly, Monday |
| `github-actions` | `/` | Monthly |
| `docker` | `/` | Monthly |

npm carries a `groups.types-only` rule (`@types/*` → patch-only) to suppress weekly noise.

---

## 2. CI pipeline (`ci.yml`)

### 2.1 Triggers + concurrency + tool pins

```yaml
on:
  push:    { branches: [main, develop] }
  pull_request: { branches: [main] }

concurrency:
  group: ci-${{ github.ref }}
  cancel-in-progress: true
```

Pinned tool versions: `PHP 8.4`, `Node 22`, `Python 3.13`.

### 2.2 Job graph

```mermaid
flowchart TD
    Push[push / PR] --> Lar[laravel<br/>Pint + PHPUnit]
    Push --> PyL[python-lint<br/>Ruff + import-cover + LangGraph boundary]
    Push --> Qwen[qwen-config-drift]
    Push --> FE[frontend<br/>tsc + vitest + Vite build]
    Push --> Had[hadolint<br/>warn-only]
    Push --> Docker[docker-build<br/>build + Trivy + SBOM matrix]
    PyL --> PyT[python-test<br/>pytest fast suite]
    Lar --> PgTAP[pgTAP<br/>MVT assertions]
```

### 2.3 Per-job detail

**`laravel`** (10 min) — Postgres service `postgis/postgis:18-3.6` digest-pinned (sha256:f81dd…). Steps: `composer install` → `vendor/bin/pint --test` → `key:generate` → `migrate --force` → `php artisan test --parallel`.

**`python-lint`** (5 min) — `ruff check` on `src/fastapi/` + `src/dagster/`. Plus `scripts/check_pyproject_covers_imports.py` (catches the hole where `sentry_sdk` was imported but not in `pyproject.toml::dependencies`). Plus `scripts/ci/langgraph_boundary_check.sh` (Phase 0 #16 — fails the build if LangGraph touches Hatchet-owned retry work or Kestra-owned outbound webhooks).

**`qwen-config-drift`** (2 min) — `scripts/check_qwen_config_drift.py` verifies four Qwen settings stay consistent across `.env.example`, `docker-compose.yml`, `config.py`.

**`python-test`** (10 min, depends on `python-lint`) — `uv sync --extra dev` for both FastAPI + Dagster. Marker exclusions:
- FastAPI fast suite: `-m "not integration and not golden and not hallucination and not live and not chaos"`.
- Dagster: `-m "not integration"`.
- The excluded markers route to `release-rehearsal.yml` (Bucket 3) and `chaos.yml` (weekly).

**`frontend`** (10 min) — `npm ci` → `npx tsc --noEmit` → `npm run test --if-present` → `npm run build`.

**`pgtap`** (15 min, depends on `laravel`) — Builds pgTAP 1.3.3 from source against PG 18. Loads `database/tests/pgtap/seed_golden_fixture.sql`. Runs every `NN_*.sql` file. Per-file PASS/FAIL with assertion plan counts.

**`hadolint`** (5 min, warn-only) — `docker/{fastapi,laravel,dagster}.Dockerfile` with `failure-threshold: warning`. Tighten to `error` once lint-clean.

**`docker-build`** (30 min, matrix) — Matrix over `{ fastapi, laravel, dagster }`. `fail-fast: false`. Platforms `linux/amd64,linux/arm64`. PR: build + load only. main: build + push to GHCR with `:<short-sha>` (immutable, consumed by `cd.yml`) and `:main` (rolling). GHA cache scoped per image. Permissions: `contents: read`, `packages: write`, `id-token: write`. **Trivy** severity `CRITICAL` fails build, `ignore-unfixed: true`. **SBOM** via `anchore/sbom-action` (SPDX-JSON, 90-day artifact retention).

---

## 3. CD pipeline (`cd.yml`)

### 3.1 Topology

```mermaid
flowchart LR
    CI[CI green on main] --> Check[check-ci<br/>resolve SHA]
    Check --> Dev[deploy_dev<br/>environment: dev<br/>no approval]
    Dev --> Staging[deploy_staging<br/>environment: staging<br/>required reviewer]
    Staging --> Prod[deploy_production<br/>environment: production<br/>required reviewer + Require branches: main]
```

Triggers:
- Automatic: `workflow_run` on `GeoRAG CI` success (`main`).
- Manual: `workflow_dispatch` with `target ∈ {dev, staging, production}` + optional `sha`.

### 3.2 Per-stage steps (uniform across dev / staging / production)

1. `actions/checkout@v4`.
2. **Install SOPS 3.9.4** from GitHub Releases (.deb).
3. **Decrypt** `.env.production.enc` → `.env.production` using `SOPS_AGE_PRIVATE_KEY` (age private key, per-environment secret).
4. **SSH deploy** to `<TARGET>_SSH_HOST` as `<TARGET>_SSH_USER` with `<TARGET>_SSH_KEY`:
   - `scp` decrypted `.env.production` to `/opt/georag/.env.production`.
   - `ssh` + `docker compose --env-file .env.production pull` for the four app services (`fastapi`, `laravel-octane`, `laravel-horizon`, `laravel-reverb`).
   - `docker compose ... --profile dev-light up -d --pull always`.
5. **Cleanup** — delete runner-local plaintext env in `if: always()` step.
6. **Health check** — poll `${BASE_URL}:8000/health` (FastAPI) and `${BASE_URL}/up` (Laravel) for up to 5 min (60 × 5s).

### 3.3 Environments + secrets

| Env | Approval | Secrets required |
|---|---|---|
| `dev` | none — auto on CI green | `SOPS_AGE_PRIVATE_KEY`, `DEV_SSH_{HOST,USER,KEY}`, optional `DEV_BASE_URL` |
| `staging` | required reviewer | `STAGING_SSH_{HOST,USER,KEY}`, `STAGING_URL` |
| `production` | required reviewer + "Require branches: main" | `PRODUCTION_SSH_{HOST,USER,KEY}` |

### 3.4 `continue-on-error` debt

3 steps in `cd.yml` carry `continue-on-error: true` + TODO markers gated on `SOPS_AGE_PRIVATE_KEY` + SSH secrets being provisioned: "Decrypt environment secrets", "Deploy via SSH", "Health check". Remove once secrets land (`ops/runbooks/secret-management.md`).

### 3.5 Production deploy primitive

On-prem / private cloud — SSH + `docker compose` is the primitive used by `cd.yml`. Each environment is a single host running the compose stack.

**Helm chart alternative** exists but is **not driven by `cd.yml`**:
- `charts/georag/` — Helm chart with 17 templates → 40 K8s resources (13 Deployment, 13 Service, 6 StatefulSet, 2 HPA, 2 PVC, 1 Ingress, 1 CronJob, 1 Job, 1 Namespace, 1 Secret). Values overlays:
  - `values-vanilla.yaml` — generic K8s.
  - `values-k3s.yaml` — K3s topology.
  - `values-airgap.yaml` — airgapped on-prem; pairs with `airgap/install.sh` 5-step K3s installer.
- `kubernetes/manifests/` — raw manifests.
- `docs/deployment/{k3s-quickstart.md,k8s-reference.md}`.

Confirm canonical prod path before relying on this — [`HANDOVER_INDEX.md`](HANDOVER_INDEX.md) §5.5.

---

## 4. Container build + image registry/lifecycle

### 4.1 Dockerfiles (5)

| File | Base | EXPOSE | Healthcheck | CMD |
|---|---|---|---|---|
| `docker/fastapi.Dockerfile` | `python:3.13-slim` (2-stage builder/runtime) | 8000 | `curl -f http://localhost:8000/health` | `uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${UVICORN_WORKERS:-6} --no-access-log --proxy-headers --forwarded-allow-ips '*' --timeout-graceful-shutdown 30 --header "server:GeoRAG"` |
| `docker/laravel.Dockerfile` | `php:8.5-cli` (2-stage) | 80, 8080 | `curl -f http://localhost:80/up` | `php artisan octane:start --host=0.0.0.0 --port=80 --server=swoole` |
| `docker/dagster.Dockerfile` | `python:3.13-slim` (single-stage) | (none) | (compose-side) | `["dagster-daemon", "run"]` (webserver service overrides to `dagster-webserver -h 0.0.0.0 -p 3001`) |
| `docker/backup-agent/Dockerfile` | `alpine@sha256:d9e853e8…` (digest-pinned) | (none) | (none — cron via Ofelia compose labels) | `["sleep", "infinity"]` |
| `docker/langfuse-mcp.Dockerfile` | (per overlay) | (overlay-specific) | (overlay-specific) | (overlay-specific) |

#### 4.1.1 FastAPI Dockerfile internals

- Builder apt: `build-essential`, `libpq-dev`, `libffi-dev`, `gdal-bin`, `libgdal-dev`, `libgeos-dev`, `libproj-dev`.
- ENV (geo build hints): `GDAL_CONFIG=/usr/bin/gdal-config`, `CPLUS_INCLUDE_PATH=/usr/include/gdal`, `C_INCLUDE_PATH=/usr/include/gdal`.
- Builder: `pip install uv` → `uv pip install --system --no-cache -r pyproject.toml` → extras `pytest>=8.0 pytest-asyncio>=0.25 slowapi>=0.1.9`.
- Runtime apt: `libpq5`, `gdal-bin`, `libgdal36`, `libgeos-c1t64`, `libproj25`, `curl`, `tesseract-ocr`, `tesseract-ocr-eng`, `poppler-utils`, `libgl1`, `libglib2.0-0`, `libpango-1.0-0`, `libpangoft2-1.0-0`, `libcairo2`, `libgdk-pixbuf-2.0-0`.
- `chown www-data /app`, `USER www-data`.

#### 4.1.2 Laravel Dockerfile internals

- **Builder**: `docker-php-ext-configure gd` → `docker-php-ext-install pdo_pgsql pgsql zip gd pcntl sockets bcmath` → `pecl install swoole redis` (Swoole built with `--enable-swoole-pgsql` for async Postgres + `--enable-openssl`) → `docker-php-ext-enable swoole redis`.
- **Node 22** from `deb.nodesource.com/setup_22.x`; `composer install` then `npm ci --ignore-scripts` + `npm run build`.
- **Runtime**: matched extension stack + slimmed apt. `memory_limit=512M`.
- **Opcache** (`opcache-production.ini`): `enable=1`, `memory_consumption=256`, `interned_strings_buffer=16`, `max_accelerated_files=20000`, `validate_timestamps=0` (safe — image rebuilt on deploy), `save_comments=1`, `fast_shutdown=1`. Compose `command:` overrides `validate_timestamps=1` for dev hot-reload.

#### 4.1.3 backup-agent

Alpine + apt-get installs Postgres/Qdrant/Neo4j client tools + `mc` + `restic` + `curl` + `bash`. Non-root user `backup` (uid 1001). Cron driven externally by **Ofelia** via compose labels (§6.6).

### 4.2 Image registry + lifecycle

- **Registry**: `ghcr.io/${{ github.repository_owner }}/georag-{fastapi,laravel,dagster}`.
- **Tags**:
  - `:<short-sha>` — immutable, what `cd.yml` resolves and pulls.
  - `:main` — rolling latest on main.
- **Multi-arch**: linux/amd64 + linux/arm64. vLLM is x86_64-only — arm64 hosts can run dev (Ollama) but not prod LLM tier; see `ops/runbooks/llm-model-swap.md`.
- **Trivy + SBOM** — CRITICAL severity blocks, HIGH reported but non-blocking; SPDX-JSON SBOM 90-day artifact retention.
- **Compose-side tagging** — `georag/<service>:latest` refers to locally built dev images; CI registry tags drive prod deploys. Image-digest evidence: `ops/audit/2026-04-19-image-digests.json`.
- **Cosign image signing absent** — flagged ([`HANDOVER_INDEX.md`](HANDOVER_INDEX.md) §5.5).
- **GHCR retention policy** not inspected — flagged.

---

## 5. Test posture

### 5.1 Test environment

**Two PHPUnit configs**:
- `phpunit.xml` (default `php artisan test`) — `APP_ENV=testing`, `BCRYPT_ROUNDS=4`, `BROADCAST_CONNECTION=null`, `CACHE_STORE=array`, `DB_CONNECTION=sqlite`, `DB_DATABASE=:memory:`, `MAIL_MAILER=array`, `QUEUE_CONNECTION=sync`. In-memory SQLite for speed.
- `phpunit.pgsql.xml` — `DB_CONNECTION=pgsql`, `DB_HOST=postgresql`, `DB_PORT=5432`. For PostGIS-required tests (RLS, MVT functions).

**PHPStan**: `phpstan.neon` at level 6 + `phpstan-baseline.neon`. Excludes `HealthController` (×2), `MetricsController` (×5), `HorizonServiceProvider` (×1).

**Pint**: `pint.json` uses `laravel` preset + custom rules (ordered_imports alpha, no_unused_imports, no_useless_return/else, nullable_type_declaration_for_default_null_value, phpdoc_align left, phpdoc_separation/trim, trailing comma in multiline). Excludes `bootstrap/cache` + `storage`.

**Playwright** (`playwright.config.ts`): `testDir: ./tests/e2e`, `baseURL: E2E_BASE_URL ?? 'http://localhost:8888'`, Desktop Chrome default. Separate from Vitest jsdom suite.

**Vitest** (`vitest.config.ts`): `include: resources/js/**/*.test.{ts,tsx}`, `setupFiles: ./resources/js/test/setup.ts`.

### 5.2 Test inventory

**Laravel** (`tests/`): `Feature/`, `Unit/` — PHPUnit (parallel) gate. `Concerns/` — shared traits. `e2e/`, `fixtures/`, `golden_questions/`, `load_k6/` — out-of-band fixtures consumed by `e2e.yml` + `perf-baseline.yml`.

**Database** (`database/tests/pgtap/`) — 8 numbered files: `01_core_schema.sql`, `02_evidence_model.sql`, `03_rls_baseline.sql`, `08_silver_mvt_functions.sql`, `09_public_geoscience_mvt_functions.sql`, `10_golden_mvt_snapshots.sql` (depends on `seed_golden_fixture.sql`), `11_rls_workspace_isolation.sql`, `12_phase3_ocr_confidence.sql`. Plus `golden/` subdir + `seed_golden_fixture.sql` + `run.sh`.

**FastAPI + Dagster** — pytest with marker exclusions:
- Fast suite (PR-blocking): excludes `integration`, `golden`, `hallucination`, `live`, `chaos`.
- Release-rehearsal (tag-push): `integration | golden | hallucination | live`.
- Chaos: weekly cron in `chaos.yml`.

**Pytest markers** (`src/fastapi/pyproject.toml`):
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
markers = [
    "integration: requires the live Docker Compose stack",
    "golden: golden query set — failure blocks milestone acceptance",
    "hallucination: adversarial queries that must be refused",
    "live: requires Ollama/vLLM running with a loaded model",
    "chaos: chaos / resilience tests — scheduled-only",
]
```

### 5.3 Quality gates matrix

| Gate | Workflow / file | Blocking? |
|---|---|---|
| Pint | `ci.yml::laravel` | Yes |
| PHPUnit (parallel) | `ci.yml::laravel` | Yes |
| Ruff lint (FastAPI + Dagster) | `ci.yml::python-lint` | Yes |
| Pyproject imports cover | `ci.yml::python-lint` | Yes |
| LangGraph boundary check | `ci.yml::python-lint` | Yes |
| Qwen config drift | `ci.yml::qwen-config-drift` | Yes |
| FastAPI pytest (fast suite) | `ci.yml::python-test` | Yes |
| Dagster pytest (non-integration) | `ci.yml::python-test` | Yes |
| TypeScript `tsc --noEmit` | `ci.yml::frontend` | Yes |
| Vitest | `ci.yml::frontend` | Yes |
| Vite build | `ci.yml::frontend` | Yes |
| pgTAP MVT assertions | `ci.yml::pgtap` | Yes |
| Hadolint | `ci.yml::hadolint` | Warn-only |
| Docker build + Trivy CRITICAL | `ci.yml::docker-build` | Yes (CRITICAL only) |
| SBOM generation | `ci.yml::docker-build` | Soft (artifact) |
| Integration / golden / hallucination / live | `release-rehearsal.yml` | Tag-push only |
| Chaos | `chaos.yml` | Weekly cron |
| Tenant isolation auditor | `tenant-isolation-auditor.yml` | Cron / dispatch |
| Perf baseline | `perf-baseline.yml` | Cron / dispatch |
| E2E (Playwright) | `e2e.yml` | Per file (flagged) |

### 5.4 Locked-decision regression gates

Beyond the standard test markers, two CI tests enforce ADR contracts:
- `src/dagster/tests/test_reranker_locked_decisions.py` — pins an SME-labelled set; any LoRA bake that flips a label fails CI.
- `src/dagster/tests/test_reranker_uses_document_passages_canonical.py` — ADR-0010 contract: reranker label chain must read `silver.document_passages`, not `silver.ingest_extractions`.

---

## 6. Operations

### 6.1 Secrets management

- **Production env** — `.env.production.enc` (SOPS + age). Plaintext template at `.env.production.example` (140 keys, CHANGE_ME placeholders).
- **Decryption key** — `SOPS_AGE_PRIVATE_KEY` stored as a GitHub Environment secret per environment.
- **Rotation** — `docs/RUNBOOK.md` (PII decryption, APP_KEY rotation, shared-secret rotation procedures).

**Operator preflight** — `scripts/operator/preflight.sh` walks 8 gates:

| Gate | Check |
|---|---|
| O-01 | `SOPS_AGE_PRIVATE_KEY` GitHub Secret set |
| O-01b | `.sops.yaml` present with age recipient(s) |
| O-02 | `{ENV}_SSH_HOST` / `_USER` / `_KEY` trio set per environment |
| O-03 | `STAGING_URL` GitHub Secret set |
| O-04 | `.env.production.enc` present and SOPS-encrypted |
| O-05 | Cold-start runbook present (`ops/runbooks/cold-start.md`) |
| O-06 | Perf-baseline doc has values (not PENDING) |
| O-07 | Alertmanager prod template + actual prod config presence |

Companion: `scripts/operator/bootstrap-secrets.sh` (initial), `scripts/operator/set-github-secrets.sh` (bulk-load from SOPS-decrypted).

### 6.2 Database init sequence (cold start) + migration connection

Postgres init runs from `docker/postgresql/init/` on a fresh data volume:

1. `init-postgis.sql` — `postgis`, `postgis_topology`, `pg_trgm`, `uuid-ossp`, `pg_stat_statements`. Schemas: `bronze`, `silver`, `gold`, `index`, `audit`.
2. `init-roles.sql` — `georag_read` / `georag_write` / `georag_audit` roles + grants. **Placement gap** (`project_init_roles_gap`): lives outside auto-init dir in some configs; verify on each fresh cluster.
3. `init-test-db.sh` — provisions test database.
4. `10-phase0-extensions-and-schemas.sql` — `auto_explain`, `h3`, `h3_postgis`, `hypopg`, `pg_stat_kcache`, `pg_partman`, `pg_repack`, `pg_ivm`. Schemas: `partman`, `audit`, `usage`, `outbox`, `workflow`, `workspace`.
5. `20-hatchet-database.sql` — creates `hatchet` role + database.
6. `Z_activate_threadripper_tuning.sql` — Threadripper-Pro `ALTER SYSTEM SET` parallelism + I/O concurrency tunings.
7. `Z_activate_wal_archiving.sql` — paired with `compose.wal-archiving.yml` overlay.

`shared_preload_libraries='pg_stat_statements,auto_explain,pg_stat_kcache'` is mandatory at boot (compose `command:` `-c` flag). Changes require Postgres restart, not SIGHUP.

**Migration connection contract**: Laravel ships two PG connections in `config/database.php`:
- `pgsql` — runtime via PgBouncer (`DB_HOST=pgbouncer`, `DB_PORT=6432`). Used by Eloquent.
- `pgsql_migrations` — direct to Postgres (`MIGRATE_DB_HOST` / `POSTGRES_DIRECT_HOST`). Used **only for DDL** because PgBouncer transaction-pool rotates backends mid-DDL.

All `artisan migrate` in CD must pass `--database=pgsql_migrations`. **`cd.yml` does NOT call this** — flagged ([`HANDOVER_INDEX.md`](HANDOVER_INDEX.md) §5.5).

### 6.3 Hatchet worker-pool selection + engine compose env

Workers boot via `python -m app.hatchet_workflows.worker` with `WORKER_POOL` env (`ingestion | ai | all`):
- `hatchet-worker-ingestion` → `WORKER_POOL=ingestion`.
- `hatchet-worker-ai` → `WORKER_POOL=ai`.

Pass `--list` at boot to print registered workflow names without engine connection (useful for CI smoke).

Worker pool memberships listed in [`SAD.md`](SAD.md) §4.5 + manifest §4.

**Hatchet engine** (`hatchet-lite`) env (`docker-compose.yml`):

| Env var | Value |
|---|---|
| `DATABASE_URL` | `postgres://hatchet:${HATCHET_DB_PASSWORD}@postgresql:5432/hatchet?sslmode=disable` |
| `SERVER_MSGQUEUE_KIND` | `postgres` — same Postgres cluster handles the message queue itself |
| `SERVER_DEFAULT_ENGINE_VERSION` | `V1` |
| `SERVER_AUTH_COOKIE_INSECURE` | `t` (dev — flip prod) |
| `SERVER_GRPC_BIND_ADDRESS` | `0.0.0.0` |
| `SERVER_GRPC_INSECURE` | `t` (paired with worker `TLS_STRATEGY=none`; flip prod) |
| `SERVER_GRPC_BROADCAST_ADDRESS` | `localhost:${HATCHET_GRPC_PORT:-7077}` |
| `SERVER_GRPC_PORT` | `7077` |
| `SERVER_URL` | `http://localhost:${HATCHET_API_PORT:-8889}` |
| `SERVER_AUTH_SET_EMAIL_VERIFIED` | `t` (dev; flip prod) |
| `SERVER_INTERNAL_CLIENT_INTERNAL_GRPC_BROADCAST_ADDRESS` | `hatchet-lite:7077` |

**Production hardening checklist**: 3 INSECURE flips. Flagged in [`HANDOVER_INDEX.md`](HANDOVER_INDEX.md) §5.8.

### 6.4 Hatchet workflow runtime semantics

- **`parents=[…]`** — child only enqueued after parent task success.
- **`execution_timeout`** — wall-clock budget for task function (asyncio.wait_for); cancels on exceed.
- **`schedule_timeout`** — engine-queue tolerance; default `2h` for ingest_pdf tasks.
- **`retries`** — counts per task: initial + retries.
- **`on_crons=[…]`** — fires workflow with empty input; `input_validator` must accept empty dict. `eval_real_rag_nightly` wraps `evaluate_workspace` because the latter requires non-empty `eval_request_id`.
- **Idempotency keys** — R2+ agent calls deduplicated via `workspace.idempotency_keys` (per agent_name + workspace + input hash). Replay returns stored result.
- **Heartbeat / cancellation** — workers heartbeat to engine; missed heartbeats → re-dispatch. Sync code blocking event loop → missed heartbeats → ping-pong (the `LOG_LEVEL=debug` + pdfminer flood incident).
- **Three independent guardrails**: idempotency (caller-driven, in-PG), concurrency (engine-driven slot cap), cancellation (worker-driven via `asyncio.wait_for`). Do not conflate.

### 6.5 Dagster schedules + sensor

| Name | Cron | Purpose |
|---|---|---|
| `full_ingest_schedule` | `0 2 * * *` | Daily 02:00 UTC — full bronze→silver→gold→index materialization |
| `silver_dq_daily_schedule` | `0 4 * * *` | Daily 04:00 UTC — silver DQ checks via `dq_writer` |
| `public_geoscience_weekly_refresh` | `0 3 * * 0` | Sundays 03:00 UTC |
| `smdi_deposits_daily_refresh` | `30 3 * * *` | Daily 03:30 UTC |
| `silver_chat_cards_backfill_schedule` | `*/30 * * * *` | Every 30 min |
| `public_geoscience_daily_edit_check` | `30 5 * * *` | Daily 05:30 UTC |

Sensor: `minio_upload_sensor` — polls SeaweedFS bronze bucket every 5 min for new objects.

### 6.6 Ofelia backup cron (compose labels)

Cron lives on `backup-agent` service labels:

| Job | Schedule | Equivalent | Script |
|---|---|---|---|
| `pg-backup` | `0 30 2 * * *` | Daily 02:30 UTC | `/backup-scripts/postgresql/backup.sh` (pg_basebackup) |
| `pg-wal-upload` | `@every 5m` | Every 5 min | `/backup-scripts/postgresql/wal-upload.sh` |
| `qdrant-backup` | `0 0 3 * * *` | Daily 03:00 UTC | `/backup-scripts/qdrant/backup.sh` |
| `neo4j-backup` | `0 0 3 * * 0` | Sundays 03:00 UTC only | `ALLOW_WEEKLY_DUMP=1 /backup-scripts/neo4j/backup.sh` — Neo4j stops ~75–120s |

Neo4j backups require explicit `ALLOW_WEEKLY_DUMP=1` env gate to prevent accidental downtime.

### 6.7 Hatchet cron-triggered workflows (30 declarations)

| Workflow | Cron |
|---|---|
| `audit_ledger_verify` | `0 2 * * *` |
| `backup_postgres` | `0 2 * * *` |
| `nightly_ingestion_integrity` | `0 2 * * *`, `0 4 * * *` |
| `repair_shadow_aggregate` | `15 2 * * *` |
| `backup_neo4j` | `15 2 * * *` |
| `backup_qdrant` | `30 2 * * *` |
| `backup_redis` | `45 2 * * *` |
| `backup_seaweedfs` | `0 3 * * *` |
| `mv_refresh_silver` | `0 3 * * *` |
| `cold_tier_archive` | `0 4 * * *` |
| `flow_jwt_key_reaper` | `0 4 * * *` |
| `idempotency_keys_cleanup` | `15 4 * * *` |
| `eval_real_rag_nightly` | `15 5 * * *` |
| `embed_pending_passages` | `45 5 * * *`, `*/10 * * * *` |
| `sync_silver_to_kg` | `30 5 * * *` |
| `what_changed_weekly` | `0 6 * * 1` (Mondays 06:00 UTC) |
| `outbox_dispatcher` | `* * * * *` (every minute) |
| `cost_burn_watcher` | `*/5 * * * *` (every 5 min) |
| `reliability_metrics_publisher` | `* * * * *` (every minute) |
| `shadow_diff` | `* * * * *` |
| `stale_run_detector` | `*/15 * * * *` (every 15 min) |
| `phase0_agents` (8 sub-workflows) | `0 1 * * *`, `0 2 * * *`, `30 2 * * *`, `0 3 * * *`, `0 4 * * *`, `0 5 * * *`, `0 6 * * *`, `0 */6 * * *` |

### 6.8 Dagster asset checks (27 across 6 files)

| File | Count | Samples |
|---|---|---|
| `silver_checks.py` | 16 | `silver_collars_check_collar_count_positive`, `silver_collars_check_schema_conformance`, `silver_collars_check_crs_srid_populated`, `silver_surveys_check_parse_total_positive` |
| `evidence_checks.py` | 5 | Evidence-model coverage gates |
| `index_checks.py` | 2 | Qdrant + Neo4j integrity |
| `interval_overlap_checks.py` | 2 | Drill-interval overlap detection |
| `drill_traces_checks.py` | 1 | Drill-trace LineString continuity |
| `__init__.py` | 1 | Aggregate registration |

Failures route to Dagster's check-result UI and feed `WorkspaceDataUpdatedEmissionSlow` + `EmbedPendingPassagesStuck` alert families.

### 6.9 Alert routing

`docker/alertmanager/alertmanager.yml` defines 3 receivers:

- `critical-webhook` → `http://localhost:9999/alertmanager/critical`
- `warn-webhook` → `http://localhost:9999/alertmanager/warn`
- `dev-null` (empty `webhook_configs[]`) — drops matched alerts

Dev URLs above are placeholders. Production template at `docker/alertmanager/alertmanager.production.yml.example` — operator-populated PagerDuty / Kestra ingestion endpoint.

**Caddy → Kestra SSO bridge**: Caddy fronts Kestra at `:8087` (HTTP) + `:8443` (HTTPS, internal CA or ACME via `CADDY_TLS_ISSUER`). Per-request `forward_auth` to `laravel-octane:80/internal/sanctum/check`; Laravel's `KestraSsoCheckController` validates session/PAT + admin Gate + returns `X-Kestra-Auth: Basic <base64>`. Detailed endpoint contract in [`API_DOCUMENTATION.md`](API_DOCUMENTATION.md) §6.3. In-app SSO forward: `ANY /admin/integrations/kestra/{path?}`.

### 6.10 Per-flow JWT rotation

Per-flow JWT for Kestra→FastAPI integration triggers:
- Secret: `KESTRA_FLOW_JWT_SECRET`.
- KV-stored per flow (e.g. `flow_jwt_external_notification`).
- Rotated by `scripts/phase3_jwt_rotate.sh`.
- Expired keys reaped by `flow_jwt_key_reaper` Hatchet workflow (04:00 UTC).

HMAC sender contract (external_notification): canonical-JSON `{notification_id, source, kind, payload, received_at}` → HMAC-SHA256 hex with `EXTERNAL_NOTIFICATION_HMAC_SECRET`. See [`API_DOCUMENTATION.md`](API_DOCUMENTATION.md) §8.2.

### 6.11 Operator scripts

**`scripts/operator/`** — CI/CD-facing:
- `preflight.sh` — read-only 8-gate check (§6.1).
- `bootstrap-secrets.sh` — initial secret provisioning.
- `set-github-secrets.sh` — bulk-load from SOPS-decrypted file.

**`ops/setup/`** — hands-on operator scripts (not part of CD):
- `apply_n8n_langfuse_secrets.sh` — Langfuse + n8n secret provisioning.
- `apply_v1_14_env_tuning.sh` — release env-knob delta.
- `bump_postgres_limits.py` — PG limits recomputer for hardware refresh.
- `fix-gpu-passthrough.sh` — NVIDIA Container Toolkit fixup.
- `sync_windows_to_wsl.sh` — **DEPRECATED** per `project_wsl_clone_retired_2026_05_23`.
- `verify-gpu.sh` — `nvidia-smi` + CUDA-version sanity check.

**`ops/runbooks/`** — 38 scenario-specific procedures. Inventory in [`HANDOVER_INDEX.md`](HANDOVER_INDEX.md) §3.4. Notable for CD operations:
- `cold-start.md` — first-deploy procedure.
- `deploy-rollback.md` — rollback procedure.
- `secret-management.md` / `secret-rotation.md` — SOPS + age procedures.
- `migration-rollback.md` — DB schema rollback.
- `dr-1-postgres-loss.md` … `dr-5-partial-outage.md` — disaster recovery scenarios.

### 6.12 Evidence artifacts

Frozen-reality sidecars for drift investigation:

**`ops/baselines/`** — datastore + docker stats CSVs, PG before/after tuning, API latency, parallel dispatch, capacity-planning.md.

**`ops/audit/`** — `2026-04-19-image-digests.json` (pinned image SHAs), infra audit + inventory + critical-fixes, resolved-compose snapshots (`docker compose config` output), datastore audit + config, ingestion asset graph.

Operators reference these when investigating drift between live state and frozen evidence.

### 6.13 Airgap install path

`airgap/install.sh` — §11.8 single-command K3s installer:

```
./install.sh [--namespace georag]
             [--registry my.registry.local/georag]
             [--secrets-file path/to/secrets.env]
```

5-step procedure: detect K3s containerd vs Docker runtime → load every image in `images/*.tar` → re-tag with private-registry prefix (default `registry.internal.local/georag/`) → `helm install` bundled `.tgz` → status summary.

Tarball built by `build_airgap_bundle.sh` from `values-airgap.yaml` overlay.

---

## 7. Local / dev workflow

- **`composer run dev`** — concurrent `php artisan serve` + `queue:listen --tries=1 --timeout=0` + `php artisan pail --timeout=0` + `npm run dev` (colour-coded via `concurrently`).
- **`composer run test`** — `config:clear` then `artisan test`.
- **`composer run lint`** / `lint:fix` — Pint.
- **`composer run stan`** / `stan:baseline` — PHPStan with 2G memory.
- **`composer run php:check`** — composite: lint → stan → test.
- **`composer run pgtap`** — `bash database/tests/pgtap/run.sh`.
- **`composer run pgtap-silver`** / `pgtap-pgeo` — subset filters.
- **`npm run build`** — Vite build (with `scripts/guard-build-perms.sh` preflight).
- **`npm run test`** — `vitest run`.
- **`npm run typecheck`** — `tsc --noEmit`.
- **`php artisan octane:reload`** — required after every `vite build` because Octane caches the Vite manifest in memory (`feedback_octane_vite_reload`).

---

## 8. Needs Confirmation

Consolidated ledger owned by [`HANDOVER_INDEX.md`](HANDOVER_INDEX.md) §5. Items relevant to CI/CD, secrets, container build, test posture, observability routing, operator procedures, and orchestrator scheduling are aggregated there.

---

*End of `CICD_PIPELINE.md`.*
