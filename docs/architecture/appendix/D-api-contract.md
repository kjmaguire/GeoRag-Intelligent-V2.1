# Appendix D — API Contract

Status: **Draft.** Manually curated inventory of the cross-process API
surface. The full per-endpoint spec must come from generated OpenAPI; this
appendix defines the contract and points at the source files so a
generator can be wired without re-discovering the surface.

## 1. Where the surface lives

| Process | Routes / handlers | Spec source |
|---|---|---|
| **Laravel** (Octane) | [routes/api.php](../../../routes/api.php) (324 lines), [routes/web.php](../../../routes/web.php) (752 lines), [routes/channels.php](../../../routes/channels.php) (171 lines) | `php artisan route:list --json` |
| **FastAPI** | [src/fastapi/app/main.py](../../../src/fastapi/app/main.py) + [src/fastapi/app/routers/*.py](../../../src/fastapi/app/routers/) (31 router modules) | `GET /openapi.json` |
| **Hatchet** | Workflow inputs (not HTTP) — see [Appendix B](B-event-payloads.md) | Pydantic models in [src/fastapi/app/hatchet_workflows/*.py](../../../src/fastapi/app/hatchet_workflows/) |
| **Kestra** | Per-flow webhook URLs | [kestra/flows/georag/*.yaml](../../../kestra/flows/georag/) |

## 2. Auth modes

| Mode | Header / cookie | Used between | Verifier |
|---|---|---|---|
| **Sanctum SPA** | Session cookie + CSRF token (`X-XSRF-TOKEN`) | browser → laravel-octane | `EnsureFrontendRequestsAreStateful` middleware |
| **Sanctum PAT** | `Authorization: Bearer <pat>` | external clients → laravel-octane | `auth:sanctum` middleware |
| **X-Service-Key** | `X-Service-Key: <FASTAPI_SERVICE_KEY>` | laravel ↔ fastapi, hatchet workers → laravel | [src/fastapi/app/services/auth.py](../../../src/fastapi/app/services/auth.py); Laravel `service.key` middleware |
| **Per-flow JWT** | `Authorization: Bearer <kestra-flow-jwt>` | kestra → fastapi | [src/fastapi/app/services/flow_jwt.py](../../../src/fastapi/app/services/flow_jwt.py) |
| **HMAC webhook** | `signature` field in body | external sender → kestra | Verified by Hatchet `external_notification` workflow |
| **Hatchet client token** | gRPC metadata | hatchet worker → hatchet-lite | `HATCHET_CLIENT_TOKEN` env |

## 3. Laravel API surface (`routes/api.php`)

Versioned under `/api/v1/*`. Selected high-traffic endpoints:

| Method | Path | Auth | Purpose | Emits |
|---|---|---|---|---|
| POST | `/api/login` | (login form) | Sanctum SPA login | session cookie |
| POST | `/api/logout` | Sanctum | Sanctum SPA logout | — |
| GET  | `/api/v1/workspaces` | Sanctum | List workspaces user can see | — |
| POST | `/api/v1/workspaces` | Sanctum + admin | Create workspace | `audit_ledger` |
| GET  | `/api/v1/projects` | Sanctum | List projects (workspace-scoped) | — |
| POST | `/api/v1/projects` | Sanctum | Create project | `audit_ledger`, `workspace-data-updated.{ws}` |
| GET  | `/api/v1/projects/{project}/uploads` | Sanctum | List uploads for a project | — |
| POST | `/api/v1/projects/{project}/uploads` | Sanctum | Multipart upload (drill data, PDF, GPKG, …) | starts ingest run; `ingestion-progress.{ws}` |
| GET  | `/api/v1/projects/{project}/ingest-runs` | Sanctum | Per-project run list | — |
| GET  | `/api/v1/ingest-progress/{run}` | Sanctum | Detailed run status | — (name `ingest_progress.show`) |
| GET  | `/api/v1/exports/{export}/download` | Sanctum | Download export tarball | `audit_ledger` (download event) |
| POST | `/api/v1/chat/queries` | Sanctum | Submit a chat query → returns `run_id` + streams via Reverb | `answer_runs` row; `query.streaming.{run_id}` |
| POST | `/api/v1/queries/{run}/feedback` | Sanctum | Thumb / comment on an assistant turn | `silver.message_feedback` |
| GET  | `/api/v1/queries/{run}/lineage` | Sanctum | Replay payload for SupportCockpit | — |
| GET  | `/api/v1/citations/{evidence}/source` | Sanctum | Pre-signed access to the source document/page for a citation marker | `audit_ledger` |
| POST | `/api/v1/projects/{project}/datasets/{kind}/{id}/categories` | Sanctum | Apply/remove data-hierarchy categories (Ch 13) | `audit_ledger`, `workspace-data-updated.{ws}` |
| GET  | `/tiles/silver/{source}/{ws}/{z}/{x}/{y}` | Sanctum | Workspace-scoped Martin proxy (sets GUC) | — |
| GET  | `/tiles/public-geoscience/{source}/{z}/{x}/{y}` | Sanctum | Public-geo Martin proxy | — |

Internal (Service-Key) routes:

| Method | Path | Caller | Purpose |
|---|---|---|---|
| POST | `/api/internal/v1/reports/{report}/progress` | FastAPI/Hatchet | Update ingest progress (internal) |
| POST | `/api/internal/v1/ingest-progress/broadcast` | FastAPI/Hatchet/Dagster | Emit `IngestProgress` Reverb event |
| POST | `/api/internal/v1/workspace-data-updated/broadcast` | FastAPI/Hatchet/Dagster | Emit `WorkspaceDataUpdated` |
| POST | `/api/internal/v1/admin-surface-updated/broadcast` | Hatchet workers | Admin UI invalidation |
| POST | `/api/internal/v1/workspace-activity/broadcast` | FastAPI | Activity feed events |
| POST | `/api/internal/v1/user-inbox-updated/broadcast` | Horizon (notifications) | Inbox refresh |
| POST | `/api/internal/v1/public-geoscience-tiles-invalidated/broadcast` | Dagster `bronze_public_geoscience` | Martin tile cache flush |
| POST | `/api/internal/v1/re-ocr` | Laravel (admin) | Trigger Hatchet `re_ocr_page` |

## 4. FastAPI surface (routers)

Router modules under [src/fastapi/app/routers/](../../../src/fastapi/app/routers/):

| Router | Prefix (typical) | Auth | Purpose |
|---|---|---|---|
| `queries.py` | `/v1/query`, `/v1/retrieve` | X-Service-Key | Chat handler + retrieval-only path |
| `answer_runs.py` | `/v1/answer-runs` | X-Service-Key | Read past `silver.answer_runs` rows |
| `evidence.py` | `/v1/evidence` | X-Service-Key | Evidence lookup / pre-signed source access |
| `citation_feedback.py` | `/v1/citation-feedback` | X-Service-Key | Stores user citation corrections |
| `projects.py` | `/v1/projects` | X-Service-Key | Project metadata mirror for FastAPI consumers |
| `pdf.py`, `ocr_render.py`, `re_ocr_trigger.py` | `/v1/pdf`, `/v1/ocr` | X-Service-Key | PDF rendering + per-page re-OCR triggers |
| `report_builder.py` | `/v1/reports` | X-Service-Key | NI 43-101 report assembly |
| `interpretation.py` | `/v1/interpretation` | X-Service-Key | Geologist annotation surface |
| `maps.py`, `visualizations.py` | `/v1/maps`, `/v1/viz` | X-Service-Key | Server-side viz computations |
| `exports.py` | `/v1/exports` | X-Service-Key | Export generation |
| `coverage.py`, `completeness.py`, `conflicts.py`, `outlier_assist.py`, `audit_findings.py`, `assessment_summary.py` | `/v1/{topic}` | X-Service-Key | Topic-specific analytical endpoints |
| `metrics_ingestion_events.py` | `/v1/metrics/ingestion-events` | X-Service-Key | Custom Prometheus surface |
| `ml_training.py` | `/v1/ml/train` | X-Service-Key | Reranker LoRA training trigger |
| `support_agents.py` | `/v1/support` | X-Service-Key | SupportCockpit agents |
| `smdi.py` | `/v1/smdi` | X-Service-Key | SMDI public-geo lookups |
| `target_recommendation_cockpit.py` | `/v1/targets` | X-Service-Key | Target generation + scoring |
| `what_changed.py` | `/v1/what-changed` | X-Service-Key | WhatChangedFeed source |
| `phase0_ops.py` | `/v1/ops/phase0` | X-Service-Key | Phase 0 agents trigger surface |
| `admin_tier1_misc.py`, `admin_tier234.py` | `/v1/admin/*` | X-Service-Key + admin role | Admin operations |
| `integrations_trigger.py` | `/internal/v1/integrations/{flow}/trigger` | per-flow JWT | Kestra → FastAPI inbound |
| `shadow_trigger.py` | `/internal/v1/shadow/ingest_pdf/trigger` | X-Service-Key | Kicks Hatchet `ingest_pdf` |
| `mv_refresh_trigger.py` | `/v1/mv/refresh` | X-Service-Key | Triggers Dagster MV refresh |
| `__main__` (`/health`, `/ready`, `/metrics`) | — | — | Liveness / readiness / Prometheus |

## 5. Endpoint contract template

Every endpoint MUST document (in code docstring + OpenAPI):

```yaml
method: POST
path: /api/v1/projects/{project}/uploads
owner: laravel-octane
auth: sanctum
request:
  multipart:
    file: binary
    kind: enum[pdf, gpkg, drillhole_zip, csv_collar, csv_assay, ...]
response_200:
  application/json:
    ingest_run_id: uuid
    upload_id: uuid
    sha256: string
error_4xx:
  422 ValidationError, 413 PayloadTooLarge, 415 UnsupportedMediaType
error_5xx:
  503 BronzeWriteFailed, 502 HatchetTriggerFailed
idempotency:
  by_sha256: bool   # repeat upload of same sha256 returns the original ingest_run_id
rate_limit:
  per_user: 60/min
  per_workspace: 600/min
tables_touched:
  - bronze.upload_files (planned) | bronze.ingest_runs
  - bronze.ingest_manifest
  - audit.audit_ledger
events_emitted:
  - ingestion-progress.{workspace_id}::IngestProgress (status=queued)
audit_event:
  action_type: upload.create
tests:
  - tests/Feature/Upload/UploadFlowTest.php
  - src/fastapi/tests/test_ingest_pdf_e2e.py
```

## 5b. Error catalog (typed guard codes)

Chat / RAG responses always return HTTP 200 (or 207 for partial answers);
quality and policy refusals ride as typed codes in the response body so
the React layer can surface a structured UI (clickable picker, conflict
diff, refusal banner) instead of a generic 5xx page.

The canonical inventory of internal codes is
[`src/fastapi/app/agent/guards.py`](../../../src/fastapi/app/agent/guards.py)
(`GuardErrorCode`). The user-facing message templates live in
[`lang/en/guard_errors.php`](../../../lang/en/guard_errors.php) and the
catalog page at
[`docs/architecture/user_facing_error_catalog.md`](../user_facing_error_catalog.md).

Codes summarised here for the API contract surface:

| Group | Codes | When fired |
|---|---|---|
| Retrieval failure | `NO_EVIDENCE_FOUND`, `ENTITY_NOT_FOUND`, `AMBIGUOUS_HOLE_ID`, `AMBIGUOUS_FORMATION_NAME`, `AMBIGUOUS_PROPERTY_NAME`, `OVER_FILTERED_QUERY`, `SPATIAL_QUERY_EMPTY`, `SPATIAL_CRS_MISMATCH`, `GRAPH_PATH_NOT_FOUND` | Retrieval returned no usable evidence or the query is ambiguous |
| Evidence quality | `NUMERIC_GROUNDING_FAILED`, `CITATION_INCOMPLETE`, `CONFLICTING_SOURCES`, `MISSING_DEPTH_INTERVAL`, `MISSING_ASSAY_UNITS`, `SOURCE_SCOPE_VIOLATION` | §04i guards (Layers 2-6) rejected the assembled answer |
| Query type | `UNSUPPORTED_QUERY_TYPE` | Query is outside the answerable surface |
| Egress / policy | `EGRESS_BLOCKED` | `LLM_BACKEND=anthropic` but the workspace profile has not opted in (`profile.allow_external_llm != true`). Refusal is hard; no Anthropic call is made. Implementation: [`app.agent.egress_gate`](../../../src/fastapi/app/agent/egress_gate.py) (Z.1 / Appendix C §5) |

Death-loop refusal (out of band: not a `GuardErrorCode` but rendered the
same way) uses the `DEATH_LOOP` translation key.

## 6. Idempotency posture

- **POST uploads**: idempotent on `sha256` — duplicate upload returns the
  original `ingest_run_id`.
- **POST chat queries**: idempotent on `Idempotency-Key` header (stored in
  `workspace.idempotency_keys`).
- **Internal Service-Key broadcasts**: idempotent on `(channel, event_id)`
  — duplicates suppressed by Reverb.

## 7. Rate limits (configured in `app/Providers/RouteServiceProvider.php`)

| Limiter | Default | Where |
|---|---|---|
| `api` | 60/min/user | All `/api/v1/*` except chat |
| `chat` | 30/min/user, 300/min/workspace | `/api/v1/chat/queries` |
| `uploads` | 10/min/user, 100/min/workspace | `/api/v1/projects/*/uploads` |
| `tiles` | 600/min/user | `/tiles/*` |
| `internal` | unlimited | Service-Key routes (network-isolated) |

## 8. Generator wiring (planned)

A nightly Dagster asset `api_openapi_dump`:
1. Hits `http://fastapi:8000/openapi.json`.
2. Calls `php artisan route:list --json` inside `laravel-octane`.
3. Merges into a single `docs/architecture/openapi/v1.json`.
4. Writes a Markdown index of the union under
   `docs/architecture/openapi/INDEX.md`.

Acceptance: the union file is checked in; any drift between merged file
and source fails CI (`api-drift-check.yml`).
