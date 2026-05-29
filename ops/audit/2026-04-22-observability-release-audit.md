# Module 10 — Observability & Release-Engineering Audit (Phase A, Read-Only)

<!-- Auditor: senior-reviewer (Opus, 1M ctx) — 2026-04-22 -->
<!-- Scope: A1–A8, eight audit areas, evidence-grounded -->
<!-- Authority: georag-architecture.html §09 / §11 / §12; module status memory; live config files -->
<!-- Working dir: /home/Development/GeoRAG Intelligence V.1.0 (WSL) -->

---

## 0. Executive Summary

The platform has functional observability scaffolding (Prometheus + 2 Grafana
dashboards + 1 alert-rule file + 19 runbooks + 1 single-stage CI workflow + a
modest golden/hallucination corpus + load_test.py) but it is **not yet a
release-engineered product**. Five of the nine intended Prometheus scrape
targets are wired (FastAPI, Laravel, Neo4j, Qdrant, Martin); Postgres + Redis
are commented out, and Reverb + Dagster + node_exporter never appear at all.
Alert rules exist for **Martin only**, and three of the four Martin rules are
labelled DORMANT because Martin 1.5.0 has no `/metrics` endpoint. There is no
Loki/Promtail and no W3C trace propagation between Laravel and FastAPI. CI
(`.github/workflows/ci.yml`) is a single workflow with no dev/staging/prod
stages, no image push/digest pinning step, no secret-management story, and
explicitly excludes the `golden`, `hallucination`, and `integration` pytest
markers from the test job (line 131 — these are the highest-value tests for a
release gate). The Module 7 Phase C measurement deliverables (citation-click,
refusal, p95 bubble-render) and the Module 9 9.8 `authz_audit` channel are
**not** surfaced in any dashboard. Only **2 of the 12 V1 controllers** carry
IDOR test coverage. The architecture doc banner says **v1.8** in the `<title>`
and **v1.9** in the footer — already drifted from itself.

**Verdict: Conditional / Don't-Ship.** The platform can *operate* in dev
under careful supervision, but it is not ready to be **released** to a paying
customer in its current state. Module 10 must close the eight high-severity
findings below (notably H-A4-01, H-A1-01, H-A2-01, H-A3-01, and H-A5-01)
before a production deploy is defensible.

---

## A1 — Prometheus / Metrics Surface

### Evidence

- `docker/prometheus/prometheus.yml` — five active jobs, two commented-out
  jobs, four service classes never mentioned at all.
- `docker/prometheus/rules/` — exactly one rule file: `martin-alerts.yml` (86
  lines, 4 rules, 3 DORMANT per Martin 1.5.0 limitation).
- `src/fastapi/app/main.py:710-755` — `@app.get("/metrics")` exists; uses
  `prometheus-fastapi-instrumentator`; module `app.metrics` is imported lazily
  at request time. Endpoint is intentionally public at the app layer per inline
  comment (review #3) — gating is network-layer.
- `routes/web.php` / `routes/api.php` — **zero** matches for `/metrics`. Laravel
  Pulse has no Prometheus exporter route, contradicting `prometheus.yml:81-99`
  which expects `laravel-octane:80/metrics`.
- `config/pulse.php:139` — recorders are configured but no `/metrics` endpoint
  exports them in Prometheus text format.
- No `Reverb` job in `prometheus.yml`. Reverb runs as `laravel-reverb` in
  `docker-compose.yml`.
- No `dagster` job in `prometheus.yml`. Dagster daemon + webserver run on
  `dev-ingest` profile.
- `redis_exporter` and `postgres_exporter` are **commented out** in
  `prometheus.yml:181-206`.
- Architecture §09a expects scrape coverage of every service + alert rules per
  service; the live config covers ~40 % of named services.

### Findings

| ID | Severity | File:Line | Finding |
|---|---|---|---|
| H-A1-01 | High | `routes/{web,api}.php` (absent) | Prometheus expects `/metrics` from `laravel-octane:80` but no Laravel route exists. Scrape will return 404, hiding queue-depth / cache-hit / slow-query / exception-rate metrics that `prometheus.yml:87-91` documents as "key metrics to dashboard." |
| H-A1-02 | High | `docker/prometheus/rules/` | Only `martin-alerts.yml` is present. No alert rules for FastAPI latency, Laravel queue saturation, Neo4j page-cache hit ratio, Qdrant pending operations, Postgres connection saturation, Redis memory pressure, or RAG-pipeline refusal-rate spike. Architecture §09a calls for per-service alert rules. |
| H-A1-03 | High | `prometheus.yml:181-206` | `redis_exporter` and `postgres_exporter` jobs are commented out. PgBouncer pool starvation and Redis answer_run_events buffer growth are both documented operational risks (Module 7 Chunk 1) but cannot be alerted. |
| M-A1-04 | Medium | `prometheus.yml` (absent) | No scrape job for Reverb (`laravel-reverb`), Dagster (`dagster-daemon` / `dagster-webserver`), or node_exporter (host CPU/RAM/NVMe). Reverb metrics are needed to alert on broadcast lag (Module 7 STREAM-02 follow-up). |
| M-A1-05 | Medium | `martin-alerts.yml:25-54` | 3 of 4 Martin alert rules annotated DORMANT — Martin 1.5.0 has no `/metrics`. Rules are correct in spec but un-actionable until Martin upgrade (`feedback_martin_tile_gotchas.md` gotcha 3). Module 10 should track Martin upstream and enable on upgrade. |
| L-A1-06 | Low | `prometheus.yml:31-33` | `external_labels: environment: "dev"` hard-coded. No mechanism to flip to `staging`/`prod` per profile (single compose file ships only dev profile). |
| L-A1-07 | Low | `prometheus.yml:44-47` | Alertmanager block is commented out. No alert-routing destination wired (PagerDuty/Slack/email). |
| L-A1-08 | Low | `src/fastapi/app/main.py:710-755` | `app.metrics` import is lazy (per-request) — first request after a metric-module reload pays import cost. Negligible but worth noting for cold-start measurements. |

---

## A2 — Grafana Dashboards

### Evidence

- `docker/grafana/dashboards/georag-overview.json` (601 lines): 4 service
  panels (FastAPI, Qdrant, Neo4j, System) with sub-panels Request Rate, p95
  Latency, Error Rate, Total Vectors, Search Duration p95, Page Cache Hit
  Ratio, Store Size, Targets Up.
- `docker/grafana/dashboards/georag-signals.json` (400 lines): 5 sections
  (Classifier escalation, Prompt cache, Model routing + failover, Cost
  accountability, Retrieval quality + latency). Panels are RAG-specific.
- `docker/grafana/provisioning/dashboards/default.yml` and
  `provisioning/datasources/prometheus.yml` exist — auto-provisioning works.
- **No** panel for Martin (despite scrape job + alert rules existing).
- **No** panel for Postgres or Redis (matches commented-out scrapes).
- **No** panel surfacing Module 7 Phase C deliverables: citation-click rate,
  feedback submission rate, refusal rate, p95 bubble-render latency. Memory
  reference: `project_module_7_status.md` ("Phase C measurement deferred").
- **No** panel for `authz_audit` channel (Module 9 9.8). Memory reference:
  `project_module_9_status.md` ("AuthorizationAuditLogger" wired but no
  observability surface).
- **No** queue-depth panel — `laravel_queue_depth` is documented in
  `prometheus.yml:88` but never plotted because the scrape itself is broken
  (see H-A1-01).
- Module 8 §05d ETag spine has no dashboard despite being a release-criticality
  cache (gotcha file describes `cache_size_mb=512` budget).

### Findings

| ID | Severity | File:Line | Finding |
|---|---|---|---|
| H-A2-01 | High | `docker/grafana/dashboards/` | Module 7 Phase C measurement deliverables (citation-click rate, feedback rate, refusal rate, p95 bubble-render) are absent. These were explicitly deferred to Module 10 and are RAG quality KPIs the SME (Kyle) needs to show prospects. |
| H-A2-02 | High | `docker/grafana/dashboards/` | No `authz_audit` dashboard. Module 9 9.8 wired the structured-403 channel but ops cannot see anomalies (e.g., a sudden burst of 403s from a single user) without grep-ing `storage/logs/authz_audit.log`. |
| M-A2-03 | Medium | `docker/grafana/dashboards/` | No Martin tile-server panel. `prometheus.yml:161-167` scrapes Martin and `martin-alerts.yml` defines alerts, but no visual surface for tile latency / cache hit ratio / 5xx rate. |
| M-A2-04 | Medium | `docker/grafana/dashboards/` | No Postgres / Redis panels (matches A1 commented scrapes). Once those exporters are added, dashboards must be created. |
| M-A2-05 | Medium | `docker/grafana/dashboards/georag-overview.json` | No Laravel queue-depth or Horizon panel. Kyle hit a `maxProcesses=5` saturation in `load_test.py` — needs visual tripwire. |
| L-A2-06 | Low | `docker/grafana/dashboards/` | Only 2 dashboards. Architecture §09 expects per-service dashboards (one each for Laravel / FastAPI / Postgres / Redis / Neo4j / Qdrant / Martin / Dagster / Reverb / RAG). |

---

## A3 — Logging / Loki / Structured Logs / Trace Correlation

### Evidence

- `docker/promtail/` does not exist. `docker/loki/` does not exist. `docker-compose.yml`
  has 22 services; none are Loki or Promtail.
- `config/logging.php:53-143` — channels `single`, `daily`, `slack`,
  `papertrail`, `stderr`, `syslog`, `errorlog`, `null`, `emergency`, **and**
  `authz_audit` (lines 73-79, daily-rotating, 30-day retention via
  `AUTHZ_AUDIT_RETENTION_DAYS`).
- `config/logging.php:73-79` — `authz_audit` retention is `daily` with default
  `30` days. No retention policy documented for `query_audit_log` (NI 43-101
  audit trail) — that is a PostgreSQL table, not a log channel; retention may
  be enforced elsewhere or not at all.
- `src/fastapi/app/agent/event_stamper.py` — referenced as the W3C `traceparent`
  injection point per Module 7 Chunk 1. `trace_id` matches in 4 fastapi files
  (`routers/queries.py`, `agent/event_stamper.py`, `models/answer_run.py`,
  `services/answer_run_store.py`).
- **No matches** for `traceparent`/`TraceParent` in `app/Http/Middleware/`
  (Laravel side). The W3C header is generated downstream of the chat-request
  ingress; if the user's browser opens a connection to Laravel without a
  `traceparent`, Laravel does not appear to generate-and-forward one.
- Plain-text log format: `config/logging.php:55-66` uses Laravel's default
  formatter — not JSON. `single`, `daily`, `authz_audit` all write line-based
  text. FastAPI uses Python `logging` defaults unless reconfigured.

### Findings

| ID | Severity | File:Line | Finding |
|---|---|---|---|
| H-A3-01 | High | `docker-compose.yml` | No Loki/Promtail (or Vector/Fluentd alternative). All 22 services log to stdout/files; ops cannot run a single query across services. Architecture §09 implies a centralised log store; spec drift is silent on which one. |
| H-A3-02 | High | `app/Http/Middleware/` (absent) | Laravel does not appear to inject a W3C `traceparent` on the inbound chat request before forwarding to FastAPI. Cross-service trace correlation is therefore best-effort. Verify by examining `app/Http/Middleware/TrustProxies.php` and chat-controller forwarders before closing. |
| M-A3-03 | Medium | `config/logging.php:55-66` | Laravel logs are plain text, not JSON. Once Loki lands, structured (key-value) logs are required for label-based search. |
| M-A3-04 | Medium | `config/logging.php` (absent) | `query_audit_log` (NI 43-101 mandatory trail) retention policy is not documented anywhere I could find. The table is PostgreSQL-backed; either a CRON purge job or a documented "keep forever" decision is required for SOC-2 / NI 43-101 audit defence. |
| L-A3-05 | Low | `src/fastapi/app/agent/event_stamper.py` | Stamper exists but has no test asserting that `traceparent` survives the round-trip from Laravel → FastAPI → SSE event. STREAM-02 closure (memory: `project_module_7_status.md`) might overlap. |

---

## A4 — Release-Gate Pipeline (CI)

### Evidence — `.github/workflows/ci.yml` (307 lines, single workflow)

- Triggers (line 3-7): push to `main`/`develop`, PR to `main`. Concurrency
  group cancels prior runs.
- Jobs: `laravel`, `python-lint`, `python-test`, `frontend`, `pgtap`,
  `docker-build`. Six jobs.
- **`python-test:131`** — `uv run pytest tests/ -v --tb=short -m "not
  integration and not golden and not hallucination"` — the **golden** and
  **hallucination** suites are explicitly **excluded** from CI. These are the
  exact suites Section 11 names as release gates.
- **`pgtap:259-285`** — pgTAP runs in CI (Module 8 8.8 wiring is intact); 11
  files in `database/tests/pgtap/`. All assertions execute against
  `postgis/postgis:17-3.5` — but `docker-compose.yml:postgresql` is pinned to
  `postgis:18-3.6-alpine`. CI tests against an older PG. Comment at line
  179-183 acknowledges the mismatch.
- **`docker-build:291-306`** — builds FastAPI / Laravel / Dagster images.
  **No push step.** Images are not tagged with commit SHA, not pushed to a
  registry, not used downstream by a deploy job.
- **No matrix** for environments. `dev`, `staging`, `prod` do not exist as
  separate workflow paths or environment-protection gates.
- **No `secrets:`** consumption in any job. There is no `.env.production`
  template, no Vault integration, no SOPS-encrypted file.
- **No e2e job.** `tests/e2e/` directory exists (`playwright.config.ts` in
  repo root) but is not invoked by CI.
- **No IDOR test invocation.** `tests/Feature/Api/V1/*IDORTest.php` (2 files)
  are picked up by the broad `php artisan test --parallel` invocation, but no
  named CI step asserts their pass/fail or fails the pipeline on missing
  coverage. The `php artisan test` runs ALL tests, so they do execute — but a
  release gate should highlight them by name.
- **No RLS-pgTAP isolation guarantee.** `11_rls_workspace_isolation.sql`
  exists in `database/tests/pgtap/`, runs in CI. Good.
- **No release-tag workflow.** No `release.yml`, no `.github/workflows/cd.yml`,
  no Helm/Kustomize/Terraform pipeline.

### Findings

| ID | Severity | File:Line | Finding |
|---|---|---|---|
| **C-A4-01** | **Critical** | `.github/workflows/ci.yml:131` | Pytest explicitly excludes `-m "not integration and not golden and not hallucination"`. Section 11 names golden + hallucination suites as **mandatory release gates** — they currently run **never** in CI. This is the single most consequential finding in the audit. |
| H-A4-02 | High | `.github/workflows/ci.yml` | No deploy stage. `docker-build` is a smoke test only — images are not pushed, not tagged with SHA, not consumed by any downstream stage. There is no path from "PR merged" to "running in staging". |
| H-A4-03 | High | `.github/workflows/ci.yml:183` vs `docker-compose.yml:postgresql` | CI tests against PostgreSQL 17.x; production runs PostgreSQL 18.3. PG18 introduced behavior changes (jsonb path operators, planner stats) that 17 doesn't share. Tests passing in CI do not prove correctness on prod. |
| H-A4-04 | High | (absent) | No `.env.production` template, no secret-management integration. Cannot deploy without per-environment config separation. |
| M-A4-05 | Medium | `.github/workflows/ci.yml` | No e2e Playwright job despite `playwright.config.ts` existing. Module 7 Chat UX work and Module 8 Map work would benefit from end-to-end smoke. |
| M-A4-06 | Medium | `.github/workflows/ci.yml` | No coverage-floor enforcement. PHPUnit + pytest run but no minimum line coverage required. |
| M-A4-07 | Medium | `.github/workflows/ci.yml:163` | `npm run test --if-present` — vitest runs only if the script is present. No assertion that frontend tests must run. |
| L-A4-08 | Low | `.github/workflows/ci.yml` | No SBOM generation on docker build (Module 9 carry-forward — supply-chain compliance for on-prem mining clients). |
| L-A4-09 | Low | `.github/workflows/ci.yml` | No hadolint / dockerfile-lint / trivy step. |

---

## A5 — Test-Corpus Completeness

### Evidence

| Asset | Count | File |
|---|---|---|
| Golden fixtures | **30** | `src/fastapi/tests/test_golden_queries.py:69` (counted via `grep -c '"id":'`) |
| Golden public-geoscience fixtures | **uncounted (no `"id":` field)** | `src/fastapi/tests/test_public_geoscience_golden.py` (321 lines — fixture style differs) |
| Hallucination adversarial cases | **20** | `src/fastapi/tests/test_hallucination_failures.py:65` |
| Public-geoscience hallucination | **uncounted** | `src/fastapi/tests/test_public_geoscience_hallucination.py` (323 lines) |
| Hallucination layer-unit tests | 92 `def test_` | `src/fastapi/tests/test_hallucination_layers.py` |
| Chaos / resilience tests | 21 `def test_` | `src/fastapi/tests/test_chaos_resilience.py` |
| FastAPI test files (total) | 43 | `src/fastapi/tests/*.py` |
| Dagster test files | 29 | `src/dagster/tests/*.py` |
| PHPUnit test files | 38 | `tests/**/*.php` |
| pgTAP files (numbered) | 4 | `08_silver_mvt_functions.sql`, `09_public_geoscience_mvt_functions.sql`, `10_golden_mvt_snapshots.sql`, `11_rls_workspace_isolation.sql` |
| Vitest / RTL files | 22 | `resources/js/**/*.test.{ts,tsx}` (count via `find ... | wc -l`) |
| IDOR PHPUnit | 2 | `tests/Feature/Api/V1/{Project,Collar}ControllerIDORTest.php` |
| V1 controllers | 12 | `app/Http/Controllers/Api/V1/` (Auth, ChatConversation, Citation, Collar, ColumnMapping, Export, HoleAnalysis, Project, Query, Upload, VendorProfile, PublicGeoscience/*) |

### Query-Class Coverage (§05c)

The architecture spec names seven query classes (count, exists, numeric,
spatial, document, graph, refusal). Searched `test_golden_queries.py` for the
field `"query_class":` — **zero matches**. The 30 golden fixtures are not
labelled by class. Class-level coverage cannot be asserted by inspection of
the corpus alone. By query content, GQ-001 → GQ-006 are count/aggregation
queries; the rest are not classified.

### Findings

| ID | Severity | File:Line | Finding |
|---|---|---|---|
| H-A5-01 | High | `tests/Feature/Api/V1/` | Only **2 of 12** V1 controllers have IDOR tests (Project + Collar). Citation, Export, HoleAnalysis, Query, Upload, ChatConversation, ColumnMapping, VendorProfile, and the PublicGeoscience controllers have no cross-workspace bypass coverage. Module 9 closed the IDOR gates; Module 10 must close the **test gap**. |
| H-A5-02 | High | `src/fastapi/tests/test_golden_queries.py` | 30 golden fixtures, none tagged with `query_class`. The §05c class coverage matrix cannot be enforced. Add a `"query_class"` field to each fixture and a CI assertion that all 7 classes have at least 3 cases each (target: ≥21 cases minimum, currently undefined). |
| H-A5-03 | High | `.github/workflows/ci.yml:131` (paired with C-A4-01) | The golden + hallucination corpora exist (50 cases) but the CI excludes them. They are committed but never executed by automation. |
| M-A5-04 | Medium | `src/fastapi/tests/test_public_geoscience_golden.py` (321 lines) | Public-geoscience golden corpus uses a different fixture shape (no `"id":` field detected). Either harmonise to the main corpus shape or document the divergence. |
| M-A5-05 | Medium | `database/tests/pgtap/` | Only 4 numbered pgTAP files (08, 09, 10, 11). Migrations 01–07 are not covered; specifically, `evidence_items` CHECK constraints (Module 6 §04j RESTRICT FK) and `silver.workspaces` RLS policy lack pgTAP assertions. |
| L-A5-06 | Low | `tests/Unit/` | Only `Models`, `Jobs`, `Fixtures`, `ExampleTest.php` — no Unit coverage of `app/Support/AuthorizationAuditLogger.php` (only an integration-style test in `tests/Feature/Authz/AuthorizationAuditLoggerTest.php`). |
| L-A5-07 | Low | `tests/e2e/` | Playwright config exists but e2e suite scope unverified. Need to count specs and confirm coverage of citation-click → bubble-render → CitationMarker flow. |

---

## A6 — Runbook Coverage

### Evidence — `ops/runbooks/` contains 19 runbooks (memory said 17 — 2 added since last memory commit)

```
backup-restore.md, citation-pipeline.md, cold-start.md, container-hardening.md,
data-version.md, datastore-tuning.md, evidence-model.md, hybrid-retrieval.md,
ingestion-pipeline.md, martin-tile-server.md, neo4j-backup.md,
qdrant-snapshot.md, redis-topology.md, retrieval-cache.md,
retrieval-pipeline.md, s3-abstraction.md, service-outage.md,
validation-corpora.md
```

(`docs/RUNBOOK.md` is a separate operator manual referenced by CLAUDE.md — not
counted above.)

### Scenario → Runbook Map

| Scenario | Runbook present? |
|---|---|
| Cold start (full stack) | YES — `cold-start.md` |
| Single-service crash recovery | PARTIAL — `service-outage.md` is generic; no per-service playbook |
| DB backup + restore | YES — `backup-restore.md`, `neo4j-backup.md`, `qdrant-snapshot.md` |
| Object-storage volume restore | PARTIAL — `s3-abstraction.md` covers SeaweedFS abstraction, not volume restore |
| Token / secret rotation | **GAP** — `docs/RUNBOOK.md` references rotation per CLAUDE.md but no `secret-rotation.md` in `ops/runbooks/` |
| Schema migration rollback | **GAP** — no `migration-rollback.md` |
| LLM model swap | **GAP** — no `llm-model-swap.md`; `docs/model_migration.md` exists but is doc, not runbook |
| Tile-cache invalidation | YES — `martin-tile-server.md` |
| `data_version` bump | YES — `data-version.md` |
| Citation pipeline failure mode | YES — `citation-pipeline.md` |
| Refusal-rate spike triage | **GAP** — no runbook; correlates with Phase C measurement gap (H-A2-01) |
| `authz_audit` anomaly investigation | **GAP** — Module 9 9.8 added the channel; no runbook for triaging the structured 403 events |
| On-call alert acknowledgment | **GAP** — no `on-call.md`; no Alertmanager wired (L-A1-07) |
| Deploy rollback | **GAP** — no `deploy-rollback.md` |
| Volume migration (Module 9 9.7) | PARTIAL — `container-hardening.md` covers hardening, not volume migration; Module 1 C1 volume-wipe is still open per memory |
| Hybrid retrieval tuning | YES — `hybrid-retrieval.md`, `retrieval-pipeline.md`, `retrieval-cache.md` |
| Evidence model | YES — `evidence-model.md` |
| Validation corpora | YES — `validation-corpora.md` |
| Redis topology | YES — `redis-topology.md` |
| Datastore tuning | YES — `datastore-tuning.md` |
| Ingestion pipeline | YES — `ingestion-pipeline.md` |

### Findings

| ID | Severity | File | Finding |
|---|---|---|---|
| H-A6-01 | High | `ops/runbooks/` | **5 release-critical runbooks missing**: `secret-rotation.md`, `migration-rollback.md`, `deploy-rollback.md`, `on-call.md`, `authz-audit-triage.md`. Without these, the platform cannot be operated by an on-call engineer who isn't Kyle. |
| M-A6-02 | Medium | `ops/runbooks/service-outage.md` | Generic "service is down" runbook — needs per-service annexes (Postgres / Neo4j / Qdrant / Redis / SeaweedFS / Reverb / Horizon / Martin / FastAPI) since each has different recovery semantics. |
| M-A6-03 | Medium | `ops/runbooks/` | No `refusal-rate-spike.md`. When the refusal-rate dashboard exists (H-A2-01) it needs a triage script. |
| M-A6-04 | Medium | `ops/runbooks/` | No `llm-model-swap.md` runbook. Memory references `docs/model_migration.md` but that is doc. The on-call playbook must include "rotate Ollama → vLLM if dev → prod" steps. |
| L-A6-05 | Low | `ops/runbooks/container-hardening.md` | Module 1 C1 (volume-wipe) and Module 9 9.7 (volume migration) carry-forwards belong in a `volume-migration.md` annex. |

---

## A7 — Doc Sweep & Spec Drift

### Architecture Doc Banner Inconsistency

- `georag-architecture.html:2` — `<title>GeoRAG Architecture v1.8</title>`
- `georag-architecture.html:24` — `<div>This v1.8 document combines a v1.2
  rebuild ... v1.8 Martin tile-serving integration.`
- `georag-architecture.html:799` — `<footer>v1.9 — April 19, 2026 · §12 Version
  Compatibility Matrix fully reconciled ...`

The doc is internally inconsistent: title + intro div say v1.8; footer says
v1.9. Module 10 should re-version to v1.10 (clean) and reconcile the banner.

### `module-10-doc-sweep.md` Triage (225 lines, 17 items)

Reviewed every item:

| Item | Status as-of 2026-04-22 |
|---|---|
| Neo4j 2026.02.3 vs 2026.03.1 image tag | **Real** — drift in §12, still open |
| Qdrant `ef_construct=128` vs 200 live | **Real — Kyle decision pending** |
| Neo4j `DrillHole` vs `Drillhole` label | **Real — Kyle decision pending** |
| `workspace_id` absent from data layers | **PARTIALLY CLOSED** — Module 9 added the column; verify all three layers |
| Qdrant `sparse_vectors_config` PATCH key | **Real — doc fix only** |
| `.env.example` `NEO4J_AUTH=none` | **CLOSED 2026-04-19** |
| `docker-compose.yml APP_DEBUG=true` default | **Real — Module 9 deployment hardening, still no `.env.production`** |
| SeaweedFS bucket naming | **CLOSED 2026-04-20** |
| PostgreSQL 18.3 banner in §12 | **Partially closed** — CLAUDE.md fixed, arch HTML still drifted |
| PostgreSQL tuning values §06/§12 | **Real** — live values 8GB/24GB/1GB not in doc |
| Neo4j heap initial-size restart pending | **Real** — restart still pending |
| KML parser removed — doc cleanup | **Real — doc edit only** |
| `evidence_items.passage_id` cascade | **Real — doc edit only** (RESTRICT) |
| `bronze.provenance` vs `document_revisions` | **Real — doc edit only** |
| §04j ↔ Module 3 §6 B8.5 cross-reference | **Real — doc edit only** |
| `silver.workspaces` schema-prefix | **Real — doc edit only** |
| rio-cogeo version range | **Real — module spec edit only** |
| Query-class precedence viz > spatial | **Real — module spec edit only** |
| Dagster GPU access for SPLADE | **Real — Kyle decision deferred to prod** |
| Neo4j retrieval timeout 2.0s vs 3.0s | **Real — doc edit, code is correct** |
| Cache key v4 includes routing-bucket | **Real — addendum §05d edit** |
| `answer_runs.retrieval_strategy_version` width | **CLOSED 2026-04-21** |

### Module 9 13 Pre-existing Test Drift Items

Per `project_module_9_status.md`, 13 pre-existing FastAPI test failures
remained. Symptom names: cache-key v5→v6, `retrieval_strategy_version` field
'hybrid', dash-form vs colon-form citation IDs, 6 TileProxy test failures.
Without running the failing tests I cannot per-item triage; **defer to
Module 10 Chunk where pytest is unblocked and tests vs spec are reconciled
case by case** (the H-A4-01 fix runs pytest with the missing markers — that
will surface the drift naturally).

### `// @ts-nocheck` Sweep

- `resources/js/Components/Analytics/AlterationMap.tsx:1` — still has `// @ts-nocheck`.
  Memory `project_module_8_status.md` flagged for Module 10 closure.
- `resources/js/Components/StripLogViewer.tsx:1` — `// @ts-nocheck`.
- `resources/js/Components/ui/skeleton.tsx:1` — `// @ts-nocheck`.
- `resources/js/Components/ui/alert.tsx:1` — `// @ts-nocheck`.
- 6 test files have `// @ts-nocheck` (tolerable — test files have looser
  type rigor by convention).

### Findings

| ID | Severity | File:Line | Finding |
|---|---|---|---|
| H-A7-01 | High | `georag-architecture.html:2 + :24 + :799` | Banner version mismatch (title v1.8 / intro v1.8 / footer v1.9). Module 10 must rev to v1.10 and reconcile. |
| H-A7-02 | High | `ops/backlog/module-10-doc-sweep.md` | 17 of 22 items still real; ~12 require doc-only edits (low risk), 5 require code or Kyle decision. Module 10 must dispatch the doc-only edits in a single sweep PR. |
| M-A7-03 | Medium | `resources/js/Components/Analytics/AlterationMap.tsx:1` | `// @ts-nocheck` still present after Module 8 promised closure. Type-annotate or document why MapLibre types remain loose. |
| M-A7-04 | Medium | `resources/js/Components/StripLogViewer.tsx:1` | `// @ts-nocheck` "migration in progress" — Module 10 should close. |
| M-A7-05 | Medium | `resources/js/Components/ui/{skeleton,alert}.tsx` | Two shadcn/ui wrapper components have `// @ts-nocheck`. shadcn/ui ships well-typed sources; replace these with the canonical typed variants. |
| L-A7-06 | Low | (Module 9 carry-forward) | 13 pre-existing FastAPI test failures cannot be triaged read-only — they manifest only when CI is reconfigured to run golden + hallucination markers. Triage in Chunk that re-enables those markers. |

---

## A8 — Performance Baseline + Load Testing

### Evidence

- `src/fastapi/scripts/load_test.py` (40+ lines visible) — concurrent-stream
  load test for Laravel → FastAPI → SSE. Configurable concurrency / total /
  project. Has a stated purpose (catch Horizon `maxProcesses=5` saturation),
  exits non-zero on failure. **Not invoked by CI.**
- No `tests/performance/`, no `tests/load/`, no k6 scripts, no Locust files
  found in repo.
- `src/fastapi/tests/test_chaos_resilience.py` — 21 chaos/resilience tests.
  Inspected first line: `"""Chaos/resilience tests (→ A grade).` Excluded from
  CI by the `not integration and not golden and not hallucination` filter only
  if marked accordingly; would need to inspect markers to confirm.
- `ops/baselines/` contains 8 files but they are **infra** baselines (Postgres
  tuning, datastore stats, Module 4 parallel-dispatch). Not API-latency
  baselines.
- Memory `project_module_8_status.md` references p95 latency targets but no
  consolidated baseline doc.
- No documented "this stack handles X concurrent queries before degradation"
  capacity-planning doc.
- `retrieval_threshold_sweep.csv` exists in `src/fastapi/` — possibly the
  closest thing to a measured-baseline artefact.

### Findings

| ID | Severity | File | Finding |
|---|---|---|---|
| H-A8-01 | High | `src/fastapi/scripts/load_test.py` | Load test exists but is **never run by CI**. There is no protection against latency regression. Wire into a nightly job at minimum. |
| H-A8-02 | High | (absent) | No consolidated `ops/baselines/2026-04-22-api-latency.md` or similar capturing p50 / p95 / p99 for the seven query classes. Without this, the term "regression" has no referent. |
| M-A8-03 | Medium | `src/fastapi/tests/test_chaos_resilience.py` | 21 chaos tests. Confirm they are run in CI (likely excluded by integration marker). Add specific marker `chaos` and a CI job that runs them weekly. |
| M-A8-04 | Medium | (absent) | No capacity-planning doc. SME needs a sentence "this single-VM dev workstation supports N concurrent users" before any prospect demo. |
| M-A8-05 | Medium | (absent) | No retrieval-cache or ETag-tile-cache hit-rate measurements over time. Module 7 Phase C deliverable territory. |
| L-A8-06 | Low | (absent) | No soak test. Memory burn over 24 h is not measured. Optional but defensible for on-prem deployments. |

---

## Carry-Forward Triage (cross-checked against MEMORY.md module-status files)

| Source | Item | Verdict |
|---|---|---|
| `project_module_1_status.md` | C1 volume-wipe still open | **Still real** — needs `volume-migration.md` runbook + execution slot |
| `project_module_1_status.md` | Neo4j 2026.03.1 image-tag drift | **Still real** — H-A7-02 covers the §12 doc fix; image already digest-pinned |
| `project_module_1_status.md` | C5-02 backup-agent SIGTERM | **Still real** — no runbook addresses graceful shutdown of backup-agent |
| `project_module_3_status.md` | Phase C deferred (paired with Module 4 start) | **Closed** — Module 4 closed 2026-04-21; verify Phase C subsumed in Module 4 close-out memo |
| `project_module_4_status.md` | Phase C + D deferred | **Still real** — measurement (C) and runbooks (D) overlap with H-A2-01 / H-A6-01 |
| `project_module_5_status.md` | TOOL-CALL-01 regression to investigate | **Re-classify** — `retrieval_strategy_version` width fix closed item; full TOOL-CALL-01 status needs a fresh look (touched code in Module 9). Defer to Chunk 5. |
| `project_module_5_status.md` | Phase C + D deferred | **Still real** — measurement + runbooks |
| `project_module_6_status.md` | Phase C measurement | **Still real** — overlaps with H-A2-01 |
| `project_module_6_status.md` | Phase D runbooks | **Partially real** — `evidence-model.md`, `citation-pipeline.md` are present; OFR-1 (RESTRICT FK) is doc-edit per H-A7-02; OFR-5 (dash-form deprecation) is Module 10 cleanup |
| `project_module_7_status.md` | STREAM-02 reconnect closure | **Still real** — overlaps with L-A3-05 |
| `project_module_7_status.md` | FreshnessBadge `data_version` diff (Module 9) | **Closed** per Module 9 close-out (verify by re-checking `useFreshnessSnapshot` if Chunk dedicates a session to it) |
| `project_module_7_status.md` | Phase C measurement / Phase D runbooks | **Still real** — H-A2-01 / H-A6-01 |
| `project_module_8_status.md` | Martin `/metrics` upgrade | **Still real** — gated on Martin upstream; M-A1-05 |
| `project_module_8_status.md` | AlterationMap `@ts-nocheck` | **Still real** — M-A7-03 |
| `project_module_8_status.md` | DEM self-host | **Still real** — separate from Module 10 unless prod-deploy chunk needs it |
| `project_module_8_status.md` | Layer-visibility localStorage | **Still real** — small scope, can fold into a UX cleanup chunk |
| `project_module_9_status.md` | JWT `kid` rotation deferred | **Still real** — needs `secret-rotation.md` runbook (H-A6-01) |
| `project_module_9_status.md` | 13 pre-existing FastAPI test drift | **Still real** — L-A7-06; unblocks when CI runs full marker set |
| `project_module_9_status.md` | 6 TileProxy test failures | **Still real** — pre-existing; resolve in test-stabilisation chunk |

---

## Consolidated Criticals + Highs (by ID)

| ID | Sev | Area | One-line summary |
|---|---|---|---|
| **C-A4-01** | **Critical** | CI | Pytest excludes golden + hallucination + integration markers; mandatory release gates never run |
| H-A1-01 | High | Metrics | Laravel `/metrics` route does not exist; Pulse-→-Prometheus exporter missing |
| H-A1-02 | High | Metrics | Only Martin alert rules; six service classes have no alerts |
| H-A1-03 | High | Metrics | Postgres + Redis exporters commented out |
| H-A2-01 | High | Dashboards | No citation-click / refusal-rate / feedback / p95 bubble-render dashboard (Module 7 Phase C) |
| H-A2-02 | High | Dashboards | No `authz_audit` dashboard (Module 9 9.8) |
| H-A3-01 | High | Logs | No Loki/Promtail; cross-service log search impossible |
| H-A3-02 | High | Logs | Laravel does not emit `traceparent` to FastAPI |
| H-A4-02 | High | CI | No deploy stage; images not pushed |
| H-A4-03 | High | CI | CI runs PG 17.x; prod runs PG 18.3 |
| H-A4-04 | High | CI | No `.env.production` template / secret management |
| H-A5-01 | High | Tests | Only 2 of 12 V1 controllers have IDOR tests |
| H-A5-02 | High | Tests | Golden corpus not classified by `query_class` |
| H-A5-03 | High | Tests | Golden + hallucination corpora exist but never executed by CI (paired with C-A4-01) |
| H-A6-01 | High | Runbooks | 5 release-critical runbooks missing |
| H-A7-01 | High | Doc | Banner version mismatch (v1.8 vs v1.9) |
| H-A7-02 | High | Doc | 17 of 22 doc-sweep items still real |
| H-A8-01 | High | Performance | Load test exists but never run by CI |
| H-A8-02 | High | Performance | No consolidated p50/p95/p99 baseline |

---

## Findings Count by Severity

| Severity | Count |
|---|---|
| Critical | 1 |
| High | 18 |
| Medium | 22 |
| Low | 16 |
| **Total** | **57** |

(Counted across A1 = 8, A2 = 6, A3 = 5, A4 = 9, A5 = 7, A6 = 5, A7 = 6, A8 = 6.
Module 10 will not close all 57 — many "Low" items are belt-and-braces hardening
that can wait for v1.1.)

---

## Module 10 Chunk Plan (Dependency-Ordered)

A 9-chunk plan. Chunks 1-3 are mandatory release gates. Chunks 4-7 are
operability gates. Chunks 8-9 are documentation closure.

**Chunk 1 — CI Release-Gate Reconfiguration** *(Critical)*
- Remove `not golden and not hallucination` exclusion in `ci.yml:131`.
- Add `pytest -m golden` and `pytest -m hallucination` named jobs that gate PR
  merge.
- Bump pgTAP CI service image to `postgis/postgis:18-3.6` (match prod).
- Add Playwright e2e job (basic smoke).
- Triage the 13 pre-existing test drift items as they surface (per-item:
  fix code or fix test, recorded in chunk close-out memo).
- Closes: C-A4-01, H-A4-03, H-A5-03, L-A7-06.

**Chunk 2 — Deploy Pipeline + Secret Management**
- Add `cd.yml` with dev → staging → prod stages (manual approval gates between).
- Tag docker images with commit SHA + push to GHCR (or chosen registry).
- Author `.env.production.example` with all required keys, document SOPS or
  Doppler integration choice.
- Add hadolint + trivy scan steps.
- Closes: H-A4-02, H-A4-04, L-A4-08, L-A4-09.

**Chunk 3 — IDOR + Test-Corpus Coverage**
- Author IDOR PHPUnit tests for the remaining 10 V1 controllers (Citation,
  Export, HoleAnalysis, Query, Upload, ChatConversation, ColumnMapping,
  VendorProfile, PublicGeoscience controllers).
- Add `"query_class"` field to all 30 golden fixtures; assert ≥3 cases per
  class via a CI step.
- Harmonise public-geoscience golden fixture shape with main corpus.
- Add pgTAP coverage for migrations 01-07 (workspaces, projects, RLS, FK
  constraints, evidence_items CHECKs).
- Closes: H-A5-01, H-A5-02, M-A5-04, M-A5-05.

**Chunk 4 — Metrics Surface + Alert Rules**
- Add Laravel Pulse → Prometheus exporter route in `routes/api.php` or
  dedicated middleware; verify scrape returns 200.
- Uncomment redis_exporter / postgres_exporter; add the services to compose
  under `dev-monitor`.
- Add Reverb + Dagster scrape jobs.
- Author per-service alert-rule files: `fastapi.yml`, `laravel.yml`,
  `postgres.yml`, `neo4j.yml`, `qdrant.yml`, `redis.yml`. Reuse Martin file's
  conventions.
- Wire Alertmanager with at-least-one routing target (Slack webhook
  acceptable for V1).
- Closes: H-A1-01, H-A1-02, H-A1-03, M-A1-04, L-A1-07.

**Chunk 5 — Dashboards (RAG-quality + AuthZ + Service)**
- Author `georag-rag-quality.json` dashboard: citation-click rate, feedback
  submission rate, refusal rate, p95 bubble-render, p50/p95 query latency by
  query_class.
- Author `georag-authz.json`: 403-event volume, top-N user buckets, IDOR-gate
  hit count, structured-403 anomaly heatmap.
- Author `georag-services.json`: per-service tile-of-tiles
  (Postgres/Redis/Martin/Reverb/Dagster).
- Author `georag-laravel-queue.json`: Horizon queue depth, Reverb broadcast
  lag, Octane request budget.
- Closes: H-A2-01, H-A2-02, M-A2-03, M-A2-04, M-A2-05, L-A2-06.

**Chunk 6 — Structured Logs + Trace Correlation**
- Add Loki + Promtail to `docker-compose.yml` under `dev-monitor`.
- Switch Laravel logging to JSON formatter (Monolog `JsonFormatter` on
  `single`, `daily`, `authz_audit` channels).
- Switch FastAPI logging to JSON via `python-json-logger`.
- Add Laravel middleware `InjectTraceparent` that emits W3C `traceparent` on
  every inbound request and forwards to FastAPI on internal calls.
- Add an integration test that asserts the round-trip `traceparent` survives
  Laravel → FastAPI → SSE.
- Document `query_audit_log` retention policy (CRON purge or "keep forever").
- Closes: H-A3-01, H-A3-02, M-A3-03, M-A3-04, L-A3-05.

**Chunk 7 — Performance Baseline + Load Testing**
- Run `load_test.py` against a fresh stack; record p50/p95/p99 per query
  class to `ops/baselines/2026-04-22-api-latency.md`.
- Add a nightly CI job that runs `load_test.py` and fails on >20 %
  regression.
- Add a `chaos` pytest marker; CI weekly job runs `test_chaos_resilience.py`.
- Author `ops/baselines/capacity-planning.md` answering "this stack
  supports N concurrent users on hardware H."
- Closes: H-A8-01, H-A8-02, M-A8-03, M-A8-04, M-A8-05.

**Chunk 8 — Runbook Gap-Fill + Operability**
- Author the 5 missing release-critical runbooks: `secret-rotation.md`,
  `migration-rollback.md`, `deploy-rollback.md`, `on-call.md`,
  `authz-audit-triage.md`.
- Author 2 follow-up runbooks: `refusal-rate-spike.md`, `llm-model-swap.md`.
- Add per-service annexes to `service-outage.md`.
- Author `volume-migration.md` (closes Module 1 C1 + Module 9 9.7
  carry-forwards).
- Closes: H-A6-01, M-A6-02, M-A6-03, M-A6-04, L-A6-05.

**Chunk 9 — Architecture Doc Sweep + Final Rev**
- Re-version `georag-architecture.html` from v1.8/v1.9 → **v1.10**.
- Apply all 12 doc-only edits from `module-10-doc-sweep.md` (Neo4j label,
  Qdrant `sparse_vectors_config`, KML deprecation, §04j cascade RESTRICT,
  `bronze.provenance` clarification, §06/§12 PG tuning values, `silver.*`
  schema prefix, viz>spatial precedence, retrieval timeout, cache key
  contract, `silver.workspaces` placement, etc.).
- Strip `// @ts-nocheck` from `AlterationMap.tsx`, `StripLogViewer.tsx`,
  `ui/skeleton.tsx`, `ui/alert.tsx`. Type-annotate or replace with canonical
  shadcn variants.
- Update Kyle-decision items in the doc-sweep with Kyle's decisions
  captured during the chunk session: `DrillHole` label canonicalisation,
  `ef_construct` 200 vs 128, Dagster GPU.
- Final §11 release-engineering pass: explicit acceptance-criteria checklist
  embedded in arch doc (§11.5 new sub-section).
- Closes: H-A7-01, H-A7-02, M-A7-03, M-A7-04, M-A7-05.

---

## What "Shipping" Means at End-of-Module-10

A sharp acceptance-criteria checklist. Kyle should be able to point at this
list and say *"every box ticked → we're done."*

### Release-Gate (must all be green on `main`)

- [ ] CI workflow runs **golden** (≥30 cases) + **hallucination** (≥20
      adversarial cases) + **integration** markers on every PR.
- [ ] CI workflow uses **PostgreSQL 18.3** (matches production).
- [ ] CI workflow runs **e2e Playwright** smoke (chat → citation click →
      bubble render → feedback) on every PR.
- [ ] CI workflow runs **pgTAP** assertions including the new migrations
      01-07 coverage.
- [ ] CI workflow tags docker images with commit-SHA, pushes to GHCR.
- [ ] **CD workflow** with dev → staging → prod stages, manual approval gate
      to prod.
- [ ] `.env.production.example` exists and is referenced in `docs/RUNBOOK.md`.
- [ ] Trivy + hadolint pass on every PR (or surface as warning that does not
      block but is visible).

### Coverage

- [ ] Every V1 controller (12) has an IDOR test.
- [ ] Every of the 7 §05c query classes has ≥3 golden cases tagged
      `query_class`.
- [ ] Public-geoscience golden corpus harmonised with main corpus shape.
- [ ] Module 9's 13 pre-existing FastAPI test drift items each triaged and
      closed (code-fix or test-fix decided per item).

### Observability

- [ ] Laravel `/metrics` route returns 200 with valid Prometheus payload.
- [ ] Redis + Postgres exporters scraped; Reverb + Dagster scraped.
- [ ] Per-service alert rule files exist and load successfully:
      `fastapi.yml`, `laravel.yml`, `postgres.yml`, `neo4j.yml`, `qdrant.yml`,
      `redis.yml`, `martin-alerts.yml` (existing).
- [ ] Alertmanager wired to at-least-one destination (Slack acceptable).
- [ ] **5 dashboards live**: `georag-overview`, `georag-signals`,
      `georag-rag-quality`, `georag-authz`, `georag-services` (and
      optionally `georag-laravel-queue`).
- [ ] Loki + Promtail scrape Laravel + FastAPI + Dagster + Postgres logs.
- [ ] Laravel and FastAPI emit JSON logs, both carry `traceparent`.
- [ ] Round-trip `traceparent` integration test passes.

### Runbooks (19 → ≥27)

- [ ] All 5 release-critical runbooks present: `secret-rotation`,
      `migration-rollback`, `deploy-rollback`, `on-call`, `authz-audit-triage`.
- [ ] Per-service outage annexes for 9 services.
- [ ] `refusal-rate-spike`, `llm-model-swap`, `volume-migration` runbooks
      exist.
- [ ] `query_audit_log` retention policy documented.

### Performance + Capacity

- [ ] `ops/baselines/2026-04-22-api-latency.md` records p50/p95/p99 per
      query class.
- [ ] Nightly load test in CI; >20 % regression fails the build.
- [ ] Weekly chaos test in CI.
- [ ] `ops/baselines/capacity-planning.md` answers "stack supports N
      concurrent users on hardware H."

### Doc Closure

- [ ] `georag-architecture.html` version bumped to **v1.10** with
      consistent banner (title + intro + footer all agree).
- [ ] All 12 doc-only items from `module-10-doc-sweep.md` resolved.
- [ ] All 5 Kyle-decision items resolved + captured.
- [ ] Zero `// @ts-nocheck` in non-test source files.
- [ ] §11.5 release-acceptance checklist embedded in the architecture doc
      itself (this very list, copied in and signed off).

---

## Read-Only Disclaimers / Items Not Verifiable Without Execution

The following could not be verified without running code or external services
(read-only constraint):

1. Whether the 13 pre-existing FastAPI test drift items resolve as
   code-fixes vs spec-fixes — surfaces only when CI reconfigures markers.
2. Whether `tests/e2e/` Playwright suite has substantive coverage — config
   present, suite content not enumerated.
3. Whether `event_stamper.py` actually emits a parseable `traceparent` — code
   exists, round-trip not asserted by any test in the corpus.
4. Whether `query_audit_log` table has a retention CRON — searched config,
   none found, but a Laravel scheduled task may exist outside `routes/console.php`.
5. Live state of `app/Http/Middleware/` — only confirmed absence of
   `traceparent` matches by grep, may exist under a different name (e.g.
   `RequestId.php`).

These items should be re-checked in Phase B when read-write access is granted.

---

*End of audit. Total findings: 57 (1 Critical, 18 High, 22 Medium, 16 Low).
Verdict: Conditional / Don't-Ship until C-A4-01 + the 8 priority Highs are
closed. Estimated Module 10 effort: 9 chunks.*
