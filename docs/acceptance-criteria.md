# GeoRAG V1 Acceptance Criteria

**Module 10 Chunk 10.9 — final spec rev.** This document is the
canonical "is V1 done?" checklist. Every box that ticks is backed by an
artifact in the repo; every box that does NOT tick has an explicit reason
(deferred to V1.5, blocked on operator setup, etc).

This file pairs with `ops/audit/2026-04-22-observability-release-audit.md`
which authored the original 57 findings, and with each module's
`project_module_<N>_status.md` memory entry which captures the per-module
acceptance.

Updated: **2026-04-27** (V1.5 wrap + D2 sign-off).

---

## Release-Gate (CI on every PR)

| Box | Status | Evidence |
|-----|--------|----------|
| Golden corpus (≥30 cases) tagged + ≥3 cases per non-refusal class | ✅ | `src/fastapi/tests/test_golden_queries.py` + `test_golden_query_class_coverage`; 30 fixtures, 7 classes |
| Hallucination corpus (≥20 adversarial cases) | ✅ | `src/fastapi/tests/test_hallucination_failures.py` + `test_hallucination_layers.py` (92 layer-unit tests) |
| Integration markers run in release-rehearsal workflow | ✅ | `.github/workflows/release-rehearsal.yml` (workflow_dispatch + tag-push) |
| CI uses PostgreSQL 18.3 (matches production) | ✅ | `.github/workflows/ci.yml` pinned `postgis/postgis:18-3.6@sha256:f81dd52d...` |
| pgTAP assertions including migrations 01-07 | ✅ | 199/199 across 7 files in `database/tests/pgtap/` |
| CI tags Docker images with commit-SHA, pushes to GHCR | ✅ | `.github/workflows/ci.yml` `docker-build` job + GHCR auth |
| Trivy + hadolint pass on every PR (warn-only OK) | ✅ | `ci.yml` Trivy CRITICAL-fail + hadolint warn-only |
| SBOM SPDX generated per image | ✅ | `ci.yml` anchore/sbom-action, 90-day artifact retention |
| Playwright e2e smoke on PR | ⏳ | Deferred — `playwright.config.ts` exists, no CI job. V1.5 with self-hosted runner. |

## CD Pipeline + Secret Management

| Box | Status | Evidence |
|-----|--------|----------|
| CD workflow with dev → staging → prod stages | ✅ | `.github/workflows/cd.yml` (581 lines) |
| Manual approval gate to prod via GitHub Environments | ✅ | `cd.yml` `environment: production` requires reviewer |
| `.env.production.example` exists | ✅ | `.env.production.example` (458 lines, 143 keys) |
| Secret management documented | ✅ | `ops/runbooks/_archived/secret-management.md` (SOPS + age) |
| Per-credential rotation procedures | ✅ | `ops/runbooks/_archived/secret-rotation.md` (12 credential classes) |
| `SOPS_AGE_PRIVATE_KEY` GitHub Secret configured | ⏳ | Operator setup; documented in `secret-management.md` |
| `STAGING_URL` + SSH host secrets configured | ⏳ | Operator setup; `cd.yml` graceful no-op until then |

## IDOR + RBAC Coverage

| Box | Status | Evidence |
|-----|--------|----------|
| All 12 V1 controllers have IDOR tests | ✅ | 11 IDOR test files; HealthController + JurisdictionController documented as workspace-global |
| 43 IDOR PHPUnit tests passing (15 PostGIS-skipped under SQLite) | ✅ | `tests/Feature/Api/V1/*IDORTest.php` |
| RLS coverage on 11/11 workspace-scoped silver tables | ✅ | Migrations `2026_04_17_120200`, `2026_04_22_170000`; 36 pgTAP assertions |
| `MULTI_TENANT_ENFORCEMENT_ENABLED=true` default + `SINGLE_TENANT_MODE` opt-out | ✅ | `src/fastapi/app/config.py` model_validator |
| `extract_user_context` 401 on missing JWT for non-probe paths | ✅ | `src/fastapi/app/services/auth.py` |
| No default-UUID workspace fallback | ✅ | `src/fastapi/app/services/workspace_resolution.py` |
| `hasProjectAccess` fail-closed + boot guard | ✅ | `app/Models/User.php` + `AppServiceProvider` boot |

## Observability — Metrics

| Box | Status | Evidence |
|-----|--------|----------|
| Laravel `/metrics` returns 200 + valid Prometheus payload | ✅ | `MetricsController.php`; verified `curl -s http://localhost:8888/metrics` |
| FastAPI `/metrics` exposed | ✅ | `src/fastapi/app/main.py:710` (pre-existing per audit retraction) |
| Redis + Postgres exporters scraped | ✅ | Compose `redis_exporter` + `postgres_exporter`, both UP |
| Reverb + Dagster scrape jobs configured | ✅ | `prometheus.yml` jobs added; targets DOWN per known infra (no native exporters — V1.5) |
| Per-service alert rule files | ✅ | 7 files: fastapi/laravel/postgres/redis/neo4j/qdrant + martin (pre-existing). 31 rules across 7 groups. |
| Alertmanager wired with destination | ✅ | `docker/alertmanager/alertmanager.yml` (dev) + `alertmanager.production.yml.example` (Slack + PagerDuty receivers pre-wired; substitute env placeholders) |
| 9 of 9 Prometheus targets UP | ✅ | UP: fastapi, laravel-octane, postgresql, redis, qdrant, prometheus, alertmanager, martin (V1.5-06), neo4j (V1.5-07 sidecar). Reverb + Dagster surface through laravel `/metrics` (V1.5-08 — not separate scrape jobs). |

## Observability — Dashboards (≥4 new)

| Box | Status | Evidence |
|-----|--------|----------|
| `georag-rag-quality.json` (Module 7 Phase C metrics) | ✅ | 10 panels |
| `georag-authz.json` (Module 9 9.8 audit channel) | ✅ | 5 panels + Loki transition path |
| `georag-services.json` (per-service tile-of-tiles) | ✅ | 10 stat tiles |
| `georag-laravel-queue.json` (Horizon + Octane + Pulse) | ✅ | 7 panels |
| Grafana shows 6 dashboards (2 existing + 4 new) | ✅ | `curl -u admin:... /api/search` confirmed 6 |
| Prometheus + Loki datasources auto-provisioned | ✅ | `docker/grafana/provisioning/datasources/prometheus.yml` |

## Observability — Logs + Trace

| Box | Status | Evidence |
|-----|--------|----------|
| Loki + Promtail running | ✅ | Compose `loki` + `promtail`; 11 Loki labels populated |
| Promtail tails authz_audit-*.log + Docker stdout | ✅ | `docker/promtail/promtail-config.yaml` 2 scrape jobs |
| `traceparent` round-trip Laravel → FastAPI | ✅ | `InjectTraceparent.php` + `StructuredAccessLogMiddleware` extension; 14/14 tests green |
| Full Monolog JSON formatter on Laravel channels | ✅ | V1.5-04: JsonFormatter on single/daily/authz_audit; Promtail pipeline simplified to single `json` stage. |
| `python-json-logger` on FastAPI | ✅ | V1.5-05: hand-rolled JsonFormatter at `src/fastapi/app/logging_config.py`; 6/6 unit tests + live container verified. |
| `query_audit_log` retention documented | ✅ | `ops/runbooks/_archived/log-retention.md` (8-row reference table) |

## Runbooks (19 → 28)

| Box | Status | Evidence |
|-----|--------|----------|
| `secret-rotation.md` — 12 credential classes | ✅ | 412-line per-credential procedures |
| `migration-rollback.md` — Laravel artisan + multi-service coordination | ✅ | Including stuck-rollback recovery |
| `deploy-rollback.md` — cd.yml-driven SHA rollback | ✅ | Closes cd.yml TODO |
| `on-call.md` — first-30-min triage tree | ✅ | Branches into all other runbooks |
| `authz-audit-triage.md` — per-reason_code + LogQL | ✅ | Pairs with georag-authz dashboard |
| `refusal-rate-spike.md` — reason_code triage | ✅ | Crosses to retrieval-pipeline + citation-pipeline |
| `llm-model-swap.md` — rolling vs cold | ✅ | Memory'd num_ctx gotcha referenced |
| `volume-migration.md` — Module 1 C1 + Module 9 9.7 carry-forwards | ✅ | UID migration + cold-start wipe + DR |
| `service-outage.md` — per-service annexes | ✅ | 6 new sections: Redis, Reverb, Martin, Horizon, FastAPI, Dagster |

## Performance + Capacity

| Box | Status | Evidence |
|-----|--------|----------|
| `chaos` pytest marker + weekly CI cron | ✅ | `.github/workflows/chaos.yml`, Mon 06:00 UTC |
| `perf-baseline.yml` nightly load test | ✅ | Nightly 02:00 UTC; >20% regression fails build |
| `compare_perf_baseline.py` regression script | ✅ | Parses baseline YAML frontmatter + JSON results |
| `ops/baselines/2026-04-22-api-latency.md` baseline | ⏳ | File committed; values PENDING first nightly run after STAGING_URL configured |
| `ops/baselines/capacity-planning.md` | ✅ | 3-tier hardware + 6 bottlenecks + per-tier estimates |

## Doc Closure

| Box | Status | Evidence |
|-----|--------|----------|
| `// @ts-nocheck` removed from 4 frontend files | ✅ | All 4 stripped: `ui/skeleton.tsx`, `ui/alert.tsx` (Module 10), `AlterationMap.tsx` (V1.5-09, 7 errors fixed), `StripLogViewer.tsx` (V1.5-10, 24 errors fixed). |
| `module-10-doc-sweep.md` items resolved | partial | 17 of 22 items still real, of which several already resolved per individual item subheadings; remaining items are inline edits to `georag-architecture.html` |
| Architecture doc v1.10 banner reconciliation | ⏳ | Deferred — multi-thousand-line HTML rev requires dedicated session; tracked as V1.5 backlog |
| §11.5 release-acceptance checklist embedded | ✅ | This file (`docs/acceptance-criteria.md`) is the canonical version; arch doc rev will reference it |
| Memory `MEMORY.md` updated for every closed module | ✅ | 16+ entries; final Module 10 entries added |

---

## Net V1 ship-readiness

**11/11 confirmed-exploitable Critical findings closed across all 10 modules.**

**Active leak primitives: zero.** No code path in production today exposes a
caller's data to a different tenant.

**Defense-in-depth landed:**
- RLS on 11 silver tables with FORCE ROW LEVEL SECURITY.
- 6 IDOR gates on Laravel controllers, 1 unified workspace resolver on FastAPI side.
- Security headers, TrustProxies, hardened cookies, container non-root, APP_DEBUG=false default.
- Structured `authz.deny` audit channel feeding both Prometheus counter and Loki labels.
- 31 alert rules + Alertmanager routing.
- Daily-rotated authz_audit log with 30-day retention.
- 28 operational runbooks covering every named scenario.
- 199/199 pgTAP, 622/622 FastAPI fast suite, 500/500 vitest, 217/217 Laravel feature tests, 14/14 traceparent round-trip.

## V1.5 status (2026-04-27): 23 / 23 engineering items closed

All originally-deferred items are closed. Remaining gates are operator-side
(below) and one validation pass (first arm64 build merge / first nightly
perf-baseline / first e2e run on the self-hosted runner) that can only
happen after the corresponding infra lands. See
`ops/backlog/v1.5-followups.md` for the per-item close-out details.

Notable closures since 2026-04-22:
- **v1.5-23** (2026-04-27): Frontend localStorage token sweep — 16 source files / 19 fetch sites migrated to Sanctum cookie auth; `Login.tsx` writer deleted; 12 follow-up regression tests added; vitest 466→500.
- **D2** (2026-04-27): `:Drillhole` → `:DrillHole` rename — Cypher migration script + code refactor + runbook prepared per Kyle sign-off; execution scheduled for next maintenance window.

## Deferred indefinitely (no client demand)

- k8s manifests beyond Helm chart skeleton (V1 is on-prem SSH-deploy by design; 5/8 prod-maturity gates templated, 3 externalised to community charts).
- Architecture doc v1.10 inline banner reconciliation in `georag-architecture.html` (multi-thousand-line HTML edit; per-module status memos + `docs/acceptance-criteria.md` are authoritative).

## Operator setup before first prod deploy

**Run `docs/OPERATOR-AFTERNOON.md` end-to-end** — that is the canonical
checklist. The three scripts in `scripts/operator/` make every step here
either fully automated or a guided prompt.

```bash
bash scripts/operator/preflight.sh           # current red/green state
bash scripts/operator/bootstrap-secrets.sh   # closes O-01 + O-04
bash scripts/operator/set-github-secrets.sh  # closes O-01 + O-02 + O-03
bash scripts/operator/preflight.sh           # confirm green
```

| ID   | Action | Closed by |
|------|--------|-----------|
| O-01 | Generate `SOPS_AGE_PRIVATE_KEY` (operator + CI age keys, `.sops.yaml`) | `bootstrap-secrets.sh` + `set-github-secrets.sh` |
| O-02 | Configure SSH host secrets (dev/staging/prod trio) | `set-github-secrets.sh` |
| O-03 | Configure `STAGING_URL` (+ optional `*_BASE_URL`) | `set-github-secrets.sh` |
| O-04 | Encrypt initial `.env.production.enc` | `bootstrap-secrets.sh` (interactive — prompts you to fill placeholders) |
| O-05 | Run cold-start migration | `ops/runbooks/_archived/cold-start.md` (manual on prod host) |
| O-06 | Capture first perf-baseline | First nightly run after O-03 set; commit YAML diff to `ops/baselines/2026-04-22-api-latency.md` |
| O-07 | Wire Alertmanager destination | Manual on prod host: copy `docker/alertmanager/alertmanager.production.yml.example` → `alertmanager.production.yml`, substitute Slack/PagerDuty placeholders |

After O-01..O-03 are green, flip cd.yml from no-op to enforcing:

```bash
bash scripts/operator/preflight.sh --emit-cd-patch | git apply
git add .github/workflows/cd.yml
git commit -m "ci(cd): enforce deploy gates after operator provisioning"
```

## Validation gates (only fire after corresponding infra lands)

| ID   | Gate | Trigger |
|------|------|---------|
| V-01 | First arm64 build merge | Merges to `main` push to GHCR with `linux/amd64,linux/arm64` (already wired in `ci.yml`) |
| V-02 | First nightly perf-baseline | After O-03 (STAGING_URL configured) |
| V-03 | First e2e run | After self-hosted GH runner labelled `georag-e2e` is provisioned |
| V-04 | First helm install in real cluster | After client cluster access; chart at `ops/charts/georag/` |
| V-05 | D2 Drillhole rename execution | Run `ops/migrations/neo4j/2026-04-27-drillhole-rename.cypher` per `ops/runbooks/_archived/drillhole-label-rename.md` during next maintenance window |

After O-01..O-07 land, every box above is green or has an explicit
deferred reason. **Ship.**
