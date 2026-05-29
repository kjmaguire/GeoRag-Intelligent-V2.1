# API Documentation — GeoRAG Intelligence V1.0

> Inferred from `routes/api.php`, `routes/web.php`, `routes/channels.php`,
> `src/fastapi/app/routers/*`, and the partial `docs/api/openapi.json`.
> The OpenAPI file on disk covers only a subset of FastAPI's `/internal/*`
> surface; the full inventory below was derived by enumerating the router
> module list and Laravel route file.

---

## 1. API surfaces at a glance

| Surface             | Base path        | Auth                                          | Audience                              |
| ------------------- | ---------------- | --------------------------------------------- | ------------------------------------- |
| Laravel SPA         | `/`              | Sanctum SPA session (cookie)                  | Browser — Inertia-rendered pages      |
| Laravel public API  | `/api/v1/*`      | Sanctum Bearer token (or SPA session)         | First-party SPA + external API users  |
| Laravel `/internal` | `/internal/v1/*` | `X-Service-Key: FASTAPI_SERVICE_KEY` header   | FastAPI callbacks only                |
| FastAPI domain svc  | `/internal/*`    | `X-Service-Key` + optional forwarded `Authorization` JWT | Laravel + Hatchet + Dagster jobs |
| WebSocket           | `:8085`          | Sanctum-authorized channel subscriptions      | Browser real-time                     |
| Tile server         | Martin port      | Postgres role `martin_ro`                     | MapLibre client                       |

All Laravel routes go through Octane on port `APP_PORT` (default `8888`).
FastAPI listens on `8000`. Reverb listens on `8085`.

---

## 2. Authentication

### 2.1 SPA cookie flow

1. `GET /sanctum/csrf-cookie` — primes XSRF-TOKEN cookie.
2. `POST /api/v1/auth/spa-login` — credential exchange; session cookie
   is the credential. No token is returned.
3. Subsequent requests include the cookie automatically.

### 2.2 Token flow (mobile / third-party)

1. `POST /api/v1/auth/login` — returns
   `{ token: "<plain-text token>", token_type: "Bearer", user: {...} }`.
2. Subsequent requests send `Authorization: Bearer <token>`.

### 2.3 Service-to-service (Laravel ↔ FastAPI)

- Laravel → FastAPI: header `X-Service-Key: $FASTAPI_SERVICE_KEY` plus a
  short-lived JWT minted by `App\Services\FastApiJwtMinter` (kid-rotation
  capable) in the `Authorization` header.
- FastAPI → Laravel `/internal/*`: header `X-Service-Key: $FASTAPI_SERVICE_KEY`.
  No JWT required on the inbound side — service key is the only
  credential.

### 2.4 Rate limits

| Limiter        | Scope                       | Budget       |
| -------------- | --------------------------- | ------------ |
| `auth-login`   | email + IP                  | 5 / minute   |
| `throttle:3,1` | register (per IP)           | 3 / minute   |
| `queries`      | user (shared across phases) | per `AppServiceProvider::boot` (not enumerated here) |
| `bridge:report-progress` | per build_id      | applied to `/internal/admin/reports/{build_id}/progress` |

---

## 3. Laravel public API — `/api/v1/*`

All routes below require `auth:sanctum` unless explicitly marked public.

### 3.1 Auth

| Method | Path                          | Notes                                              |
| ------ | ----------------------------- | -------------------------------------------------- |
| POST   | `/api/v1/auth/register`       | Public. Rate limit 3/min.                          |
| POST   | `/api/v1/auth/login`          | Public. Rate limit `auth-login`. Returns Bearer token. |
| POST   | `/api/v1/auth/spa-login`      | Public. Rate limit `auth-login`. Cookie session.   |
| POST   | `/api/v1/auth/logout`         | Revokes current token / clears session.            |
| GET    | `/api/v1/auth/me`             | Current user profile.                              |

### 3.2 Projects + drill data

| Method | Path                                                                | Purpose                                           |
| ------ | ------------------------------------------------------------------- | ------------------------------------------------- |
| `apiResource` | `/api/v1/projects`                                          | Full CRUD; scoped to user's memberships in controller. |
| `apiResource` (`index|store|show|destroy`) | `/api/v1/projects/{project}/collars` | Scoped nested resource. |
| GET    | `/api/v1/projects/{projectId}/holes/{holeIdOrCollarId}/analysis`    | Per-hole analysis bundle (surveys + structures + geochem). |
| GET    | `/api/v1/projects/{projectId}/coverage-density`                     | CC-03 Item 5 — GeoJSON heatmap.                  |
| `apiResource` (`index|store|show`) | `/api/v1/projects/{project}/exports`         | Dispatch Horizon export jobs, poll status.        |
| GET    | `/api/v1/exports/{export}/download`                                 | Pre-signed redirect to artifact.                  |
| POST   | `/api/v1/projects/{project}/upload`                                 | Generic upload to bronze bucket → Dagster sensor. |
| GET    | `/api/v1/upload/categories`                                         | Supported categories.                             |
| POST   | `/api/v1/projects/{slug}/drill-uploads`                             | CC-01 Item 1 — slug-routed drill upload, synchronous Dagster GraphQL dispatch. |

### 3.3 RAG queries + chat

| Method | Path                                          | Purpose                                          |
| ------ | --------------------------------------------- | ------------------------------------------------ |
| POST   | `/api/v1/queries`                             | Two-phase phase 1 — reserve `queryId` + channel. |
| POST   | `/api/v1/queries/{queryId}/start`             | Phase 2 — dispatch Horizon job.                  |
| GET    | `/api/v1/citations/resolve`                   | Resolve source text for a `source_chunk_id`.     |
| GET    | `/api/v1/conversations`                       | List chat conversations for user.                |
| GET    | `/api/v1/conversations/{conversationId}`      | Single conversation.                             |
| PUT    | `/api/v1/conversations/{conversationId}`      | Upsert (localStorage → server sync).             |
| DELETE | `/api/v1/conversations/{conversationId}`      | Delete conversation.                             |

### 3.4 Trust / interpretation / charts

| Method | Path                                                  | Purpose                                                |
| ------ | ----------------------------------------------------- | ------------------------------------------------------ |
| GET    | `/api/v1/answer-runs/{id}/trust-summary`              | §19.2 Trust Inspector — proxies FastAPI 7-section payload. |
| ANY    | `/api/v1/interpretation/{tail}`                       | §19.3 — proxy GET/POST/PUT/DELETE for notes / section-lines / target-zones / comments. |
| POST   | `/api/v1/charts/render`                               | §17.3 Charts Gallery — render any of 8 chart kinds.     |

### 3.5 Ingestion + vendor profiles

| Method | Path                                                        | Purpose                                            |
| ------ | ----------------------------------------------------------- | -------------------------------------------------- |
| GET    | `/api/v1/ingest-progress/{run_id}`                          | Polling fallback for `silver.ingest_progress`. UUID-only. |
| `apiResource` | `/api/v1/vendor-profiles`                            | Global column-mapping profiles.                    |
| `apiResource` (`index|store|update|destroy`) | `/api/v1/vendor-profiles/{vendor_profile}/column-mappings` | Per-profile column maps. |

### 3.6 Public REST API breadth (§3.3)

| Method | Path                                                | Purpose                                       |
| ------ | --------------------------------------------------- | --------------------------------------------- |
| GET    | `/api/v1`                                           | Self-describing index.                        |
| GET    | `/api/v1/openapi.json`                              | OpenAPI document.                             |
| GET    | `/api/v1/answers/{answer_run_id}`                   | Single answer-run.                            |
| GET    | `/api/v1/maps/{project_id}/layers`                  | Map layer descriptors.                        |
| GET    | `/api/v1/reports`                                   | List reports.                                 |
| GET    | `/api/v1/targets/{project_id}`                      | Target recommendations.                       |
| GET    | `/api/v1/interpretations/{project_id}`              | Interpretations.                              |
| GET    | `/api/v1/audit/{workspace_id}`                      | Workspace audit feed.                         |
| GET    | `/api/v1/usage/{workspace_id}`                      | Usage metrics.                                |
| GET    | `/api/v1/webhooks`                                  | Webhook registry.                             |

### 3.7 Public Geoscience (§10)

All under `/api/v1/public-geoscience/`:

| Method | Path                                                                  | Notes                                                                          |
| ------ | --------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| GET    | `jurisdictions`                                                       | Read-only jurisdiction registry.                                               |
| GET    | `health`                                                              | Health check.                                                                  |
| GET    | `features/{layer}/{feature_id}`                                       | Single-feature drill-in. `layer` validated against `LAYER_TABLES` registry.    |
| GET    | `entities/{canonical_type}/{pg_id}/references`                        | `canonical_type` ∈ `mine|mineral_occurrence|drillhole_collar|resource_potential_zone|rock_sample|assessment_survey|mineral_disposition`. |
| GET    | `documents/{report_id}/references`                                    | Cross-corpus linker drill-in (plan §07d).                                      |

### 3.8 Dashboard (§3–§4)

All under `/api/v1/dashboard/`:

- `GET platform-readiness`
- `GET portfolio/kpis`, `portfolio/projects`, `portfolio/query-activity`,
  `portfolio/ingestion-health`, `portfolio/feedback`, `portfolio/activity`
- `GET projects/{slug}/header`, `projects/{slug}/kpis`,
  `projects/{slug}/aoi`, `projects/{slug}/kg-counts`,
  `projects/{slug}/recent-queries`, `projects/{slug}/feedback`,
  `projects/{slug}/documents`, `projects/{slug}/drill-summary`,
  `projects/{slug}/analytics`
- `GET projects/by-id/{projectId}/context` — D6 banner aggregator.

---

## 4. Laravel `/internal/*` — FastAPI → Laravel bridge

Service-key only. All routes are under `/internal/*` (no `/api` prefix)
and protected by the `service.key` middleware.

| Method | Path                                                                | Purpose                                                          |
| ------ | ------------------------------------------------------------------- | ---------------------------------------------------------------- |
| POST   | `/internal/admin/reports/{build_id}/progress`                       | Report build progress fan-out. Throttled per build_id.           |
| POST   | `/internal/v1/ingest-progress/broadcast`                            | Reliability Spec Phase 1 — ingestion progress.                   |
| POST   | `/internal/v1/workspace-data-updated`                               | Non-ingestion workspace updates (score_targets etc.).            |
| POST   | `/internal/v1/admin-surface-updated`                                | Phase 2 generic admin surface push.                              |
| POST   | `/internal/v1/workspace-activity`                                   | Phase 3 — Portfolio + Projects activity push.                    |
| POST   | `/internal/v1/user-inbox-updated`                                   | Phase 3 — per-user inbox + nav badge.                            |
| POST   | `/internal/v1/public-geoscience-tiles-invalidated`                  | Phase 4 — tile cache-bust epoch bump.                            |

---

## 5. FastAPI domain service — `/internal/*`

Mounted under `/internal` (applied in `main.py` not in router files).
All routes require `X-Service-Key`. Many also accept a forwarded
Sanctum `Authorization` header for workspace/user attribution.

The router module map below is taken directly from
`src/fastapi/app/routers/__init__.py` and `main.py` imports. Where the
on-disk `docs/api/openapi.json` covers a path, the request/response
schema is authoritative — see that file. Otherwise the path is listed
with intent and the contract is **inferred** from the router file name.

| Router module                          | Representative paths                                                  | Purpose                                                                       |
| -------------------------------------- | --------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| `queries`                              | `POST /internal/queries`                                              | Submit a geological RAG query; streams SSE.                                   |
| `projects`                             | `GET /internal/projects/{project_id}` <br/> `GET /internal/projects/{project_id}/collars` | Project metadata + collars for agent context.                  |
| `answer_runs`                          | `GET /internal/answer_runs/{id}` <br/> `GET /internal/answer_runs/{id}/trust-summary` | Run inspection + trust payload (proxied via Laravel).         |
| `evidence`                             | `GET /internal/evidence/*`                                            | Evidence-fetch tool surface for the agent.                                    |
| `outlier_assist`                       | `POST /internal/outlier-assist`                                       | Outlier triage assistant.                                                     |
| `coverage`                             | `GET /internal/coverage/density`                                      | CC-03 Item 5 coverage density.                                                |
| `completeness`                         | `GET /internal/completeness/*`                                        | CC-03 Item 2 completeness audit.                                              |
| `exports`                              | `POST /internal/exports/geopackage` <br/> `POST /internal/exports/shapefile` | Build export artifacts (Horizon-orchestrated on the Laravel side).     |
| `pdf` / `ocr_render` / `re_ocr_trigger`| `POST /internal/v1/ocr/render` <br/> `POST /internal/v1/re_ocr_page/trigger` | PDF render + per-page OCR + re-OCR escape hatch.                       |
| `report_builder` / `assessment_summary`| `POST /internal/report/build` <br/> `POST /internal/assessment-summary` | Long-form report + assessment-report structured summary.                  |
| `maps` / `visualizations`              | `POST /internal/maps/*` <br/> `POST /internal/visualizations/*`       | Map ingest stub + Plotly chart compute.                                       |
| `interpretation`                       | `GET|POST|PUT|DELETE /internal/interpretation/*`                       | Notes, section-lines, target-zones, comments.                                 |
| `support_agents`                       | `POST /internal/support/*`                                            | Phase G.5 support cockpit agents.                                             |
| `target_recommendation_cockpit`        | `POST /internal/target-recommendation/*`                              | Phase H4 §8 UI surface.                                                       |
| `ml_training`                          | `POST /internal/ml/training/*`                                        | Phase H4 §12.                                                                 |
| `citation_feedback`                    | `POST /internal/citations/feedback`                                   | Phase H4 §12.8.                                                               |
| `conflicts` / `audit_findings` / `what_changed` | `GET /internal/conflicts/*` <br/> `GET /internal/audit/findings` <br/> `GET /internal/what-changed` | Phase H4 audit + reasoning UIs.                       |
| `admin_tier1_misc` / `admin_tier234`   | `/internal/admin/tier1/*` <br/> `/internal/admin/tier{2,3,4}/*`        | Source-trust + export-gate + k6 + recommendations + QP + members + settings + AP + audit + maps. |
| `phase0_ops`                           | `/internal/phase0/*`                                                  | Phase 0 operator surfaces.                                                    |
| `shadow_trigger`                       | `POST /internal/v1/shadow/ingest_pdf/trigger`                         | Shadow-ingest trigger for parser experiments.                                 |
| `mv_refresh_trigger`                   | `POST /internal/v1/mv_refresh/trigger`                                | Materialized view refresh trigger.                                            |
| `integrations_trigger`                 | `POST /internal/v1/integrations/{flow_name}/trigger` <br/> `GET /internal/v1/integrations/flows` | Kestra flow trigger + flow registry.            |
| `metrics_ingestion_events`             | `POST /internal/metrics/ingestion-events`                             | Metrics emit endpoint.                                                        |
| `smdi`                                 | `GET /internal/smdi/features`                                         | SMDI ingestion plan v1.1 Phase 6.                                             |

> **OpenAPI authority**: `src/fastapi/app/main.py` is a standard FastAPI
> app — calling `GET /openapi.json` against the running container returns
> the live OpenAPI document, which is the single source of truth.
> `docs/api/openapi.json` is a frozen snapshot.

---

## 6. WebSocket channels

Reverb on `:8085`, pusher-js protocol. Channel auth is defined in
`routes/channels.php`.

| Channel pattern                              | Auth rule                                          | Producer                                                |
| -------------------------------------------- | -------------------------------------------------- | ------------------------------------------------------- |
| `private-App.Models.User.{id}`               | Default — `$user->id === (int) $id`                | UserInboxBridge / per-user notifications                |
| `private-query.{queryId}`                    | Caller owns the query                              | Horizon RAG job (forwarding FastAPI SSE)                |
| `private-workspace.{workspaceId}.activity`   | Caller is workspace member                         | WorkspaceActivityBridge                                 |
| `private-project.{projectId}.ingestion`      | Caller can access project                          | IngestionProgressBroadcast / WorkspaceDataUpdated       |
| `private-admin.*`                            | Admin role gate                                    | AdminSurfaceUpdated                                     |
| `private-admin.reports.{build_id}`           | Admin                                              | ReportBuildProgress                                     |
| `private-admin.ingestion-review`             | Admin                                              | Silver review queue events                              |

Events broadcast: `App\Events\Workspace\WorkspaceActivityBroadcast`,
`App\Events\Admin\AdminSurfaceUpdated`,
`App\Events\User\UserInboxUpdated`,
`App\Events\Map\PublicGeoscienceTilesInvalidated`,
plus query.* events for streaming answers.

---

## 7. Tile API (Martin)

- Martin serves Mapbox Vector Tiles (MVT) from PostGIS functions in
  the `silver` schema, granted to the `martin_ro` role.
- URL shape: typically `https://<host>/tiles/{source}/{z}/{x}/{y}.pbf`
  — exact mount is set by the Caddy reverse proxy
  (`docker/martin/` + `caddy/`).
- Cache-bust epoch is propagated via the
  `public_geoscience_tiles_invalidated` Reverb event.

---

## 8. Versioning + deprecation

- **Laravel API** — versioned under `/api/v1/*`. No v2 routes observed.
- **FastAPI** — internal-only; no explicit `/v1` prefix on most routes
  (some operator triggers use `/v1/...` because they were added later).
- **OpenAPI snapshot** — `docs/api/openapi.json` is partial; regenerate
  by hitting the live `GET /openapi.json` on the FastAPI container.

---

## 9. Missing / Needs Confirmation

- **Detailed request/response shapes** — for routes outside the
  on-disk OpenAPI snapshot, the contract lives in the controller /
  router code + Pydantic models / FormRequest classes. A full
  request/response catalog was not generated in this pass.
- **Public-API authentication for external callers** — `PublicApiController`
  is mounted inside the `auth:sanctum` group, so external API
  consumers need a Sanctum personal-access token. There is no API-key
  or OAuth2 client-credentials grant observed.
- **Webhooks** — `GET /api/v1/webhooks` advertises a webhook registry;
  the registration/dispatch model lives in Kestra. Webhook subscription
  CRUD endpoints were not enumerated.
- **OpenAPI completeness** — only `queries`, `projects`,
  `outlier-assist`, `exports/geopackage`, `exports/shapefile`,
  `v1/ocr/render`, `v1/re_ocr_page/trigger`,
  `v1/shadow/ingest_pdf/trigger`, `v1/integrations/flows`,
  `v1/integrations/{flow_name}/trigger` appear in the on-disk
  `openapi.json`. The remaining ~25 routers expose paths via FastAPI's
  live introspection only.
- **Rate-limit budgets for `queries`** — exact rpm budget is set in
  `AppServiceProvider::boot`; not enumerated here.
