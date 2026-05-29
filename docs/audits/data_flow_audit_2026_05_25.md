# Page / Component Data-Flow Audit — 2026-05-25

Audit of every Inertia page → controller → data source → mutating async job → current update mechanism, with gaps grouped by user-visible urgency.

Sources read:
- `routes/web.php`, `routes/api.php`
- All `Inertia::render(...)` call sites under `app/Http/Controllers/`
- Real-time / poll / `router.reload` patterns across `resources/js/Pages/`
- `src/fastapi/app/routers/` (FastAPI mutating endpoints)
- `src/fastapi/app/hatchet_workflows/` (Hatchet workflow inventory)
- `kestra/flows/georag/` (Kestra flows)
- `docker/martin/martin.yaml` (Martin tile sources)

Real-time legend:
- **Echo** = Laravel Echo channel subscription
- **Poll** = JS `setInterval` / `fetch` poll
- **router.reload** = Inertia partial reload (manual, triggered by an action — not auto-refresh)
- **Static** = props set once at page load, no client refresh path

---

## Page-by-Page Audit Table

| Page / Component | Route | Data Source (Props or API) | Mutated By Async Job? | Which Jobs | Current Update Mechanism | Gap? |
|---|---|---|---|---|---|---|
| **Login** | `GET /login` | inline `Inertia::render('Login')`, no props | No | — | Static | No |
| **ForgotPassword** | `GET /forgot-password` | inline render, no props | No | — | `fetch /api/v1/auth/forgot-password` | No |
| **Foundry/Login** | `GET /foundry/login` | inline render, no props | No | — | `fetch /api/v1/auth/spa-login` | No |
| **Foundry/Portfolio** (`/dashboard`) | `PortfolioController@show` | `org_name`, `projects[]`, `kpis[]`, `recent_activity[]` (silver.projects, silver.collars count, query history) | Yes | `ingest_pdf`, `embed_pending_passages`, `sync_silver_to_kg`, `mv_refresh_silver`, `score_targets`, `what_changed_*`, drill-upload Dagster jobs | Static (no Echo, no poll) | **Yes — P2** (KPIs/activity stale until next nav) |
| **Foundry/Projects** (`/projects`) | `ProjectsIndexController@show` | `projects[]` (project_id, slug, region, commodity, status, crs_epsg, data_version, workspace_id, created_at, updated_at), `empty` | Yes | drill-upload, `ingest_pdf` (changes project counts), `public_geoscience_pull` | Static | **Yes — P2** (new projects/uploads not visible without nav) |
| **Foundry/NewProject** (`/foundry/projects/new`) | inline render | none (form) | No | — | `fetch POST /api/v1/projects`, then `fetch POST /api/v1/projects/{id}/upload` | No |
| **Foundry/Overview** (`/projects/{slug}`) | `OverviewController@show` | `project{}`, `kpis[]`, `recent_runs[]`, `cta`, `ingest_card` derived from collar/sample/log curve/hypothesis/query counts | Yes | `ingest_pdf`, drill-upload Dagster, `embed_pending_passages`, `score_targets` | `fetch /projects/{slug}/ingestion-runs.json` + `router.reload` on ingest card (no Echo here) | **Yes — P1/P2** (KPIs only refresh on reload; only ingest card polls) |
| **Foundry/IngestionRuns** (`/projects/{slug}/ingestion-runs`) | `IngestionRunsController@show` | `project{}`, `runs[]` (silver.reports + bronze MinIO listing), `fetched_at` | Yes | `ingest_pdf`, drill-upload Dagster, `tiff_normalize`, `tiff_ocr_cluster`, `embed_pending_passages` | **Echo private channel `project.{projectId}.ingestion`** + 5s `fetch .json` poll fallback | No |
| **Foundry/IngestQuality** (`/projects/{slug}/imports/quality`) | `IngestQualityController@show` | quality metrics per source_file (silver.reports parse_quality_pct, row counts, error categories) | Yes | `ingest_pdf`, drill-upload Dagster | Static | **Yes — P1** (user watches quality during ingest) |
| **Foundry/Lakehouse** (`/projects/{slug}/lakehouse`) | `LakehouseController@show` | `project{}`, `bronze{}` (source_files/ingest_manifest/provenance counts), `silver{}` (collars/lithology/structures/geophysics/spatial_features/raster_layers/reports), `gold{}` | Yes | `ingest_pdf`, drill-upload, `mv_refresh_silver`, `sync_silver_to_kg`, `tiff_normalize` | `router.reload` only (manual) | **Yes — P2** (counts stale during ingest run) |
| **Foundry/DrillholeDetail** (`/projects/{slug}/holes/{collarId}/detail`) | `DrillholeDetailController@show` | `project{}`, `collar`, `intervals`, `assays`, `structures`, `cross_sections`, `qa`, `lithology_quality` | Yes | drill-upload Dagster, `sync_silver_to_kg`, `mv_refresh_silver` | `router.reload` only | **Yes — P3** (rare to mutate during view, but no auto-refresh on backfill) |
| **Foundry/Explorer** (`/projects/{slug}/explorer`) | `ExplorerController@show` | `project{}`, `collars[]`, `detail` (lithology + samples + assays per hole), `filters`, `empty` | Yes | drill-upload, `ingest_pdf`, `sync_silver_to_kg` | `router.reload({only:['collars','detail','filters','empty']})` (manual filter changes) | **Yes — P2** (newly ingested holes don't appear until reload) |
| **Foundry/HoleCompare** (`/projects/{slug}/compare`) | `HoleCompareController@show` | comparison rows per selected hole | Yes | drill-upload, `ingest_pdf` | Static | **Yes — P3** |
| **Foundry/Chat** (`/projects/{slug}/chat`) | `ChatController@show` | `project{}`, `threads[]`, `active_thread_id`, `active_thread`, `messages[]` | Yes | `queries` Horizon job + FastAPI RAG dispatch | **Echo private channel** for query stream (`Echo.private(channel)`) + `fetch /api/v1/queries` POST then `/api/v1/queries/{id}/start` | No |
| **Chat** (legacy `/chat`) | redirects to `/dashboard` | — | — | — | (deprecated; legacy `resources/js/Pages/Chat.tsx` still in repo for tests) | No |
| **SearchQuery** (`/search`) | inline render | none (single-shot search) | Yes | `queries` Horizon job, FastAPI RAG | **Echo public channel** for query stream + `fetch /api/v1/queries` two-phase handshake | No |
| **Foundry/Reasoning** (`/projects/{slug}/reasoning`) | `ReasoningController@show` | `project{}`, `hypotheses[]`, `collars_in_project`, `scope_note` | Yes | `continuous_learning_loop`, `field_outcome_learning`, `sync_silver_to_kg` | Static | **Yes — P3** |
| **Foundry/Hypothesis** (`/projects/{slug}/hypothesis`) | `ReasoningController@show` (same render) | as above | Yes | as above | Static | **Yes — P3** |
| **Foundry/SourceGraph** (`/projects/{slug}/graph`) | `SourceGraphController@show` | `project{}`, graph nodes/edges (parsers → reports → hypotheses), `reports_featured` | Yes | `ingest_pdf`, `sync_silver_to_kg` | Static | **Yes — P3** |
| **Foundry/Sources** (`/projects/{slug}/sources`) | `SourcesController@show` | `project{}`, `parser_activity` (count/bytes/completed_at per parser), filing-date roll-ups | Yes | `ingest_pdf`, drill-upload, `public_geoscience_pull` | Static | **Yes — P2** |
| **Foundry/Corpus** (`/projects/{slug}/corpus`) | `CorpusController@show` | `project{}`, `stats{reports, reports_with_content, passages, entity_links}`, `reports[]`, `passages[]` (recent passages) | Yes | `ingest_pdf`, `embed_pending_passages`, `sync_silver_to_kg` | Static | **Yes — P2** (corpus growth invisible) |
| **Foundry/Report** (`/projects/{slug}/reports`) | `ReportController@index` | `project{}`, `reports[]` (with parse_quality_pct), `is_admin` | Yes | `ingest_pdf`, `generate_report` | Static | **Yes — P2** |
| **Foundry/ReportView** (`/projects/{slug}/reports/{report_id}`) | `ReportController@view` | `project{}`, `figures`, `report{}`, `sections[]`, `passages[]`, `is_admin` | Yes | `ingest_pdf`, OCR re-runs, figure re-rendering | Static | **Yes — P3** |
| **Foundry/AssessmentSummary** (`/projects/{slug}/reports/{report_id}/assessment-summary`) | `AssessmentSummaryController@show` | structured assessment summary + completeness audit | Yes | `assessment_summary` regenerate FastAPI route + `completeness` audit | `router.reload({only:['summary','completeness_audit']})` after POST | No |
| **Foundry/RetrievalInspector** (`/retrieval/{traceId}`) | `RetrievalInspectorController@show` | `trace_id`, `run{}`, `plan`, `retrieval_items[]`, `citations[]`, `empty` | No (post-query read-only) | — | Static | No |
| **Foundry/SavedMapViews** (`/projects/{slug}/saved-views`) | `SavedMapViewsController@show` | `project_id`, `views[]`, `empty` | No (user-driven CRUD via `/api/v1/projects/{p}/saved-map-views`) | — | Static | No |
| **Foundry/Decisions** (`/projects/{slug}/decisions`) | `DecisionsController@show` | `project{}`, `decisions[]`, `empty` | Yes (in-app POST + audit anchoring) | `outbox_dispatcher`, `audit_ledger_verify` | Static | No (user-driven only) |
| **Foundry/AuditLog** (`/projects/{slug}/audit`) | `AuditLogController@show` | `project{}`, `totals{queries,refused,avg_latency,total_tokens}`, `refusal_pct`, `rows[]` | Yes | every query writes audit; `audit_ledger_verify` workflow | Static | **Yes — P2** (new queries don't append) |
| **Foundry/Inbox** (`/inbox`) | `InboxController@show` | `mentions[]`, `reviews[]`, `refusals[]`, `empty` | Yes | mention creators, review-request creators, refused queries | Static | **Yes — P2** (no notification badge updates) |
| **Foundry/Settings** (`/settings`) | `SettingsController@show` | `workspace{id,name,slug,data_version}`, `member_count`, `can_admin` | Yes | workspace mutations | Static | No |
| **Foundry/Investigations** (`/projects/{slug}/investigations`) | `InvestigationsController@show` | `project{}`, `investigations[]`, `empty` | Yes | chat conversation creation | Static | **Yes — P2** |
| **Foundry/ProjectAnalytics** (`/projects/{slug}/analytics`) | `ProjectAnalyticsController@show` | KPIs/timeseries from silver + audit | Yes | `mv_refresh_silver`, `reliability_metrics_publisher` | Static | **Yes — P2** |
| **Foundry/Targets** (`/projects/{slug}/targets`) | `TargetsController@show` | `project{}`, `models[]`, `targets[]` (rank/score/confidence/summary/positives/negatives/analogues/next_data/constraints/geochem) | Yes | `score_targets`, `train_target_model`, `continuous_learning_loop` | Static | **Yes — P1/P2** (model run completion invisible until reload) |
| **Foundry/Rationale** (`/projects/{slug}/targets/{targetId}/rationale`) | `RationaleController@show` | `target_id`, `project{}`, `rank`, `coord`, `confidence`, `summary`, `positives`, `negatives`, `analogues` | Yes | `score_targets`, `train_target_model` | Static | **Yes — P3** |
| **Foundry/DrillReview** (`/projects/{slug}/drill-review`) | `DrillReviewController@show` | review queue (silver.review_queue) | Yes | drill-upload Dagster writes queue rows; review POST decides | Static (POST→full reload) | **Yes — P1** (new queue items arrive during ingest) |
| **Foundry/WhatChangedFeed** (`/projects/{slug}/whats-changed`) | `WhatChangedController@show` | what-changed digest rows | Yes | `what_changed_detector`, `what_changed_weekly` | Static | **Yes — P3** |
| **Foundry/Workspace** (`/projects/{slug}/workspace`) | `WorkspaceController@show` (3D mode, 9 sub-views per memory) | hole geometry, traces, lithology, log curves, structures, cross-sections, etc. | Yes | `sync_silver_to_kg`, `mv_refresh_silver`, ingest jobs | Static | **Yes — P3** |
| **Foundry/SupportCockpit** (`/support-cockpit`) | `Foundry/SupportCockpitController@show` | support tickets / replay runs | Yes | `support_replay` | Static | **Yes — P2** |
| **Foundry/Tier3Unlock** (`/public-geoscience/tier3-unlock`) | `Tier3Controller@show` | unlock state, request form | No (user POST) | — | Static | No |
| **Foundry/PublicGeo** | (redirect to `/public-geoscience`) | controller exists, unwired | — | — | — | n/a |
| **Foundry/DataImportWizard** (`/foundry/imports/wizard`) | inline render | none | Yes | drill-upload Dagster, `ingest_pdf` | `setInterval` poll (locally driven, no server channel) | **Yes — P1** (wizard reports progress via interval-driven local fetch, no cross-tab broadcast) |
| **PublicGeoscience/Index** (`/public-geoscience`) | `PublicGeoscienceController@index` | none (jurisdiction picker fetches `/api/v1/public-geoscience/jurisdictions` client-side) | Yes | `public_geoscience_pull` (Kestra → Hatchet) | Static + on-demand API | **Yes — P3** (new jurisdiction pulls invisible) |
| **Dashboards/EvidenceQuality** | `/dashboards/evidence-quality` | props from `CustomerDashboardsController@evidenceQuality` (citation/refusal stats) | Yes | every RAG query write | Static | **Yes — P3** |
| **Dashboards/VisualReadiness** | `/dashboards/visual-readiness` | viz readiness scores | Yes | `mv_refresh_silver`, viz-render jobs | Static | **Yes — P3** |
| **Dashboards/PublicGeoOverlay** | `/dashboards/publicgeo-overlay` | public-geoscience overlay metrics | Yes | `public_geoscience_pull` | Static | **Yes — P3** |
| **Dashboards/TargetRecommendation** | `/dashboards/target-recommendation` | target-model rollups | Yes | `score_targets`, `train_target_model` | Static | **Yes — P3** |
| **Dashboards/Reporting** | `/dashboards/reporting` | report KPIs | Yes | `generate_report` | Static | **Yes — P3** |
| **Dashboards/LlmCost** | `/dashboards/llm-cost` | cost rollups | Yes | `cost_burn_watcher`, query-time cost writes | Static | **Yes — P2** |
| **InterpretationWorkspace** (`/projects/{projectId}/interpretation`) | `InterpretationWorkspaceController@index` | notes/section-lines/zones (proxied to FastAPI `/v1/interpretation/*`) | No (user-driven CRUD; FastAPI handles writes) | — | `fetch` proxy GET/POST/PUT/DELETE | No |
| **ChartsGallery** (`/charts/gallery`) | `ChartsGalleryController@gallery` | `chart_kinds` | No | — | `fetch /api/v1/charts/render` | No |
| **Onboarding/Wizard** (`/onboarding`) | `OnboardingController@index` | wizard state | No (user POST) | — | `fetch` POST/upload | No |
| **Admin/WorkflowRuns** (`/admin/workflow-runs`) | `WorkflowRunController@index` | workflow_runs list (all engines) | Yes | every Hatchet workflow writes here | Static | **Yes — P1** (operator watches live runs) |
| **Admin/HatchetWorkers** (`/admin/hatchet-workers`) | `HatchetWorkersController@index` | worker list + recent runs | Yes | worker heartbeats | Static | **Yes — P1** |
| **Admin/IngestionReview** (`/admin/ingestion-review`) | `IngestionReviewController@index` + `.json` | queue list / item detail / page render | Yes | `ingest_pdf`, drill-upload | `fetch` for item detail + `router.reload({only:['queue','summary']})` after PATCH | **Yes — P1** (new review items don't appear without manual reload) |
| **Admin/ClusterIngest** (`/admin/cluster-ingest`) | `ClusterIngestController@index` | cluster-ingest phases | Yes | cluster-ingest pipeline (bronze.ingest_runs, manifest, silver collars/passages/embeddings) | Static | **Yes — P1** |
| **Admin/CacheTelemetry** (`/admin/cache-telemetry`) | `CacheTelemetryController@index` | (page-only render) | Yes | query cache writers | `fetch /admin/cache-telemetry/skip-reasons.json` (manual window slider) | **Yes — P2** |
| **Admin/EvalDashboard** (`/admin/eval-dashboard`) | `EvalDashboardController@index` | golden_questions / run_summaries / ontology terms | Yes | `eval_real_rag_nightly`, `evaluate_workspace` | Static | **Yes — P2** |
| **Admin/EvalQuestions** (`/admin/eval/questions`) | `EvalQuestionsController@index` | question list | Yes | user CRUD (no async) | Static + `router.reload` | No |
| **Admin/EvalQuestionEditor** (`/admin/eval/questions/{id\|new}`) | `EvalQuestionsController@show/create` | one question | Yes | dry-run dispatch returns result | `fetch transition` + `fetch dry-run` + `router.reload` | No |
| **Admin/EvalCompare** (`/admin/eval/compare`) | `EvalCompareController@index` | run list | Yes | `eval_real_rag_nightly` | `fetch runs/{id}.json` + `fetch assess` | No |
| **Admin/DecisionHistory** (`/admin/decision-history`) | `DecisionHistoryController@index` | decision_records cross-workspace | Yes | `outbox_dispatcher` | Static | **Yes — P3** |
| **Admin/DecisionNew** (`/admin/decisions/new`) | `DecisionHistoryController@create` | form | No | — | POST | No |
| **Admin/SupportCockpit** (`/admin/support-cockpit`) | `SupportCockpitController@index` | tickets / access / replay runs | Yes | `support_replay`, support agents | `fetch` per-agent POST | **Yes — P2** |
| **Admin/HypothesisWorkspace** (`/admin/hypothesis-workspace`) | `HypothesisWorkspaceController@index` | hypotheses + evidence links | Yes | `continuous_learning_loop`, `field_outcome_learning` | Static | **Yes — P3** |
| **Admin/TargetRecommendationRuns** (`/admin/target-recommendation/runs`) | `TargetRecommendationCockpitController@index` | run list | Yes | `score_targets`, `train_target_model` | Static | **Yes — P1** |
| **Admin/TargetRecommendationCockpit** (`/admin/target-recommendation/runs/{run_id}`) | `TargetRecommendationCockpitController@show` | one run | Yes | `score_targets` | `fetch signoff` + `router.reload({only:['run']})` | **Yes — P1** (active run progress doesn't stream; only post-sign-off reload) |
| **Admin/ReportBuilder** (`/admin/reports`) | `ReportBuilderController@index` | builds list | Yes | `generate_report` | `fetch build` + `router.reload({only:['builds']})` | **Yes — P1** (new build appears only after manual reload of list) |
| **Admin/ReportBuild** (`/admin/reports/{build_id}`) | `ReportBuilderController@show` | one build with sections | Yes | `generate_report` (long-running) | **Echo private channel** for build progress (via `/internal/admin/reports/{build_id}/progress`) + section save/export `fetch` | No |
| **Admin/MlTrainingRuns** (`/admin/ml/training-runs`) | `MlTrainingRunsController@index` | runs list | Yes | `train_target_model`, `train_source_trust` | `fetch /admin/ml/{endpoint}` + `router.reload({only:['runs']})` | **Yes — P1** (training progress not streamed; manual reload only) |
| **Admin/Dashboards** (`/admin/dashboards`) | `AdminDashboardsController@index` | Grafana links | No | — | Static | No |
| **Admin/Conflicts** (`/admin/conflicts`) | `ConflictsController@index` | conflict list | Yes | conflict detector | `fetch run` + page reload | **Yes — P2** |
| **Admin/AuditFindings** (`/admin/audit`) | `AuditFindingsController@index` | findings + cold-tier archive runs | Yes | `cold_tier_archive` | `fetch` + `router.reload({only:['archive_runs']})` | **Yes — P1** (archive run completion not streamed) |
| **Admin/WhatChanged** (`/admin/what-changed`) | `WhatChangedController@index` | digest | Yes | `what_changed_detector`, `what_changed_weekly` | Static | **Yes — P3** |
| **Admin/SourceTrust** (`/admin/source-trust`) | `AdminMiscController@sourceTrust` | trust scores | Yes | `train_source_trust` | Static | **Yes — P3** |
| **Admin/ExportGate** (`/admin/export-gate`) | `AdminMiscController@exportGate` | export gate state | Yes | `workspace_export`, `outbox_dispatcher` | Static | **Yes — P2** |
| **Admin/LoadTest** (`/admin/load-test`) | `AdminMiscController@loadTest` | k6 results | No | — | Static | No |
| **Admin/Recommendations** (`/admin/recommendations`) | `Tier234Controller@recommendations` | (no props) | Yes | NBD / analogue scorers | POST + reload | **Yes — P2** |
| **Admin/QpCredentials** (`/admin/qp-credentials`) | `Tier234Controller@qpCredentials` | credentials list | No | — | `fetch verify` + `router.reload` | No |
| **Admin/WorkspaceMembers** (`/admin/workspace-members`) | `Tier234Controller@workspaceMembers` | members | No | — | Static | No |
| **Admin/WorkspaceSettings** (`/admin/workspace-settings/{ws}`) | `Tier234Controller@workspaceSettings` | workspace config | No | — | `fetch` save | No |
| **Admin/AuditExplorer** (`/admin/audit-explorer`) | `Tier234Controller@auditExplorer` | audit_ledger | Yes | `audit_ledger_verify`, every query writes | Static | **Yes — P2** |
| **Admin/BackupsDashboard** (`/admin/backups`) | `Tier234Controller@backupsDashboard` | backup status | Yes | `backup_postgres`, `backup_neo4j`, `backup_qdrant`, `backup_redis`, `backup_seaweedfs`, `cold_tier_archive` | Static | **Yes — P2** (running backup progress invisible) |
| **Admin/PhaseH4Health** (`/admin/phase-h4-health`) | `Tier234Controller@phaseH4Health` | composite health | Yes | many | `router.reload` only | **Yes — P1** |
| **Admin/SavedMaps** (`/admin/saved-maps`) | `Tier234Controller@savedMaps` | maps list | No | — | Static | No |
| **Admin/AlertsInbox** (`/admin/alerts-inbox`) | `Tier234Controller@alertsInbox` | alerts | Yes | `cost_burn_watcher`, `reliability_metrics_publisher`, `stale_run_detector` | `fetch` ack + `router.reload({only:['items']})` | **Yes — P1** (new alerts don't push) |
| **Admin/Integrations** (`/admin/integrations`) | `IntegrationsController@index` | flows, senders, jwt keys, history | Yes | `flow_jwt_key_reaper`, Kestra/Activepieces flows | multiple `router.reload({only:[...]})` after CRUD | **Yes — P2** (Kestra flow run history not streamed) |
| **Admin/AgentConfig/Pins** | `/admin/agent-config/pins` | pins | No | — | PATCH + reload | No |
| **Admin/AgentConfig/Prompts** | `/admin/agent-config/prompts` | prompts | No | — | PATCH + reload | No |
| **Admin/AgentConfig/Timeouts** | `/admin/agent-config/timeouts` | timeouts | No | — | PATCH + reload | No |
| **Admin/AgentConfig/Workspaces** | `/admin/agent-config/workspaces` | workspace agent overrides | No | — | PATCH + reload | No |
| **Admin/ShadowRuns/Index, Show** | (routes removed per web.php line 462; controller orphaned) | n/a | — | — | n/a | n/a (retired) |
| **NotFound** | implicit | n/a | n/a | — | Static | No |

### Tile / Map layers (MapLibre + Martin)

These aren't pages — they are layer sources consumed by **Foundry/Workspace**, **Foundry/SavedMapViews**, **PublicGeoscience/Index**, and **Dashboards/PublicGeoOverlay** via the `/tiles/public-geoscience/{source}/{z}/{x}/{y}.pbf` and `/tiles/silver/{source}/{z}/{x}/{y}.pbf` proxy routes (`PublicGeoscienceTileProxy`). Martin functions/views are listed below with the writer jobs.

| Tile source | PostGIS table/view | Writer job | Cache strategy | Gap? |
|---|---|---|---|---|
| `pg_collars_by_project`, `pg_drill_traces_by_project`, `pg_seismic_by_project` | `silver.collars`, `silver.drill_traces`, `silver.seismic` | drill-upload Dagster, `sync_silver_to_kg`, `mv_refresh_silver` | ETag from `silver.projects.data_version` (daily) | **Yes — P2** (data_version bump not pushed to client; client only re-fetches on map reload) |
| `pg_cross_section_lines` | `gold.cross_section_panels` | gold mv refresh | ETag | **Yes — P3** |
| `significant_intersections_by_project` | `gold.significant_intersections` + `silver.collars` | `mv_refresh_silver` | ETag | **Yes — P3** |
| `density_choropleth_h3` | `gold.h3_density_mineral` | gold mv refresh | ETag | **Yes — P3** |
| `pg_mines`, `pg_mineral_occurrences`, `pg_drillhole_collars`, `pg_rock_samples`, `pg_assessment_surveys`, `pg_resource_potential`, `pg_mineral_dispositions`, `pg_bedrock_geology` (8 public-geo views) | `public_geo.v_pg_*_mvt` | Kestra `public_geoscience_pull` → Hatchet `public_geoscience_pull` workflow | Cache-Control public 24h | **Yes — P3** (no invalidation push) |
| `smdi_deposits` | `public.smdi_deposits` | SMDI parallel pipeline (overnight) | static | **Yes — P3** |

### Internal Reverb broadcast endpoints (already wired)

- `POST /internal/admin/reports/{build_id}/progress` — broadcasts to private `report.build.{build_id}` (consumed by **Admin/ReportBuild**)
- `POST /internal/v1/ingest-progress/broadcast` — broadcasts on private `project.{projectId}.ingestion` (consumed by **Foundry/IngestionRuns** only)

### Hatchet workflows discovered (writer inventory)

`ingest_pdf`, `embed_pending_passages`, `generate_report`, `score_targets`, `train_target_model`, `train_source_trust`, `continuous_learning_loop`, `field_outcome_learning`, `evaluate_workspace`, `eval_real_rag_nightly`, `sync_silver_to_kg`, `mv_refresh_silver`, `what_changed_detector`, `what_changed_weekly`, `shadow_diff` (archived), `support_replay`, `tiff_normalize`, `tiff_ocr_cluster`, `ocr_quality_check`, `re_ocr_page`, `cold_tier_archive`, `nightly_ingestion_integrity`, `outbox_dispatcher`, `reliability_metrics_publisher`, `stale_run_detector`, `idempotency_keys_cleanup`, `flow_jwt_key_reaper`, `cost_burn_watcher`, `audit_ledger_verify`, `backup_postgres`, `backup_neo4j`, `backup_qdrant`, `backup_redis`, `backup_seaweedfs`, `restore_workspace`, `workspace_export`, `public_geoscience_pull`, `external_notification`, `phase0_agents`, `phase2_smoke`, `worker`.

### Kestra flows

- `public_geoscience_pull` (cron 6h → SeaweedFS bronze + FastAPI trigger)
- `external_notification` (inbound webhook → FastAPI)
- `support_packet_dispatch` (failure-triggered → FastAPI packet assemble + SMTP)

---

## Gap priority grouping

### P1 — user is actively waiting (ingestion status, AI answer, document processing stages)

- **Foundry/Overview** — ingest card uses `.json` poll, but headline KPIs (collars, samples, log curves, queries) are static-rendered; user starting ingest stays on this page and sees no movement except in the small card.
- **Foundry/IngestQuality** — quality metrics are computed at ingest time; while the user watches an upload, the page never refreshes.
- **Foundry/DataImportWizard** — `setInterval` polls *locally*; the wizard has no server-side Reverb channel, so a parallel ingestion run from another tab/CLI is invisible.
- **Foundry/DrillReview** — new review-queue items appear during drill-upload but the page is fully static.
- **Foundry/Targets** — a `score_targets` run finishing while the user views this page is invisible until they nav away/back.
- **Admin/WorkflowRuns** — the workflow-runs dashboard itself does not auto-refresh; the operator's primary status surface is static.
- **Admin/HatchetWorkers** — worker heartbeats invisible without reload.
- **Admin/IngestionReview** — new SRQ items written by the in-flight ingest don't appear; `router.reload({only:['queue','summary']})` only fires after the operator PATCHes a disposition.
- **Admin/ClusterIngest** — phases A/B/C/D progress static.
- **Admin/TargetRecommendationRuns + Cockpit** — `score_targets` run progress invisible; only post-sign-off reload.
- **Admin/ReportBuilder (index)** — new builds appear only after manual reload of the list (the *detail* view does have Echo).
- **Admin/MlTrainingRuns** — training progress not streamed; manual reload only.
- **Admin/AuditFindings** — `cold_tier_archive` completion not streamed.
- **Admin/PhaseH4Health** — composite health page, but no live refresh of its components.
- **Admin/AlertsInbox** — `cost_burn_watcher` / `reliability_metrics_publisher` / `stale_run_detector` write alerts that don't push.

### P2 — user notices within the session (project list, document counts, map layers)

- **Foundry/Portfolio** — `projects[]`, `kpis[]`, `recent_activity[]` all static.
- **Foundry/Projects** — new projects/uploads not visible without navigation.
- **Foundry/Lakehouse** — bronze/silver/gold counts stale during ingest run.
- **Foundry/Explorer** — newly ingested holes don't appear until reload.
- **Foundry/Sources** — parser activity counts/bytes static.
- **Foundry/Corpus** — reports / passages / entity-links counts static.
- **Foundry/Report (index)** — new generated reports invisible.
- **Foundry/AuditLog** — new queries don't append; user issues a query in another tab and audit doesn't grow.
- **Foundry/Inbox** — no notification badge updates for new mentions/reviews/refusals.
- **Foundry/Investigations** — new conversations from chat invisible.
- **Foundry/ProjectAnalytics** — KPIs from `mv_refresh_silver` static.
- **Foundry/SupportCockpit** — tickets static.
- **Dashboards/LlmCost** — cost rollups updated by `cost_burn_watcher` invisible.
- **Admin/CacheTelemetry** — only refreshes on window slider.
- **Admin/EvalDashboard** — nightly eval runs invisible.
- **Admin/SupportCockpit** — `support_replay` runs invisible.
- **Admin/Conflicts** — conflict-detector output static.
- **Admin/AuditExplorer** — `audit_ledger_verify` output static.
- **Admin/BackupsDashboard** — running backups static.
- **Admin/Integrations** — Kestra flow run history not streamed.
- **Admin/ExportGate** — `workspace_export` / `outbox_dispatcher` writes invisible.
- **Admin/Recommendations** — NBD/analogue scorers async; result invisible.
- **Tile/MapLibre — silver project layers** — `silver.projects.data_version` bump after ingest is not pushed; cached tiles stale.

### P3 — user notices on next visit (metadata, background geoscience pulls)

- **Foundry/DrillholeDetail** — rare to mutate mid-view but no auto-refresh on backfill.
- **Foundry/HoleCompare** — same.
- **Foundry/Reasoning + Hypothesis** — `continuous_learning_loop` / `field_outcome_learning` outputs.
- **Foundry/SourceGraph** — `sync_silver_to_kg` rebuilds.
- **Foundry/Rationale** — model re-scoring.
- **Foundry/ReportView** — OCR re-runs / figure re-renders.
- **Foundry/Workspace** — 3D mode + 9 sub-views, all static.
- **Foundry/WhatChangedFeed** — weekly digest.
- **PublicGeoscience/Index** — `public_geoscience_pull` cron (every 6h) invisible.
- **Dashboards/EvidenceQuality**, **VisualReadiness**, **PublicGeoOverlay**, **TargetRecommendation**, **Reporting** — all static admin dashboards.
- **Admin/DecisionHistory** — outbox replay invisible.
- **Admin/HypothesisWorkspace** — same engines as Foundry/Reasoning.
- **Admin/WhatChanged**, **Admin/SourceTrust** — model-trained metadata.
- **Tile/MapLibre — gold layers** (`cross_section_lines`, `significant_intersections`, `density_choropleth_h3`).
- **Tile/MapLibre — 8 public-geo views** + **smdi_deposits** — no invalidation push.
