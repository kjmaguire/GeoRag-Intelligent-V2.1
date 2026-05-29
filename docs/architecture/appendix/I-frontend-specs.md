# Appendix I — Frontend Workflow Specs

Status: **Draft.** Per-page acceptance spec for the 9 highest-value
pages. Lower-value pages stay in the [Ch 10](../manual/10-frontend.md)
inventory.

> **Per-page template.** Route • Controller • Inertia props • API
> calls • permissions • empty / loading / error states • Reverb
> channels • cache invalidation • user actions • tables touched •
> audit events • acceptance tests.

## 1. Upload + Data Import Wizard

- **Route**: `GET /foundry/projects/{project}/upload` →
  `DataImportWizard.tsx`.
- **Controller**: `App\Http\Controllers\Foundry\DataImportWizardController`.
- **Inertia props**:
  - `project_id`, `workspace_id`
  - `supported_formats[]` — from [Appendix E](E-ingestion-format-matrix.md)
  - `recent_runs[]` (last 10 `bronze.ingest_runs`)
- **API calls**:
  - `POST /api/v1/projects/{project}/uploads` (multipart per file)
  - `GET /api/v1/projects/{project}/ingest-runs?since=…`
- **Permissions**: workspace `data.write` role.
- **Empty state**: "No files uploaded — start with a CSV or PDF."
  Action button → file picker.
- **Loading state**: per-file progress bar driven by `XHR upload progress`
  (browser) then Reverb `IngestProgress`.
- **Error state**: 413 → "File too big (cap 2 GB)"; 415 → "Unsupported
  format"; 422 → field-level errors from the kind-specific Form Request.
- **Reverb channels**:
  - `ingestion-progress.{workspace_id}` → live progress per file.
- **Cache invalidation**: on each `IngestProgress(status=completed)` →
  invalidate `["projects", project_id, "ingest_runs"]` query.
- **User actions**: drop / pick file, classify kind (auto-detected when
  possible), submit, cancel a queued run.
- **Tables touched**: `bronze.ingest_runs`, `bronze.ingest_manifest`,
  `bronze.upload_files` (planned), SeaweedFS `bronze` bucket,
  `audit.audit_ledger`.
- **Audit events**: `action_type='upload.create'`, target =
  `bronze.ingest_runs.run_id`.
- **Acceptance tests**:
  1. Drop a 1.4 GB PDF → upload progress visible → `IngestProgress`
     events arrive → run completes; the new doc appears in Sources.
  2. Drop an unrecognised binary → 415 with a kind-suggestion list.
  3. Upload the same sha256 twice → second returns the original
     `ingest_run_id`, no second bronze write.
  4. RLS: a user in workspace B cannot see workspace A's uploads.

## 2. Drill Review (CC-01 Item 1)

- **Route**: `GET /foundry/projects/{project}/drill-review` →
  `DrillReview.tsx`.
- **Inertia props**: `project_id`, `workspace_id`,
  `queue_items[]` (paginated from `silver.review_queue`).
- **API calls**:
  - `GET /api/v1/projects/{project}/review-queue?lifecycle=pending`
  - `POST /api/v1/review-queue/{queue_id}/decision` body
    `{decision_kind, corrections?}`.
- **Permissions**: workspace `review.decide` role.
- **Empty state**: "Nothing in the review queue."
- **Loading**: skeleton rows.
- **Error**: 409 on stale `silver.review_queue.lifecycle` (someone else
  approved already).
- **Reverb**: `ingestion-progress.{workspace_id}` and
  `workspace-data-updated.{workspace_id}` on commit.
- **Cache invalidation**: invalidate the queue list and the dependent
  silver-table queries (`collars`, `lithology`, `assays_v2`) on commit.
- **User actions**: approve / approve-with-corrections / reject / defer;
  bulk approve.
- **Tables touched**: `silver.review_queue`, the silver target row
  (committed on approve), `audit.audit_ledger`.
- **Audit events**: `action_type='review.decide'`.
- **Acceptance tests**:
  1. Approve a queue item → target silver row appears → reverse-cascade
     count updates in the Overview card.
  2. Approve with corrections → corrections applied to the silver row,
     `bronze.provenance.source_col_map` records the override.
  3. Reject → no silver row written, `lifecycle='archived'`.
  4. Cross-workspace fence test.

## 3. Ingestion Runs

- **Route**: `GET /foundry/projects/{project}/ingestion-runs` →
  `IngestionRuns.tsx`.
- **Inertia props**: paginated `bronze.ingest_runs` joined to
  `silver.ingest_progress` per-step rows (Phase B).
- **API**: `GET /api/v1/projects/{project}/ingest-runs/{run_id}/steps`
  for the expand-row view.
- **Reverb**: `ingestion-progress.{workspace_id}`.
- **States**: loading / empty ("No runs yet") / error.
- **User actions**: re-run failed step, abort running run, view raw
  bronze object, jump to Drill Review for pending review rows.
- **Tables touched**: `bronze.ingest_runs`, `silver.ingest_progress`,
  `silver.parser_run_artifacts`, `silver.ocr_page_quality`.
- **Audit**: re-run emits `action_type='ingest.rerun'`.
- **Acceptance tests**: per-step status reflects Hatchet step results;
  cancellation actually cancels the worker.

## 4. Ingest Quality

- **Route**: `GET /foundry/projects/{project}/ingest-quality` →
  `IngestQuality.tsx`.
- **Inertia props**: aggregated KPIs over the last N runs:
  - Avg parser duration per format
  - OCR mean confidence histogram
  - Table extraction precision/recall vs golden
  - Low-confidence page count
- **API**: `GET /v1/metrics/ingestion-events?range=...` (FastAPI).
- **Permissions**: workspace `data.read`.
- **States**: loading / empty / error.
- **No Reverb subscription** — refreshed on `WorkspaceDataUpdated`.
- **Tables touched**: `silver.document_ingestion_quality`,
  `silver.ocr_page_quality`, `silver.table_extraction_quality`,
  `silver.low_confidence_page_reviews`.
- **Acceptance tests**: KPI numbers reconcile with raw rows; per-format
  filter works.

## 5. Chat + Citation Drawer

- **Route**: `GET /foundry/projects/{project}/chat` →
  `Foundry/Chat.tsx`.
- **Inertia props**: thread state, context envelope, intent suggestions,
  available retrieval profiles, model tier choices, `data_categories`
  facets (Ch 13).
- **API**:
  - `POST /api/v1/chat/queries` → returns `run_id`.
  - `GET /api/v1/citations/{evidence_id}/source` for the drawer.
- **Permissions**: workspace `chat.use`. Tier 3 features gated by
  `usage.workspace_cost_quotas`.
- **Empty state**: greeting card + sample queries.
- **Loading**: typing-indicator stream from `QueryToken`.
- **Error**: `QueryRefusal` renders a "No defensible answer" card with
  reason; `QueryPersistFailure` (planned) renders a "Not recorded — try
  again" banner.
- **Reverb**: `query.streaming.{run_id}` (all five event types).
- **Cache invalidation**: none on chat itself; `feedback` mutation
  invalidates `["message_feedback", run_id]`.
- **User actions**: send, stop, retry, edit context envelope, change
  intent, pin a citation, send feedback.
- **Tables touched**: `silver.answer_runs`, `silver.answer_citation_items`,
  `silver.answer_citation_spans`, `silver.message_feedback`,
  `audit.audit_ledger`.
- **Audit**: `action_type='answer_runs.create'`.
- **Citation drawer**:
  - Triggered by clicking an `[ev:xxxxxxxx]` pill.
  - Renders the evidence row + paged PDF view (PNG from `bronze-raster`)
    or table view.
  - Drawer is a separate Reverb-aware component since the underlying
    document might still be ingesting.
- **Acceptance tests**:
  1. End-to-end answer with ≥ 1 citation pill → drawer opens the correct
     page.
  2. Refusal renders correctly.
  3. Streaming continues across a tab refresh (resumes by `run_id`).
  4. Feedback persists.

## 6. Lakehouse

- **Route**: `GET /foundry/projects/{project}/lakehouse` →
  `Lakehouse.tsx`.
- **Inertia props**: project bounding box, available silver layers,
  available public_geo overlays, per-table scope pills.
- **API**:
  - Direct fetch of tile URLs from Martin (proxied) — no JSON API.
- **Permissions**: per-layer; some Tier-3 layers gated.
- **Empty**: zoom-to-project hint.
- **Loading**: tile placeholder.
- **Reverb**: `workspace-data-updated.{workspace_id}` re-fetches active
  layers.
- **User actions**: toggle layers, draw a polygon to scope, save as a
  named view (writes `silver.saved_map_views`).
- **Tables touched** (via Martin): every `silver.pg_*_by_project` plus
  `public_geo.v_pg_*_mvt`.
- **Acceptance tests**: each layer renders within 1.5 s P95; layer
  toggling does not refetch unchanged sources.

## 7. Workspace 3D

- **Route**: `GET /foundry/projects/{project}/workspace-3d` →
  `Workspace.tsx`.
- **Inertia props**: 9 sub-views per
  [notes/INDEX.md#project_workspace_3d_expansion_2026_05_25](../notes/INDEX.md#project_workspace_3d_expansion_2026_05_25).
- **API**: `GET /v1/viz/compute` (FastAPI) for derived 3D meshes.
- **Permissions**: workspace `geology.view`.
- **States**: loading / empty / error.
- **Reverb**: `workspace-data-updated.{workspace_id}`.
- **User actions**: pan/rotate/zoom; select a hole → reveals strip log;
  toggle sub-views; save 3D viewpoint.
- **Tables touched**: `silver.collars`, `silver.drill_traces`,
  `gold.drillhole_intervals_visual`, `gold.structure_measurements_visual`,
  `silver.geophysics_surveys`, `silver.raster_layers`.
- **Acceptance tests**:
  1. Each of the 9 sub-views materially renders.
  2. Cameco U₃O₈ rolls up from `silver.samples` (NOT
     `gold.assay_composites`).
  3. Cross-section line set in B6 surfaces in the project map (Martin
     `pg_cross_section_lines_by_project`).

## 8. Source Graph

- **Route**: `GET /foundry/projects/{project}/source-graph` →
  `SourceGraph.tsx`.
- **Inertia props**: initial node id, default fan-out depth (=2).
- **API**: `GET /v1/graph/neighbours?node=...&depth=...` (FastAPI →
  Neo4j) returning React-Flow-compatible node/edge JSON.
- **Permissions**: workspace `graph.view`.
- **States**: loading / empty (orphan node) / error.
- **Reverb**: none (graph is on-demand).
- **User actions**: expand a node, contract a sub-tree, jump-to-source
  (a node opens the matching silver-row page).
- **Tables touched**: Neo4j only (read-only at chat time).
- **Acceptance tests**:
  1. Open `:DrillHole` → see `:HAS_HOLE`, `:INTERSECTS`,
     `:CITES_DRILLHOLE` neighbours.
  2. Cross-workspace fence test (graph traversal honours `$ws` filter).
  3. No orphan edges (every edge has both endpoints in-window).

## 9. Targets

- **Route**: `GET /foundry/projects/{project}/targets` → `Targets.tsx`.
- **Inertia props**: target list (paginated), per-target factor
  contributions, uncertainty breakdown.
- **API**:
  - `GET /v1/targets/{project_id}/list`
  - `POST /v1/targets/{project_id}/score` → triggers Hatchet
    `score_targets` workflow.
- **Permissions**: workspace `targets.use`; ranking generation gated by
  `targeting.target_backtests` quota.
- **States**: loading / empty ("No targets yet — score the project").
- **Reverb**: `workspace-data-updated.{workspace_id}` re-fetches list.
- **User actions**: trigger scoring, drill into rationale, approve /
  archive a target, pin a hypothesis.
- **Tables touched**: `targeting.*`, `silver.target_rationales`,
  `silver.hypotheses`.
- **Audit**: `action_type='targets.score'`,
  `action_type='target.approve'`.
- **Acceptance tests**:
  1. Scoring run completes with non-zero targets given a project with
     enough silver data.
  2. Rationale links resolve to actual `silver.evidence_items` rows.
  3. Approving a target writes the decision into `silver.decision_records`.

---

## 10. Cross-cutting frontend rules

These apply to **every** page above:

1. **Workspace context provider** ([Ch 10 §7](../manual/10-frontend.md))
   injects `X-Workspace-Id` on every API call.
2. **Octane reload after `vite build`** — `php artisan octane:reload`
   ([notes/INDEX.md#feedback_octane_vite_reload](../notes/INDEX.md#feedback_octane_vite_reload)).
3. **Reverb dual-purpose env** — `REVERB_HOST/PORT` differ server vs
   browser ([notes/INDEX.md#project_reverb_dual_purpose_env_2026_05_21](../notes/INDEX.md#project_reverb_dual_purpose_env_2026_05_21)).
4. **React Query keys** must include `workspace_id` so a workspace
   switch invalidates cached data.
5. **Inertia `Inertia::optional()`** for any prop whose computation can
   exceed 50 ms — never `Inertia::lazy()` (removed in v3).
6. **Skeletons** for any deferred prop.
7. **Error boundaries** at the page level; bubble to a global error
   modal for 500s.
8. **A11y**: every interactive element has keyboard focus + ARIA
   labels; map controls expose a tabular fallback view.

## 11. Test conventions

- Playwright e2e: `tests/Browser/Foundry/<Page>Test.php` (project layer)
  + `playwright/<page>.spec.ts` (per-page interactions).
- Inertia component test: `resources/js/Pages/__tests__/<Page>.test.tsx`.
- API contract test (every page's calls): `tests/Feature/Api/<Page>Test.php`.
- Reverb channel mock: shared helper in
  `tests/Support/ReverbMock.php`.
