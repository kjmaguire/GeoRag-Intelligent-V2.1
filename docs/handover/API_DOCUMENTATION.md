# API Documentation — GeoRAG Intelligence

> Caller-facing contracts: HTTP routes, WebSocket channels, tile API, refusal
> vocabulary, HMAC envelope, Trust-Summary, security response headers.
> Routes into the live router code for request/response models, and into
> `docs/api/openapi.json` (partial snapshot — `~10` of `109` FastAPI
> endpoints). Live OpenAPI from the running container at `GET /openapi.json`
> is the single source of truth when `OPENAPI_DOCS_PUBLIC=true`.
>
> System composition: [`SAD.md`](SAD.md). Data flows behind the routes:
> [`DFS.md`](DFS.md).

---

## 1. API surfaces at a glance

| Surface | Base path | Auth | Audience |
|---|---|---|---|
| Laravel SPA | `/` | Sanctum SPA session (cookie) | Browser — Inertia-rendered pages |
| Laravel public API | `/api/v1/*` | Sanctum Bearer token (or SPA session) | First-party SPA + external API users |
| Laravel admin web | `/admin/*` (via `routes/web.php`) | Sanctum SPA + `admin` Gate | Operator UI (Inertia pages) |
| Laravel `/internal/v1/*` | `/internal/v1/*` | `X-Service-Key: FASTAPI_SERVICE_KEY` | FastAPI callbacks only |
| FastAPI domain service | 6 URL families (§5.1) | `X-Service-Key` + optional forwarded `Authorization` JWT | Laravel + Hatchet + Dagster + Kestra |
| WebSocket | `:8085` | Sanctum-authorized channel subscriptions | Browser real-time |
| Tile server | `:3002` (Martin) | Postgres `martin_ro` role | MapLibre client |
| Liveness probe | `/up` | None — Laravel built-in | Compose / k8s readiness |
| Metrics | `/metrics` | Private-IP allowlist (`MetricsController::isAllowedScraper`) | Prometheus scraper |
| Built-in dashboards | `/horizon`, `/pulse` | Email allowlist gate / Pulse gate | Operators |

Laravel ports: `APP_PORT` (default 80; dev override 8888). FastAPI: 8000. Reverb: 8085. Martin: 3002. Hatchet UI: 8889.

---

## 2. Authentication

### 2.1 SPA cookie flow (Sanctum stateful)

1. `GET /sanctum/csrf-cookie` — primes XSRF-TOKEN.
2. `POST /api/v1/auth/spa-login` — credential exchange; session cookie is the credential.
3. Subsequent `/api/v1/*` requests include the cookie automatically.

`EnsureFrontendRequestsAreStateful` middleware is prepended on the api middleware group so SPA + cookie work on `/api/v1/*` without bearer tokens. Stateful domain allowlist: `SANCTUM_STATEFUL_DOMAINS`.

### 2.2 Token flow (Sanctum bearer)

1. `POST /api/v1/auth/login` returns `{ token, token_type: "Bearer", user }`.
2. Subsequent requests send `Authorization: Bearer <token>`.

Token expiration: `SANCTUM_TOKEN_EXPIRATION=480` (minutes = 8 hours). `SANCTUM_TOKEN_PREFIX` empty default — flagged for secret-scanner hardening ([`HANDOVER_INDEX.md`](HANDOVER_INDEX.md) §5.3).

### 2.3 Service-to-service (Laravel ↔ FastAPI)

- **Laravel → FastAPI**: `X-Service-Key: $FASTAPI_SERVICE_KEY` + short-lived JWT minted by `App\Services\FastApiJwtMinter` in `Authorization` header.
- **Key rotation envelope**:
  - `FASTAPI_SERVICE_KEY` + `FASTAPI_SERVICE_KEY_KID` — current signing key + its `kid` header value.
  - `FASTAPI_SERVICE_KEY_PREVIOUS` + `FASTAPI_SERVICE_KEY_PREVIOUS_KID` — verification-only previous key. FastAPI accepts either; Laravel signs only with the current.
  - `KESTRA_FLOW_JWT_SECRET` — separate per-flow JWT secret for Kestra → FastAPI integration triggers.
- **FastAPI → Laravel `/internal/*`**: `X-Service-Key` only.

### 2.4 Rate limits (`AppServiceProvider::boot`)

| Limiter | Scope | Budget |
|---|---|---|
| `auth-login` | `e:sha1(email) + '\|' + ip` | 5 / minute |
| `throttle:3,1` | register, per IP | 3 / minute |
| `queries` | authenticated user id (IP fallback) | 30 / minute (counts both `POST /queries` and `POST /queries/{id}/start`) |
| `bridge:report-progress` | per `build_id` | 600 / minute |
| `public-geoscience-tiles` | authenticated user id (IP fallback) | 600 / minute |

FastAPI-side `slowapi` rate limits are off by default (`RATE_LIMIT_ENABLED=False`). When enabled: `RATE_LIMIT_DEFAULT=60/minute`, `RATE_LIMIT_QUERIES=20/minute`.

---

## 3. Laravel public API — `/api/v1/*`

All routes under `auth:sanctum` except the auth group below. Inventory (verified 2026-05-29): **61 direct `Route::*` declarations + 6 `Route::resource` declarations** in `routes/api.php` (67 total entries; `artisan route:list` expands the resources for an effective endpoint count of ~91).

### 3.1 Auth

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/auth/register` | Public. Rate limit 3/min. |
| POST | `/api/v1/auth/login` | Public. Rate limit `auth-login`. Returns Bearer token. |
| POST | `/api/v1/auth/spa-login` | Public. Rate limit `auth-login`. Cookie session. |
| POST | `/api/v1/auth/logout` | Revokes current token / clears session. |
| GET | `/api/v1/auth/me` | Current user profile. |

### 3.2 Projects + drill data

| Method | Path | Notes |
|---|---|---|
| `apiResource` | `/api/v1/projects` | CRUD; scoped to user memberships in controller. |
| `apiResource` (`index\|store\|show\|destroy`) | `/api/v1/projects/{project}/collars` | Scoped nested resource. |
| GET | `/api/v1/projects/{projectId}/holes/{holeIdOrCollarId}/analysis` | Per-hole bundle (surveys + structures + geochem). |
| GET | `/api/v1/projects/{projectId}/coverage-density` | GeoJSON heatmap (CC-03 Item 5). |
| `apiResource` (`index\|store\|show`) | `/api/v1/projects/{project}/exports` | Dispatch Horizon export jobs, poll status. |
| GET | `/api/v1/exports/{export}/download` | Pre-signed redirect to artifact. |
| POST | `/api/v1/projects/{project}/upload` | Generic upload → bronze bucket → Dagster sensor. |
| GET | `/api/v1/upload/categories` | Supported categories. |
| POST | `/api/v1/projects/{slug}/drill-uploads` | Slug-routed drill upload (CC-01 Item 1). |

### 3.3 RAG queries + chat

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/queries` | Phase 1 — reserve `queryId` + channel. |
| POST | `/api/v1/queries/{queryId}/start` | Phase 2 — dispatch Horizon job. |
| GET | `/api/v1/citations/resolve` | Resolve source text for `source_chunk_id`. |
| GET | `/api/v1/conversations` | List chat conversations. |
| GET | `/api/v1/conversations/{conversationId}` | Single conversation. |
| PUT | `/api/v1/conversations/{conversationId}` | Upsert (localStorage → server sync). |
| DELETE | `/api/v1/conversations/{conversationId}` | Delete. |

### 3.4 Trust / interpretation / charts

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/answer-runs/{id}/trust-summary` | §19.2 Trust Inspector — proxies FastAPI 7-section payload. |
| ANY | `/api/v1/interpretation/{tail}` | §19.3 — proxies CRUD for notes / section-lines / target-zones / comments. |
| POST | `/api/v1/charts/render` | §17.3 Charts Gallery (8 kinds — see §8.4). |

### 3.5 Ingestion + vendor profiles

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/ingest-progress/{run_id}` | Polling fallback. UUID-only. 404 on cross-workspace `run_id` (prevents existence fingerprinting). |
| `apiResource` | `/api/v1/vendor-profiles` | Global column-mapping profiles. |
| `apiResource` (`index\|store\|update\|destroy`) | `/api/v1/vendor-profiles/{vendor_profile}/column-mappings` | Per-profile column maps. |

### 3.6 Public REST API breadth (§3.3)

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1` | Self-describing index. |
| GET | `/api/v1/openapi.json` | OpenAPI document. |
| GET | `/api/v1/answers/{answer_run_id}` | Single answer-run. |
| GET | `/api/v1/maps/{project_id}/layers` | Map layer descriptors. |
| GET | `/api/v1/reports` | List reports. |
| GET | `/api/v1/targets/{project_id}` | Target recommendations. |
| GET | `/api/v1/interpretations/{project_id}` | Interpretations. |
| GET | `/api/v1/audit/{workspace_id}` | Workspace audit feed. |
| GET | `/api/v1/usage/{workspace_id}` | Usage metrics. |
| GET | `/api/v1/webhooks` | Webhook registry. CRUD lives in Kestra — flagged ([`HANDOVER_INDEX.md`](HANDOVER_INDEX.md) §5.3). |

### 3.7 Public Geoscience (§10)

All under `/api/v1/public-geoscience/`:

| Method | Path | Notes |
|---|---|---|
| GET | `jurisdictions` | Read-only jurisdiction registry. |
| GET | `health` | Health check. |
| GET | `features/{layer}/{feature_id}` | Single-feature drill-in. `layer` validated against `LAYER_TABLES`. |
| GET | `entities/{canonical_type}/{pg_id}/references` | `canonical_type` ∈ `mine\|mineral_occurrence\|drillhole_collar\|resource_potential_zone\|rock_sample\|assessment_survey\|mineral_disposition`. |
| GET | `documents/{report_id}/references` | Cross-corpus linker drill-in (plan §07d). |

### 3.8 Dashboard (§3–§4)

All under `/api/v1/dashboard/`:

- `GET platform-readiness`
- `GET portfolio/{kpis,projects,query-activity,ingestion-health,feedback,activity}`
- `GET projects/{slug}/{header,kpis,aoi,kg-counts,recent-queries,feedback,documents,drill-summary,analytics}`
- `GET projects/by-id/{projectId}/context` — D6 banner aggregator.

### 3.9 Citation feedback

| Method | Path | Notes |
|---|---|---|
| POST | `/api/v1/citations/feedback` | Registered inside `routes/web.php` Sanctum group. Per-answer thumbs-up/down + correction text. |

### 3.10 Admin surfaces (`routes/web.php`)

All under `auth:sanctum` + admin gate. Both Inertia page-renders and JSON action handlers. **155 `Route::*` declarations** in `web.php` (verified 2026-05-29; supersedes prior 157 — 2 routes removed in the May feature waves). URL families:

- **Workflow / cluster / ingest**: `/admin/workflow-runs`, `/admin/cluster-ingest`, `/admin/hatchet-workers`, `/admin/shadow-runs/*`, `/admin/ingestion-review`.
- **Reasoning / agents**: `/admin/decisions`, `/admin/support-cockpit`, `/admin/hypothesis-workspace`.
- **Target recommendation**: `/admin/target-recommendation/runs[/{run_id}]` + `/signoff` + `/geojson`.
- **Report builder**: `/admin/reports`, `/admin/reports/build`, `/admin/reports/{build_id}[/sections/{section_id}[/history]]`, `/admin/reports/export`.
- **ML training**: `/admin/ml/training-runs`, `/admin/ml/train-target-model`, `/admin/ml/train-source-trust`.
- **Conflicts / audit / what-changed**: `/admin/conflicts[/run]`, `/admin/audit[/cold-tier-archive]`, `/admin/what-changed`.
- **Tier 2/3/4 misc** (`Tier234Controller`): `/admin/source-trust`, `/admin/export-gate`, `/admin/load-test`, `/admin/dashboards`, `/admin/recommendations[/nbd\|/analogue]`, `/admin/qp-credentials[/{id}/verify]`, `/admin/workspace-{members,settings}`, `/admin/audit-explorer[/verify-chain]`, `/admin/phase-h4-health`, `/admin/backups`, `/admin/saved-maps`, `/admin/alerts-inbox[/acknowledge]`.
- **Integrations + SSO bridge**: `/admin/integrations`, `/admin/integrations/flags/{flag_name}` (PATCH), `ANY /admin/integrations/kestra/{path?}` (Caddy → Kestra SSO forward, see [`CICD_PIPELINE.md`](CICD_PIPELINE.md) §6.9 split rule), `/admin/integrations/{senders,jwt-keys/rotate}`.
- **Agent config**: `/admin/agent-config/{timeouts,prompts,pins,workspaces}[/{name|id}/{promote|update}]`.

### 3.11 SPA / Inertia top-level

- `GET /` — landing
- `GET /login`, `/forgot-password`, `/foundry/login`
- `GET /up` — Laravel built-in health
- `GET /metrics` — Prometheus exposition, private-IP gated
- `GET /dashboard`, `/projects[/{slug}/...]` — Foundry surface (Overview, Chat, Explorer, Workspace, Lakehouse, DrillholeDetail, DrillReview, HoleCompare, Targets, Rationale, Decisions, Audit, Analytics, Reasoning, Hypothesis, AssessmentSummary, Report, RetrievalInspector, WhatsChanged, SavedMapViews, IngestionRuns, ImportsQuality)
- `GET /retrieval/{traceId}`
- `GET /public-geoscience/tier3-unlock`, `POST /public-geoscience/tier3-unlock`
- `GET /support-cockpit`, `GET /threads`
- `GET /charts-gallery`

---

## 4. Laravel `/internal/*` — FastAPI → Laravel bridge

Service-key only (`service.key` middleware). All under `/internal/*` (no `/api` prefix).

| Method | Path | Purpose |
|---|---|---|
| POST | `/internal/sanctum/check` | Caddy forward-auth target — returns 204 + `X-Kestra-Auth` for admins |
| POST | `/internal/admin/reports/{build_id}/progress` | Report build progress fan-out (throttled per build_id) |
| POST | `/internal/v1/ingest-progress/broadcast` | Ingestion progress (Reliability Spec Phase 1) |
| POST | `/internal/v1/workspace-data-updated` | Non-ingestion workspace updates (`score_targets` etc.); carries new `data_version` |
| POST | `/internal/v1/admin-surface-updated` | Phase 2 generic admin surface push |
| POST | `/internal/v1/workspace-activity` | Phase 3 — Portfolio + Projects activity |
| POST | `/internal/v1/user-inbox-updated` | Phase 3 — per-user inbox + nav badge |
| POST | `/internal/v1/public-geoscience-tiles-invalidated` | Phase 4 — tile cache-bust epoch bump |

---

## 5. FastAPI domain service

109 endpoints across 32 router files. Service-key required globally (most routers declare `dependencies=[Depends(verify_service_key)]`).

### 5.1 URL families

The `/internal` prefix is applied at `include_router` time only for `queries`, `projects`, `exports`, `outlier_assist`. Every other router declares its own prefix:

| Family | Routers / files |
|---|---|
| `/queries`, `/projects/*` | `queries.py`, `projects.py` (mounted at `/internal` by `main.py`) |
| `/internal/exports/*` | `exports.py` |
| `/outlier-assist` | `outlier_assist.py` (mounted at `/internal`) |
| `/internal/v1/*` | `ocr_render.py` (`/ocr`), `re_ocr_trigger.py` (`/re_ocr_page`), `shadow_trigger.py` (`/shadow`), `mv_refresh_trigger.py` (`/mv-refresh`), `integrations_trigger.py` (`/integrations`), `metrics_ingestion_events.py` (`/metrics`) |
| `/v1/*` | `answer_runs.py` (`/v1/answer_runs`), `evidence.py` (`/v1/evidence`), `visualizations.py` (`/v1/viz`), `interpretation.py` (`/v1/interpretation`) |
| `/api/v1/*` | `phase0_ops.py` (`/api/v1`), `citation_feedback.py` (`/api/v1/citations`) |
| `/api/v1/admin/*` | `audit_findings.py`, `conflicts.py`, `ml_training.py` (`/ml`), `report_builder.py` (`/reports`), `support_agents.py` (`/support`), `target_recommendation_cockpit.py` (`/target_recommendation`), `what_changed.py`, plus `admin_tier1_misc.py` sub-routers (`/source-trust`, `/export-gate`, `/load-test`) and `admin_tier234.py` sub-routers (`/recommendations`, `/qp-credentials`, `/workspace-members`, `/workspace-settings`, `/audit-explorer`, `/saved-maps`, `/alerts-inbox`, `/phase-h4-health`, `/backups`, `/eval`, `/eval/questions`) |
| Top-level | `maps.py` (`/maps`), `pdf.py` (`/pdf`), `coverage.py` (`/coverage`), `assessment_summary.py` (`/assessment_summary`), `completeness.py` (`/completeness_audit`), `smdi.py` (`/public-geo/smdi`) |

### 5.2 Endpoint catalogue (109 endpoints)

Full inventory in `HANDOVER_MANIFEST.md` §1. Grouped by URL family below.

#### `/api/v1/*` (56 endpoints)

| Method | Path | Handler |
|---|---|---|
| GET | `/api/v1/admin/alerts-inbox` | `admin_tier234.py:693` |
| GET | `/api/v1/admin/audit-explorer/search` | `admin_tier234.py:452` |
| GET | `/api/v1/admin/audit-explorer/verify-chain` | `admin_tier234.py:539` |
| GET | `/api/v1/admin/audit/boundary-violations` | `audit_findings.py:287` |
| GET | `/api/v1/admin/audit/cold-tier-archive-runs` | `audit_findings.py:203` |
| GET | `/api/v1/admin/audit/tenant-isolation-findings` | `audit_findings.py:107` |
| GET | `/api/v1/admin/backups/cold-tier-runs` | `admin_tier234.py:1173` |
| GET | `/api/v1/admin/backups/snapshot-runs` | `admin_tier234.py:1030` |
| GET | `/api/v1/admin/backups/workspace-consistency/{workspace_id}` | `admin_tier234.py:1146` |
| GET | `/api/v1/admin/conflicts/recent` | `conflicts.py:76` |
| GET | `/api/v1/admin/eval/questions` | `admin_tier234.py:1533` |
| GET | `/api/v1/admin/eval/questions/{question_id}` | `admin_tier234.py:1585` |
| GET | `/api/v1/admin/eval/runs` | `admin_tier234.py:1311` |
| GET | `/api/v1/admin/eval/runs/{run_id}/per-set-summary` | `admin_tier234.py:1395` |
| GET | `/api/v1/admin/export-gate/results` | `admin_tier1_misc.py:136` |
| GET | `/api/v1/admin/ml/training-runs` | `ml_training.py:96` |
| GET | `/api/v1/admin/qp-credentials` | `admin_tier234.py:110` |
| GET | `/api/v1/admin/reports/builds` | `report_builder.py:228` |
| GET | `/api/v1/admin/reports/builds/{build_id}` | `report_builder.py:331` |
| GET | `/api/v1/admin/reports/builds/{build_id}/sections/{section_id}/history` | `report_builder.py:492` |
| GET | `/api/v1/admin/reports/types` | `report_builder.py:164` |
| GET | `/api/v1/admin/saved-maps` | `admin_tier234.py:607` |
| GET | `/api/v1/admin/source-trust/scores` | `admin_tier1_misc.py:52` |
| GET | `/api/v1/admin/target_recommendation/runs` | `target_recommendation_cockpit.py:318` |
| GET | `/api/v1/admin/target_recommendation/runs/{run_id}` | `target_recommendation_cockpit.py:212` |
| GET | `/api/v1/admin/target_recommendation/runs/{run_id}/geojson` | `target_recommendation_cockpit.py:259` |
| GET | `/api/v1/admin/what-changed/runs` | `what_changed.py:46` |
| GET | `/api/v1/admin/workspace-members` | `admin_tier234.py:253` |
| GET | `/api/v1/admin/workspace-settings/{workspace_id}` | `admin_tier234.py:345` |
| POST | `/api/v1/admin/alerts-inbox/acknowledge` | `admin_tier234.py:832` |
| POST | `/api/v1/admin/audit/cold-tier-archive` | `audit_findings.py:247` |
| POST | `/api/v1/admin/conflicts/run` | `conflicts.py:124` |
| POST | `/api/v1/admin/eval/assess-promotion` | `admin_tier234.py:1252` |
| POST | `/api/v1/admin/eval/questions` | `admin_tier234.py:1629` |
| POST | `/api/v1/admin/eval/questions/{question_id}/dry-run` | `admin_tier234.py:1806` |
| POST | `/api/v1/admin/eval/questions/{question_id}/transition` | `admin_tier234.py:1722` |
| POST | `/api/v1/admin/ml/train-source-trust` | `ml_training.py:161` |
| POST | `/api/v1/admin/ml/train-target-model` | `ml_training.py:148` |
| POST | `/api/v1/admin/qp-credentials` | `admin_tier234.py:163` |
| POST | `/api/v1/admin/qp-credentials/{qp_credential_id}/verify` | `admin_tier234.py:211` |
| POST | `/api/v1/admin/recommendations/analogue` | `admin_tier234.py:71` |
| POST | `/api/v1/admin/recommendations/nbd` | `admin_tier234.py:52` |
| POST | `/api/v1/admin/reports/build` | `report_builder.py:176` |
| POST | `/api/v1/admin/reports/export` | `report_builder.py:286` |
| POST | `/api/v1/admin/support/agents/customer-response-draft` | `support_agents.py:165` |
| POST | `/api/v1/admin/support/agents/escalation-routing` | `support_agents.py:187` |
| POST | `/api/v1/admin/support/agents/root-cause-investigation` | `support_agents.py:145` |
| POST | `/api/v1/admin/support/agents/support-packet` | `support_agents.py:127` |
| POST | `/api/v1/admin/support/agents/ticket-triage` | `support_agents.py:114` |
| POST | `/api/v1/admin/target_recommendation/runs/{run_id}/signoff` | `target_recommendation_cockpit.py:393` |
| POST | `/api/v1/citations/feedback` | `citation_feedback.py:64` |
| POST | `/api/v1/incidents/diagnose` | `phase0_ops.py:71` |
| POST | `/api/v1/support/packets/assemble` | `phase0_ops.py:119` |
| PUT | `/api/v1/admin/eval/questions/{question_id}` | `admin_tier234.py:1666` |
| PUT | `/api/v1/admin/reports/builds/{build_id}/sections/{section_id}` | `report_builder.py:411` |
| PUT | `/api/v1/admin/workspace-settings/{workspace_id}` | `admin_tier234.py:379` |

#### `/v1/*` (24 endpoints)

| Method | Path | Handler |
|---|---|---|
| GET | `/v1/answer_runs/{answer_run_id}/events` | `answer_runs.py:133` |
| GET | `/v1/answer_runs/{answer_run_id}/lineage` | `answer_runs.py:548` |
| GET | `/v1/answer_runs/{answer_run_id}/trust-summary` | `answer_runs.py:333` |
| POST | `/v1/answer_runs/{answer_run_id}/feedback` | `answer_runs.py:207` |
| GET | `/v1/evidence/{evidence_id}` | `evidence.py:581` |
| GET | `/v1/viz/chart-kinds` | `visualizations.py:766` |
| GET | `/v1/viz/cross_section` | `visualizations.py:328` |
| GET | `/v1/viz/stereonet` | `visualizations.py:460` |
| GET | `/v1/viz/strip_log` | `visualizations.py:192` |
| POST | `/v1/viz/chart` | `visualizations.py:771` |
| POST | `/v1/viz/qa` | `visualizations.py:1004` |
| POST | `/v1/viz/readiness` | `visualizations.py:1051` |
| GET | `/v1/interpretation/notes` | `interpretation.py:146` |
| POST | `/v1/interpretation/notes` | `interpretation.py:193` |
| DELETE | `/v1/interpretation/notes/{note_id}` | `interpretation.py:235` |
| GET | `/v1/interpretation/section-lines` | `interpretation.py:253` |
| POST | `/v1/interpretation/section-lines` | `interpretation.py:296` |
| DELETE | `/v1/interpretation/section-lines/{section_id}` | `interpretation.py:335` |
| GET | `/v1/interpretation/target-zones` | `interpretation.py:353` |
| POST | `/v1/interpretation/target-zones` | `interpretation.py:401` |
| POST | `/v1/interpretation/target-zones/{zone_id}/accept` | `interpretation.py:447` |
| DELETE | `/v1/interpretation/target-zones/{zone_id}` | `interpretation.py:491` |
| GET | `/v1/interpretation/comments` | `interpretation.py:509` |
| POST | `/v1/interpretation/comments` | `interpretation.py:548` |

#### `/internal/v1/*` (8 endpoints)

| Method | Path | Handler |
|---|---|---|
| GET | `/internal/v1/integrations/flows` | `integrations_trigger.py:104` |
| POST | `/internal/v1/integrations/{flow_name}/trigger` | `integrations_trigger.py:116` |
| GET | `/internal/v1/ocr/render` | `ocr_render.py:127` |
| POST | `/internal/v1/metrics/ingestion-event` | `metrics_ingestion_events.py:53` |
| POST | `/internal/v1/mv-refresh/run` | `mv_refresh_trigger.py:76` |
| POST | `/internal/v1/re_ocr_page/trigger` | `re_ocr_trigger.py:45` |
| POST | `/internal/v1/shadow/ingest_pdf/trigger` | `shadow_trigger.py:51` |
| POST | `/internal/v1/shadow/tiff_normalize/trigger` | `shadow_trigger.py:79` |

#### `/internal/*` (5 endpoints)

| Method | Path | Handler |
|---|---|---|
| POST | `/internal/queries` | `queries.py:590` |
| GET | `/internal/projects/{project_id}` | `projects.py:98` |
| GET | `/internal/projects/{project_id}/collars` | `projects.py:167` |
| POST | `/internal/exports/geopackage` | `exports.py:95` |
| POST | `/internal/exports/shapefile` | `exports.py:61` |
| POST | `/internal/outlier-assist` | `outlier_assist.py:129` |

#### Top-level (`/maps`, `/pdf`, `/coverage`, `/assessment_summary`, `/completeness_audit`, `/public-geo/smdi`) — 16 endpoints

| Method | Path | Handler |
|---|---|---|
| POST | `/maps/ingest` | `maps.py:137` |
| POST | `/pdf/render_page` | `pdf.py:221` |
| GET | `/pdf/extract_text` | `pdf.py:372` |
| GET | `/pdf/find_tables` | `pdf.py:438` |
| GET | `/pdf/find_legends` | `pdf.py:562` |
| POST | `/pdf/crop_region` | `pdf.py:655` |
| POST | `/pdf/ocr_region` | `pdf.py:816` |
| GET | `/pdf/summarize_section` | `pdf.py:947` |
| GET | `/pdf/find_coordinates` | `pdf.py:1220` |
| GET | `/coverage/density` | `coverage.py:72` |
| POST | `/assessment_summary/{pdf_id}` | `assessment_summary.py:107` |
| GET | `/assessment_summary/{pdf_id}` | `assessment_summary.py:148` |
| POST | `/completeness_audit/{pdf_id}` | `completeness.py:67` |
| GET | `/completeness_audit/{pdf_id}/latest` | `completeness.py:114` |
| GET | `/public-geo/smdi/features` | `smdi.py:115` |

### 5.3 OpenAPI gating

`OPENAPI_DOCS_PUBLIC` (default `True`) controls whether `/docs`, `/redoc`, and `/openapi.json` are mounted. **Production sets it to `False`** — schema and auth-claim shapes are not exposed even with a valid service key.

### 5.4 Server-side guards

| Middleware | Behaviour |
|---|---|
| `BodySizeLimitMiddleware` | Returns 413 over `MAX_REQUEST_BODY_BYTES` (1 MiB default) |
| `GlobalTimeoutMiddleware` | Returns 504 over `REQUEST_TIMEOUT_S` (30s default) |
| `StructuredAccessLogMiddleware` | JSON access log + W3C traceparent |

Per-resource timeout budgets (`app/config.py`): `TIMEOUT_POSTGIS_S=5`, `TIMEOUT_NEO4J_S=3`, `TIMEOUT_QDRANT_S=2`, `TIMEOUT_RERANKER_S=8`, `TIMEOUT_REDIS_S=0.5`, `TIMEOUT_GATHER_S=8`, `AGENTIC_TIMEOUT_S=10`, `KESTRA_HTTP_TIMEOUT_S=5`, `PAGERDUTY_HTTP_TIMEOUT_S=5`.

### 5.5 FastAPI slowapi rate limits

| Constant | Default | Notes |
|---|---|---|
| `RATE_LIMIT_ENABLED` | `False` | Off by default — Laravel owns user-facing throttling |
| `RATE_LIMIT_DEFAULT` | `60/minute` | When enabled |
| `RATE_LIMIT_QUERIES` | `20/minute` | Tighter bucket for `POST /internal/queries` |

---

## 6. WebSocket / Reverb channels

Reverb on `:8085`, pusher-js protocol. Channel auth in `routes/channels.php`. Echo client config in `resources/js/bootstrap.ts`.

### 6.1 Channel patterns (30 — `routes/channels.php`)

| Channel pattern | Producer | Consumer (Inertia hook) |
|---|---|---|
| `App.Models.User.{id}` | `/internal/v1/user-inbox-updated` | `useUserInbox` |
| `query.{queryId}` | Horizon RAG job (forwarding FastAPI SSE) | Foundry/Chat page-local |
| `workspace.{workspaceId}.activity` | `/internal/v1/workspace-activity` | `useWorkspaceActivity` |
| `project.{projectId}.ingestion` | `/internal/v1/{ingest-progress/broadcast,workspace-data-updated}` | `useWorkspaceDataUpdated` |
| `admin.ingestion-review` | Silver-review queue events | Admin/IngestionReview |
| `admin.reports.{build_id}` | `/internal/admin/reports/{build_id}/progress` | Admin/ReportBuild |
| `admin.workflow-runs` | `/internal/v1/admin-surface-updated` | Admin/WorkflowRuns |
| `admin.cluster-ingest` | `/internal/v1/admin-surface-updated` | Admin/ClusterIngest |
| `admin.target-recommendation` | bridge | Admin/TargetRecommendationCockpit |
| `admin.target-run.{run_id}` | bridge | Admin/TargetRecommendationRuns |
| `admin.reports` | bridge | Admin/ReportBuilder |
| `admin.ml-training` | bridge | Admin/MlTrainingRuns |
| `admin.audit-findings` | bridge | Admin/AuditFindings |
| `admin.alerts-inbox` | bridge | Admin/AlertsInbox |
| `admin.support-cockpit` | bridge | Admin/SupportCockpit |
| `admin.llm-cost` | bridge | (cost dashboard) |
| `admin.cache-telemetry` | bridge | Admin/CacheTelemetry |
| `admin.eval-dashboard` | bridge | Admin/EvalDashboard |
| `admin.conflicts` | bridge | Admin/Conflicts |
| `admin.audit-explorer` | bridge | Admin/AuditExplorer |
| `admin.backups` | bridge | Admin/BackupsDashboard |
| `admin.integrations` | bridge | Admin/Integrations |
| `admin.export-gate` | bridge | Admin/ExportGate |
| `admin.decision-history` | bridge | Admin/DecisionHistory |
| `admin.hypothesis-workspace` | bridge | Admin/HypothesisWorkspace |
| `admin.what-changed` | bridge | Admin/WhatChanged |
| `admin.source-trust` | bridge | Admin/SourceTrust |
| `admin.dashboards-evidence-quality` | bridge | Admin/Dashboards |
| `admin.dashboards-visual-readiness` | bridge | Admin/Dashboards |
| `public-geoscience.tiles` | `/internal/v1/public-geoscience-tiles-invalidated` | `useTileInvalidation` |

### 6.2 Event class catalog (`app/Events/` — 11 files)

All implement `ShouldBroadcastNow` (synchronous broadcast).

| Event class | Channel | `broadcastAs()` |
|---|---|---|
| `QueryStreamEvent` | `query.{queryId}` | `QueryStreamEvent` |
| `IngestionProgressBroadcast` | `project.{projectId}.ingestion` | `ingestion.progress` |
| `WorkspaceDataUpdated` | `project.{projectId}.ingestion` | `workspace.data_updated` |
| `Workspace\WorkspaceActivityBroadcast` | `workspace.{workspaceId}.activity` | `workspace.activity` |
| `User\UserInboxUpdated` | `App.Models.User.{userId}` | `user.inbox_updated` |
| `Map\PublicGeoscienceTilesInvalidated` | `public-geoscience.tiles` | `public_geoscience.tiles_invalidated` |
| `Admin\AdminSurfaceUpdated` | dynamic (e.g. `admin.workflow-runs`) | `admin.surface_updated` |
| `Admin\IngestionReviewDispositionChanged` | `admin.ingestion-review` | `IngestionReviewDispositionChanged` |
| `Admin\ReportBuildProgress` | `admin.reports.{buildId}` | `ReportBuildProgress` |
| `Dashboard\ActivityEventBroadcast` | (dashboard surfaces) | `ActivityEventBroadcast` |
| `Dashboard\DocumentStageChanged` | (dashboard surfaces) | `DocumentStageChanged` |

Naming-convention drift: snake-dot for newer events, PascalCase for older ones. Frontend hooks key off the broadcast-name string.

#### 6.2.1 `broadcastWith()` payload shapes

Field shapes consumed by frontend hooks. All payloads include `timestamp` or `updated_at` ISO-8601.

| Event | Payload fields |
|---|---|
| `QueryStreamEvent` | Arbitrary `$this->payload` array — caller decides shape. SSE token-chunk vocabulary: `{type: 'delta'\|'completed'\|'failed', ...}` carried as the payload. |
| `IngestionProgressBroadcast` | `workspace_id`, `project_id`, `pipeline_run_id`, `stage`, `status`, `message`, `pct`, `timestamp` |
| `WorkspaceDataUpdated` | `workspace_id`, `project_id`, `pipeline_run_id`, `affected_types[]`, `data_version` (monotonic BIGINT — drives `useWorkspaceDataUpdated` strict-greater-than partial-reload gate), `updated_at` |
| `Workspace\WorkspaceActivityBroadcast` | `workspace_id`, `affected_types[]`, `payload` (free-form), `updated_at` |
| `User\UserInboxUpdated` | `user_id`, `kind`, `count_delta`, `payload` (free-form), `updated_at` |
| `Map\PublicGeoscienceTilesInvalidated` | `jurisdiction_epoch`, `source_ids[]`, `updated_at` — frontend uses `jurisdiction_epoch` as MapLibre `sourceCache` invalidation key |
| `Admin\AdminSurfaceUpdated` | `surface`, `surface_id`, `affected_props[]`, `payload` (free-form), `timestamp` |
| `Admin\IngestionReviewDispositionChanged` | `review_item_id`, `report_id`, `page`, `new_status`, `reason`, `actor_id`, `re_ocr_triggered`, `timestamp` |
| `Admin\ReportBuildProgress` | `build_id`, `stage`, `section_id`, `message`, `sections_completed`, `sections_total`, `timestamp` |
| `Dashboard\ActivityEventBroadcast` | Arbitrary `$this->payload` array. |
| `Dashboard\DocumentStageChanged` | `document_id`, `old_stage`, `new_stage`, `timestamp` |

**Critical contract:** `WorkspaceDataUpdated.data_version` MUST be a strict-monotonic BIGINT. Trigger `workspaces_data_version_monotonic` rejects decrements at PG level. Hook `useWorkspaceDataUpdated` compares against the version embedded in the rendered Inertia page and only fires partial reload on strict `>`. See DFS §7.4.

### 6.3 Caddy → Kestra SSO bridge

Caddy listens on `:8087` (HTTP) + `:8443` (HTTPS, internal CA / ACME-swappable) and proxies to `kestra:8080`. Per request: `forward_auth` sub-request to `laravel-octane:80/internal/sanctum/check`.

| Endpoint | Method | Auth | Behaviour |
|---|---|---|---|
| `/internal/sanctum/check` | GET | Cookie / Bearer (Sanctum) + `admin` Gate | Returns 204 + `X-Kestra-Auth: Basic <base64>` for authenticated admins; non-200 blocks the forward |
| `<caddy>/*` → `kestra:8080/*` | any | Indirect (above) | Caddy rewrites upstream `Authorization` to Kestra's basic-auth credential |
| `<caddy>/healthz` | GET | None | `ok` 200 |

In-app SSO forward (Laravel-side): `ANY /admin/integrations/kestra/{path?}`. CD-side: [`CICD_PIPELINE.md`](CICD_PIPELINE.md) §6.9 split rule.

### 6.4 Echo + Reverb identity

**Echo client** (`resources/js/bootstrap.ts`):
- `broadcaster: 'reverb'`
- `key: VITE_REVERB_APP_KEY` (defaults to `georag-reverb-key`)
- `wsPort`/`wssPort`: `VITE_REVERB_PORT` (default `8085`)
- `forceTLS`: `VITE_REVERB_SCHEME === 'https'`

**Server-side identity**: `REVERB_APP_ID=georag-app`, `REVERB_APP_KEY=georag-reverb-key`, `REVERB_APP_SECRET=georag-reverb-secret`, `REVERB_SERVER_HOST=0.0.0.0`, `REVERB_SERVER_PORT=8080` (host map → 8085).

**Per-app config** (`config/reverb.php::apps[0]`): `ping_interval=60s`, `activity_timeout=30s`, `max_message_size=10_000` bytes, `accept_client_events_from='members'`, `pulse_ingest_interval=15s`, `telescope_ingest_interval=15s`, rate-limiting opt-in (60 attempts / 60s decay). Server-level `REVERB_MAX_REQUEST_SIZE=10_000` caps broadcast payloads.

**Scaling**: `REVERB_SCALING_ENABLED` opt-in uses Redis pub/sub on the `reverb` channel.

---

## 7. Tile API (Martin)

- **Container**: `ghcr.io/maplibre/martin:1.7.0`. Host port `MARTIN_PORT=3002` (container `3000`).
- **DB connection**: direct to `postgresql:5432` (NOT PgBouncer) per §04d-tile — tile cache benefits from long-lived connections.
- **Role**: `martin_ro` (silver MVT functions SELECT only).
- **Config**: `docker/martin/martin.yaml` — `keep_alive: 75`, `worker_processes: 2`, `cache_size_mb: 512` (split into tile/sprite/font caches: 256/64/64 MB), `pool_size: 20`.
- **URL shape**: `http://<host>:3002/{function}/{z}/{x}/{y}`.

### 7.1 Silver per-project MVT functions (8)

`silver.pg_collars_by_project`, `silver.pg_drill_traces_by_project`, `silver.pg_formations_by_project`, `silver.pg_geochem_by_project`, `silver.pg_boundaries_by_project`, `silver.pg_cross_section_lines_by_project`, `silver.pg_historic_workings_by_project`, `silver.pg_seismic_by_project`.

Plus `silver.density_choropleth_h3` (H3 hexagonal binning via `h3` + `h3_postgis` extensions) and `silver.significant_intersections_by_project`.

### 7.2 Public-geo MVT functions (8)

`public_geo.pg_mines_tiles`, `public_geo.pg_mineral_occurrences_tiles`, `public_geo.pg_mineral_dispositions_tiles`, `public_geo.pg_drillhole_collars_tiles`, `public_geo.pg_assessment_surveys_tiles`, `public_geo.pg_bedrock_geology_tiles`, `public_geo.pg_resource_potential_tiles`, `public_geo.pg_rock_samples_tiles`.

All MVT functions emit `ST_AsMVT(tile, '<layer>', 4096, 'geom')` at standard 4096-extent.

### 7.3 Cache-bust epoch

Cache invalidation epoch propagated via the `public_geoscience.tiles_invalidated` Reverb event (`Map\PublicGeoscienceTilesInvalidated`). MapLibre client re-issues `setTiles(...)` with `?v={epoch}` cache-bust.

> **Flagged**: martin.yaml targets the `public_geo` schema; canonical Phase-0 name is `public_geoscience`. Rename pending — see [`HANDOVER_INDEX.md`](HANDOVER_INDEX.md) §5.1.

---

## 8. Contracts

### 8.1 Refusal code vocabulary (`lang/en/guard_errors.php`)

25 typed codes rendered client-side via the `guard_errors` Inertia prop (shipped at boot — no round-trip). Categories:

- **Citation / evidence**: `CITATION_INCOMPLETE`, `NO_EVIDENCE_FOUND`, `NUMERIC_GROUNDING_FAILED`, `CONFLICTING_SOURCES`, `CONFLICTING_SOURCES_WITH_AUTHORITY`.
- **Entity resolution**: `ENTITY_NOT_FOUND`, `ENTITY_NOT_FOUND_NO_ALIASES`, `AMBIGUOUS_HOLE_ID`, `AMBIGUOUS_PROPERTY_NAME`, `AMBIGUOUS_FORMATION_NAME`.
- **Schema / data shape**: `MISSING_ASSAY_UNITS`, `MISSING_DEPTH_INTERVAL`, `REQUEST_DEPTH_CLARIFICATION`.
- **Pipeline / graph**: `GRAPH_PATH_NOT_FOUND`, `OVER_FILTERED_QUERY`, `DEATH_LOOP`.
- **Partial-answer scaffolding**: `PARTIAL_ANSWER_HEADER`, `PARTIAL_ANSWER_EVIDENCE_LABEL`, `PARTIAL_ANSWER_MISSING_LABEL`, `PARTIAL_ANSWER_SUGGESTION_LABEL`.

The FastAPI-side `GuardErrorCode` enum (`src/fastapi/app/agent/guards.py`) holds 18 values mapped to repair strategies via `STRATEGY_FOR_CODE` (`src/fastapi/app/agent/repair_strategy.py` — 13 strategies). Repair-loop architecture: `docs/architecture/repair_loop_spec.md`.

### 8.2 External-notification HMAC envelope

Senders compute HMAC-SHA256 (hex) over canonical-JSON of `{notification_id, source, kind, payload, received_at}` (sorted keys, no whitespace, UTF-8) using `EXTERNAL_NOTIFICATION_HMAC_SECRET`, and pass it as `signature` in the POST body. Kestra `external_notification.yaml` webhook trigger forwards verbatim to FastAPI `/internal/v1/integrations/external_notification/trigger` with the per-flow JWT. The Hatchet `external_notification` workflow re-verifies HMAC on the receiving side; tampered or unsigned payloads short-circuit with `skipped=true, reason='hmac_verification_failed:...'`.

### 8.3 Trust-Summary 7-section payload

`GET /api/v1/answer-runs/{id}/trust-summary` proxied to `GET /v1/answer_runs/{id}/trust-summary`. Returns:

1. **Header** — `answer_run` metadata + final answer.
2. **Citation summary** — count per `source_store` + per source.
3. **Retrieval summary** — what was fetched per stage.
4. **Citation rows** — sample for the "Sources" section.
5. **User feedback** on this run (optional).
5b. **Claim ledger summary** (§7.4).
6. **Aggregate confidence** — heuristic from citation breadth.
7. **Inferred missing data** — when `partial_resolution_rate < 1.0`.

Auth: Laravel mints short-TTL JWT carrying user + project context for the service-to-service call.

### 8.4 Form Request validation

**`StoreQueryRequest`** (`POST /api/v1/queries`):
- `query` — required, string, `max:2000`.
- `project_id` — required, uuid, DB existence check (404 if not found).
- `context_envelope` — optional 12-field array:
  - `area_of_interest` — nullable string `max:500`.
  - `crs_epsg` — nullable integer 1024–32767.
  - `depth_reference` — nullable in `bgl,asl,rl,tvd,md`.
  - `scale_resolution` — nullable string `max:64`.
  - `stratigraphic_frame` — nullable string `max:200`.
  - `specific_objects[]` — strings `max:128`.
  - `data_sources[]` — in `drill_logs,assays,technical_reports,maps,geophysics,public_geoscience`.
  - `qaqc_constraints` — nullable string `max:500`.
  - `units_and_detection_limits` — nullable string `max:500`.
  - `reporting_code` — nullable in `NI 43-101,CIM,CRIRSCO,JORC,SAMREC,PERC`.
  - `decision_to_support` — nullable string `max:500`.
  - `desired_output_structure` — nullable string `max:200`.
  - `mode` — nullable in `field,office`.

FastAPI mirror: `ContextEnvelope` typed model with same enums and defaults (`DEFAULT_QUERY_MODE="office"`, `DEFAULT_REPORTING_CODE="NI 43-101"`).

**`StoreExportRequest`** (`POST /api/v1/projects/{project}/exports`):
- `export_type` — required, in 10 formats: `csv_collars`, `csv_samples`, `csv_assays`, `csv_lithology`, `csv_geochem`, `csa_bundle`, `shapefile`, `geopackage`, `dxf`, `las_bundle`. Lockstep with `App\Jobs\GenerateExportJob::generate`.
- `filters` — optional array:
  - `hole_id` — nullable string `max:64`.
  - `hole_type` — nullable in `Diamond,RC,RAB,Rotary,Percussion`.
  - `status` — nullable in `Active,Completed,Abandoned`.

**`POST /api/v1/charts/render`** — `chart_kind` enum: `long_section`, `harker_diagram`, `spider_diagram`, `ree_pattern`, `ternary_diagram`, `grade_tonnage`, `anomaly_map`, `target_heatmap` (`ChartsGalleryController::KNOWN_CHARTS`). Backend renders via FastAPI `POST /v1/viz/chart`.

### 8.5 Security response headers

`SecurityHeadersMiddleware` (`app/Http/Middleware/SecurityHeadersMiddleware.php`):

Always emitted:
- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`
- `Permissions-Policy: geolocation=(), microphone=(), camera=(), payment=()`

Conditional:
- `Strict-Transport-Security: max-age=31536000; includeSubDomains` — HTTPS only (preserves local HTTP dev).
- `Content-Security-Policy` — `buildCsp($env)`:

```
default-src 'self';
script-src 'self' 'unsafe-inline' 'unsafe-eval';
style-src 'self' 'unsafe-inline' https://fonts.bunny.net;
img-src 'self' data: blob: https:;
connect-src 'self' wss: ws:
  https://tiles.openfreemap.org
  https://demotiles.maplibre.org
  https://basemaps.cartocdn.com https://*.basemaps.cartocdn.com
  https://s3.amazonaws.com
  https://server.arcgisonline.com;
font-src 'self' data: https://fonts.bunny.net;
worker-src 'self' blob:;
frame-ancestors 'none';
base-uri 'self';
form-action 'self';
object-src 'none';
```

`'unsafe-eval'` required by MapLibre worker shim + Plotly evaluation paths; nonce-based tightening is a tracked Module-10 hardening item.

**CORS** (`config/cors.php`):
- `paths: ['api/*', 'sanctum/csrf-cookie']`
- `allowed_methods: [GET, POST, PUT, PATCH, DELETE, OPTIONS]`
- `allowed_origins` — env CSV, defaults to 7 localhost variants.
- `allowed_headers` — 11 including 9 needed for Inertia handshake: `Accept`, `Authorization`, `Content-Type`, `X-CSRF-TOKEN`, `X-XSRF-TOKEN`, `X-Inertia`, `X-Inertia-Version`, `X-Inertia-Partial-Component`, `X-Inertia-Partial-Data`, `X-Requested-With`, `X-Request-ID`.
- `exposed_headers: ['X-Request-ID', 'Server-Timing']`
- `max_age: 0` (no preflight cache — raise for prod once origins stable)
- `supports_credentials: true`

---

## 9. Versioning + deprecation

- **Laravel API** — versioned under `/api/v1/*`. No v2 routes.
- **FastAPI** — internal-only; mixed family prefixes (`/internal`, `/internal/v1`, `/v1`, `/api/v1`, `/api/v1/admin`). Living OpenAPI from running container is single source of truth when `OPENAPI_DOCS_PUBLIC=true`.
- **OpenAPI snapshot** at `docs/api/openapi.json` covers ~10 of 109 endpoints — regenerate by hitting `GET /openapi.json` on the FastAPI container.
- **Reverb event names** — naming-convention drift documented in §6.2 (snake-dot vs PascalCase). Both conventions coexist; consumers key off the broadcast string.

---

## 10. Needs Confirmation

Consolidated ledger owned by [`HANDOVER_INDEX.md`](HANDOVER_INDEX.md) §5. API-surface items needing operator/SME confirmation are aggregated there.

---

*End of `API_DOCUMENTATION.md`.*
