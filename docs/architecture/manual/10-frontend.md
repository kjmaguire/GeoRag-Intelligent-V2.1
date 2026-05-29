# Chapter 10 — Frontend

React 19 + Inertia.js v3 + Tailwind v4 + shadcn/ui + MapLibre GL + React
Flow + Plotly. Streams via Laravel Reverb (Pusher protocol). Vite for
bundling.

## 1. Repo layout

| Path | What lives there |
|---|---|
| [resources/js/Pages/](../../../resources/js/Pages/) | Top-level Inertia pages, one component per route |
| [resources/js/Components/](../../../resources/js/Components/) | Shared UI (cards, panels, charts) |
| [resources/js/Components/Map/](../../../resources/js/Components/Map/) | MapLibre wrappers + layer helpers |
| [resources/js/Hooks/](../../../resources/js/Hooks/) | Custom React hooks (e.g., useWorkspaceData, useReverbChannel) |
| [resources/js/Lib/](../../../resources/js/Lib/) | Utilities (date formatters, citation parser, etc.) |
| [resources/js/Pages/Foundry/](../../../resources/js/Pages/Foundry/) | The "Foundry" product surface — workspace 3D, targets, hypothesis |
| [resources/js/Pages/Dashboards/](../../../resources/js/Pages/Dashboards/) | Pre-built dashboards (EvidenceQuality, LlmCost, PublicGeoOverlay, Reporting, TargetRecommendation, VisualReadiness) |
| [resources/js/Pages/PublicGeoscience/](../../../resources/js/Pages/PublicGeoscience/) | Public geoscience browsing (Index, Workspace, Overlay) |
| [resources/js/Pages/Onboarding/](../../../resources/js/Pages/Onboarding/) | First-run onboarding |
| [resources/js/Pages/Admin/](../../../resources/js/Pages/Admin/) | Admin integrations (Hatchet, Kestra, Pulse, Horizon) |

## 2. Pages by feature

| Page | File | Notes |
|---|---|---|
| Login | [Login.tsx](../../../resources/js/Pages/Login.tsx) | Sanctum SPA login |
| Forgot Password | [ForgotPassword.tsx](../../../resources/js/Pages/ForgotPassword.tsx) | |
| Projects (list) | [Foundry/Projects.tsx](../../../resources/js/Pages/Foundry/Projects.tsx) | Per-workspace project list |
| New Project | [Foundry/NewProject.tsx](../../../resources/js/Pages/Foundry/NewProject.tsx) | Project creation form |
| Project Overview | [Foundry/Overview.tsx](../../../resources/js/Pages/Foundry/Overview.tsx) | Project home dashboard, includes "Ingest" card derived from silver.reports + bronze MinIO listing |
| Lakehouse | [Foundry/Lakehouse.tsx](../../../resources/js/Pages/Foundry/Lakehouse.tsx) | Map + table cross-filter view (per-table scope pills from RLS work) |
| Drill Review | [Foundry/DrillReview.tsx](../../../resources/js/Pages/Foundry/DrillReview.tsx) | Drill-data upload review queue (`silver.review_queue`) |
| Drillhole Detail | [Foundry/DrillholeDetail.tsx](../../../resources/js/Pages/Foundry/DrillholeDetail.tsx) | Per-hole strip log + inset map |
| Hole Compare | [Foundry/HoleCompare.tsx](../../../resources/js/Pages/Foundry/HoleCompare.tsx) | Side-by-side hole comparison |
| Ingest Quality | [Foundry/IngestQuality.tsx](../../../resources/js/Pages/Foundry/IngestQuality.tsx) | Aggregated per-document parser/OCR quality |
| Ingestion Runs | [Foundry/IngestionRuns.tsx](../../../resources/js/Pages/Foundry/IngestionRuns.tsx) | Per-project run list (Phase A landed; Phase B uses silver.ingest_progress) |
| Chat | [Foundry/Chat.tsx](../../../resources/js/Pages/Foundry/Chat.tsx) (and [PublicGeoscience/Chat.tsx](../../../resources/js/Pages/PublicGeoscience/Chat.tsx)) | The main RAG chat surface. Renders OIUR cards + inline ADR-0007 chat cards |
| Investigations | [Foundry/Investigations.tsx](../../../resources/js/Pages/Foundry/Investigations.tsx) | Saved investigations (multi-turn lineage) |
| Hypothesis | [Foundry/Hypothesis.tsx](../../../resources/js/Pages/Foundry/Hypothesis.tsx) | Hypothesis tracker (`silver.hypotheses`) |
| Decisions | [Foundry/Decisions.tsx](../../../resources/js/Pages/Foundry/Decisions.tsx) | Decision intelligence schema |
| Rationale | [Foundry/Rationale.tsx](../../../resources/js/Pages/Foundry/Rationale.tsx) | Target rationales |
| Reasoning | [Foundry/Reasoning.tsx](../../../resources/js/Pages/Foundry/Reasoning.tsx) | Step-by-step reasoning trace |
| Targets | [Foundry/Targets.tsx](../../../resources/js/Pages/Foundry/Targets.tsx) | Generated drill targets + ranking |
| Source Graph | [Foundry/SourceGraph.tsx](../../../resources/js/Pages/Foundry/SourceGraph.tsx) | Neo4j-backed entity-relationship explorer (React Flow) |
| Sources / Corpus | [Foundry/Sources.tsx](../../../resources/js/Pages/Foundry/Sources.tsx), [Foundry/Corpus.tsx](../../../resources/js/Pages/Foundry/Corpus.tsx) | Corpus inventory |
| Audit Log | [Foundry/AuditLog.tsx](../../../resources/js/Pages/Foundry/AuditLog.tsx) | `audit.audit_ledger` filtered by workspace |
| Workspace | [Foundry/Workspace.tsx](../../../resources/js/Pages/Foundry/Workspace.tsx) | 3D workspace view; 9 sub-views as of 2026-05-25 |
| Portfolio | [Foundry/Portfolio.tsx](../../../resources/js/Pages/Foundry/Portfolio.tsx) | Cross-project portfolio rollup |
| Project Analytics | [Foundry/ProjectAnalytics.tsx](../../../resources/js/Pages/Foundry/ProjectAnalytics.tsx) | Plotly charts over silver/gold |
| Retrieval Inspector | [Foundry/RetrievalInspector.tsx](../../../resources/js/Pages/Foundry/RetrievalInspector.tsx) | Debug view: per-query retrieval/fusion/rerank traces |
| Reporting / Report Builder / Report View | [Reporting.tsx](../../../resources/js/Pages/Dashboards/Reporting.tsx), [Foundry/Report.tsx](../../../resources/js/Pages/Foundry/Report.tsx), [Foundry/ReportView.tsx](../../../resources/js/Pages/Foundry/ReportView.tsx) | NI 43-101-style report assembly + view |
| Saved Map Views | [Foundry/SavedMapViews.tsx](../../../resources/js/Pages/Foundry/SavedMapViews.tsx) | Persisted MapLibre view state |
| Support Cockpit | [Foundry/SupportCockpit.tsx](../../../resources/js/Pages/Foundry/SupportCockpit.tsx) | Trace + audit + Langfuse deep-link operator UI |
| Settings | [Foundry/Settings.tsx](../../../resources/js/Pages/Foundry/Settings.tsx) | Workspace settings, integration keys |
| Tier 3 Unlock | [Foundry/Tier3Unlock.tsx](../../../resources/js/Pages/Foundry/Tier3Unlock.tsx) | Cost-gated feature unlock flow |
| What Changed Feed | [Foundry/WhatChangedFeed.tsx](../../../resources/js/Pages/Foundry/WhatChangedFeed.tsx) | Workspace activity feed |
| Inbox | [Foundry/Inbox.tsx](../../../resources/js/Pages/Foundry/Inbox.tsx) | User notifications |
| Assessment Summary | [Foundry/AssessmentSummary.tsx](../../../resources/js/Pages/Foundry/AssessmentSummary.tsx) | NI 43-101 assessment summarisation |
| Charts Gallery | [ChartsGallery.tsx](../../../resources/js/Pages/ChartsGallery.tsx) | Plotly chart catalog |
| Interpretation Workspace | [InterpretationWorkspace.tsx](../../../resources/js/Pages/InterpretationWorkspace.tsx) | Geologist annotation surface (`interpretation.*` schema) |
| Search Query | [SearchQuery.tsx](../../../resources/js/Pages/SearchQuery.tsx) | Faceted search |
| Explorer | [Explorer.tsx](../../../resources/js/Pages/Explorer.tsx) + [Foundry/Explorer.tsx](../../../resources/js/Pages/Foundry/Explorer.tsx) | Generic data explorer |
| Data Import Wizard | [Foundry/DataImportWizard.tsx](../../../resources/js/Pages/Foundry/DataImportWizard.tsx) | Step-by-step ingest UI |

### Dashboards

| Dashboard | File |
|---|---|
| Evidence Quality | [Dashboards/EvidenceQuality.tsx](../../../resources/js/Pages/Dashboards/EvidenceQuality.tsx) |
| LLM Cost | [Dashboards/LlmCost.tsx](../../../resources/js/Pages/Dashboards/LlmCost.tsx) |
| Public Geo Overlay | [Dashboards/PublicGeoOverlay.tsx](../../../resources/js/Pages/Dashboards/PublicGeoOverlay.tsx) |
| Reporting | [Dashboards/Reporting.tsx](../../../resources/js/Pages/Dashboards/Reporting.tsx) |
| Target Recommendation | [Dashboards/TargetRecommendation.tsx](../../../resources/js/Pages/Dashboards/TargetRecommendation.tsx) |
| Visual Readiness | [Dashboards/VisualReadiness.tsx](../../../resources/js/Pages/Dashboards/VisualReadiness.tsx) |

## 3. Reverb broadcast channels

Authentication for private channels goes through `routes/channels.php`
(Sanctum-authed). Echo client: [resources/js/Lib/echo.ts](../../../resources/js/Lib/echo.ts).

| Channel | Event | Producer | Consumer (page) |
|---|---|---|---|
| `query.streaming.{run_id}` | `QueryToken` | FastAPI → Laravel `BroadcastQueryToken` event | Chat (assistant message stream) |
| `query.streaming.{run_id}` | `QueryCitation` | FastAPI → Laravel | Chat (inline citation pill insertion) |
| `query.streaming.{run_id}` | `QueryComplete` | FastAPI → Laravel | Chat (mark turn done) |
| `ingestion-progress.{workspace_id}` | `IngestProgress` | Hatchet workers / Dagster commit → Laravel | IngestionRuns, DrillReview |
| `workspace-data-updated.{workspace_id}` | `WorkspaceDataUpdated` | Hatchet `score_targets`, Dagster `commit_ingestion_run`, Laravel mutation listeners | Overview, Lakehouse, Targets — invalidate React Query caches |
| `audit-ledger.{workspace_id}` | `AuditEvent` | Trigger-driven via Laravel listener | AuditLog (incremental tail) |
| `notifications.{user_id}` | `Notification` | Horizon `notifications` queue | Inbox |
| `support-replay.{run_id}` | `ReplayProgress` | Hatchet `support_replay` workflow | SupportCockpit |

### Reverb dual-purpose env trap

[project_reverb_dual_purpose_env_2026_05_21](../notes/INDEX.md#project_reverb_dual_purpose_env_2026_05_21):
`REVERB_HOST/PORT` serve two purposes — server-side publisher and browser
client. Vite doesn't expand `${VAR}`, so a previous `.env` literal
`${REVERB_HOST_PORT}` ended up in the bundle → 60 s channel-drop timeouts.
Fix: server uses `laravel-reverb:8080`, browser uses literal `8085`.

## 4. Inertia surface

- Server-side rendering: Inertia v3 supports `@inertiajs/vite` SSR mode
  in dev (no separate Node SSR server). See CLAUDE.md inertia-laravel/core
  rules.
- Page components export from `resources/js/Pages/<Name>.tsx` are resolved
  via `Inertia::render('Name', $props)` in Laravel controllers.
- Deferred props: `Inertia::optional()` (replaces v2 `Inertia::lazy()`).
- Events: `httpException`, `networkError` (renamed from `invalid`/`exception`
  in v3).

## 5. Vite build

- Vite config: [vite.config.ts](../../../vite.config.ts).
- Output: `public/build/`.
- Important: after every `vite build`, run `php artisan octane:reload`
  ([feedback_octane_vite_reload](../notes/INDEX.md#feedback_octane_vite_reload)) —
  Swoole workers cache the Vite manifest, so a stale bundle hash 404s
  otherwise.

## 6. Sanctum SPA auth

Stateful domains
([docker-compose.yml:530](../../../docker-compose.yml)):
`localhost,localhost:8888,127.0.0.1,127.0.0.1:8888,host.docker.internal,host.docker.internal:8888`.
Without these, `EnsureFrontendRequestsAreStateful` treats inbound traffic as
token-only and skips `StartSession`, so spaLogin 500s with "Session store
not set on request".

## 7. Workspace context provider

A React context provider mounted near the Inertia root injects the current
`workspace_id` into every API client request header. Without the
`X-Workspace-Id` header, controllers default to the user's primary
workspace; with it, the controller calls
`SET LOCAL app.workspace_id = ?` on the PG connection before the query.

## 8. Chat surface specifics

- OIUR sections render as four distinct cards (Observation, Interpretation,
  Uncertainty, Recommendation).
- Inline ADR-0007 chat cards:
  - `evidence_list` — referenced evidence rows with links
  - `metric_box` — single-metric callout with trend
  - `coverage_gap_chart` — what's missing
  - `project_summary_card` — derived from `query_project_summary` intent
  - `spatial_quick_map` — inline mini MapLibre with relevant feature
- Citation pills: `[ev:xxxxxxxx]` markers render as clickable pills that
  open the evidence drawer
  ([Components/Citation/](../../../resources/js/Components/Citation/)).
- Streaming: tokens arrive via the `query.streaming.{run_id}` Reverb channel;
  the React component appends as they arrive, replacing citation markers
  with `<CitationPill/>` components in real time.

## 9. The "Plotly" surface

Charts:
- [resources/js/Components/Charts/](../../../resources/js/Components/Charts/)
- Plotly is used for cross-section panels, downhole strip logs, geochem
  scatter, target-score plots.
- Export contract: `docs/chart_export_contract_spec.md`.

## 10. React Flow

The Source Graph page uses `reactflow` to render Neo4j subgraphs returned
by the FastAPI `/v1/graph/neighbours` endpoint.

## 11. Browser logs / debugging

`browser-logs` MCP via Laravel Boost — also surfaced in dev.
