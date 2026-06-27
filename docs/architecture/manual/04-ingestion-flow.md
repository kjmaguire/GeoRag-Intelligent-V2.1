# Chapter 04 — Ingestion Flow

From a file appearing in the browser to a citable answer in the chat. Every
step here has a file:line anchor.

## 1. The 30-second picture

```
Browser ── multipart upload ──▶ laravel-octane
                                     │
                                     ├─ insert bronze.upload_files row
                                     ├─ PUT to SeaweedFS  bronze/<workspace>/<project>/<sha>/<file>
                                     ├─ insert bronze.ingest_runs (status=running)
                                     └─ dispatch Hatchet workflow (ingest_pdf | ingest_csv | …)
                                                              │
                                                              ▼
                              ┌──────────────────────────────────────────┐
                              │ hatchet-worker-ingestion (WORKER_POOL=…) │
                              │   preflight → parse → persist            │
                              │   → outbox.pending_propagations          │
                              │   → embed_verify (kicks off ai-pool)     │
                              │   → broadcast Reverb event               │
                              └────────────────────┬─────────────────────┘
                                                   │
                            ┌──────────────────────┼──────────────────────┐
                            ▼                      ▼                      ▼
                  hatchet-worker-ai      Neo4j (graph upsert)    Qdrant (vector index)
                  embed_pending_passages         │                       │
                          │                      └───── outbox_dispatcher (every minute)
                          ▼
                     Qdrant points
                          │
                          ▼
                  silver.ingest_progress (status=completed)
                          │
                          └─ broadcast workspace-data-updated.{workspace_id}
                                                 │
                                                 ▼
                                       browser refetches affected views
```

## 2. Upload endpoints (Laravel)

`POST /api/projects/{project}/uploads` — main entry for drill data uploads
(CC-01 Item 1 flow). Controller dispatches a Laravel queue job which:

1. Validates the multipart form (`PHP_UPLOAD_MAX_FILESIZE=2G`,
   `POST_MAX_SIZE=2G`, Swoole `package_max_length` and `socket_buffer_size`
   all raised in lockstep — see
   [project_upload_size_stack_2026_05_21](../notes/INDEX.md#project_upload_size_stack_2026_05_21)).
2. Streams the upload to SeaweedFS through aioboto3 (S3 endpoint
   `http://minio:8333`, `AWS_USE_PATH_STYLE_ENDPOINT=true`).
3. Writes a `bronze.upload_files` row + a `bronze.ingest_runs` row with
   `status='running'`.
4. Calls the FastAPI shadow ingest trigger endpoint:
   `POST /internal/v1/shadow/ingest_pdf/trigger` (Service-Key
   `FASTAPI_SERVICE_KEY` HMAC).
5. FastAPI uses the Hatchet client (`HATCHET_CLIENT_TOKEN`,
   `HATCHET_CLIENT_HOST_PORT=hatchet-lite:7077`) to enqueue the appropriate
   workflow.

QField uploads land via a separate sub_type=218 route documented in
[project_cc03_item4_qfield_ingestion](../notes/INDEX.md#project_cc03_item4_qfield_ingestion).

## 3. The ingest_pdf Hatchet workflow

[src/fastapi/app/hatchet_workflows/ingest_pdf.py](../../../src/fastapi/app/hatchet_workflows/ingest_pdf.py).
Decomposed into 5 steps + 1 on-failure task:

| Step | Function | What it does | Side-effects |
|------|---------|---|---|
| `preflight` | `preflight()` | S3 GET, magic-byte check, sha256, page count, size cap | Updates `silver.ingest_progress` to `preflight_complete` |
| `parse` | `parse()` → `_parse_body()` → `_run_parser_subprocess()` | Runs the §04p PDF stack (see [Ch 05](05-pdf-stack.md)) in a memory-guarded subprocess pool | Caches the body bytes under `BRONZE_LOCAL_DIR` |
| `persist` | `persist()` → `_persist_body()` | Writes `silver.reports`, `silver.report_pages`, table extractions, figure captions; inserts into `bronze.provenance`; bumps `silver.workspaces.data_version` | Bronze provenance trigger auto-fills `workspace_id` |
| `embed_verify` | `embed_verify()` | Inserts a verify task into Hatchet for the `ai` pool to pick up; gates the run on the embed sweep finishing | Posts a Reverb progress event |
| `p04p_dual_write` | `p04p_dual_write()` | When `P04P_DUAL_WRITE_ENABLED=true`, also runs the legacy parser and diffs (shadow A/B) | Writes to `silver.shadow_runs` |
| `on_failure` | `on_failure_task` (line 1428) | Marks the run failed, broadcasts a failure Reverb event | Hatchet retries this task itself (retries=2) |

Memory protection:
- `_compute_parse_max_workers()` returns `min(os.cpu_count(), 4)` when
  `PARSE_SUBPROCESS_MAX_WORKERS` is empty.
- `_wait_for_memory_headroom()` awaits `psutil.virtual_memory().available ≥ MIN_FREE_RAM_MB`,
  raising `MemoryError` after `MEMORY_WAIT_MAX_S` so Hatchet retries on a
  freer worker
  ([docker-compose.yml:2055-2065](../../../docker-compose.yml)).

## 4. Other ingest_* workflows

Located under [src/fastapi/app/hatchet_workflows/](../../../src/fastapi/app/hatchet_workflows/):

| Workflow | Module | Triggers |
|---|---|---|
| `ingest_pdf` | `ingest_pdf.py` | Drill report uploads, NI 43-101 PDFs |
| `re_ocr_page` | `re_ocr_page.py` | Per-page re-OCR triggered from the `/api/internal/v1/re-ocr` Laravel endpoint |
| `tiff_ocr_cluster` | `tiff_ocr_cluster.py` | Phase E.1 — bulk TIFF OCR (now DEPRECATED by `tiff_normalize` per ADR-0005) |
| `ocr_quality_check_wf` | `ocr_quality_check.py` | Post-ingest quality audit; writes `silver.ocr_page_quality` |
| `embed_pending_passages_wf` | `embed_pending_passages.py` | AI pool; selects rows from `silver.document_passages WHERE embed_status='pending'`, embeds via bge-small, upserts to Qdrant |
| `outbox_dispatcher` | `outbox_dispatcher.py` | Cron `* * * * *`; polls `outbox.pending_propagations FOR UPDATE SKIP LOCKED` and fans to Qdrant/Neo4j/SeaweedFS |
| `audit_ledger_verify` | `audit_ledger_verify.py` | Cron `0 2 * * *`; verifies the previous 24 h hash chain |
| `stale_run_detector` | `stale_run_detector.py` | Cron; closes `bronze.ingest_runs` left stuck `running` past TTL |
| `nightly_ingestion_integrity` | `nightly_ingestion_integrity.py` | Cron; cross-checks bronze vs silver row counts |
| `reliability_metrics_publisher` | `reliability_metrics_publisher.py` | Cron; publishes SLI gauges |
| `mv_refresh_silver` | `mv_refresh_silver.py` | Refreshes silver materialised views |
| `sync_silver_to_kg` | `sync_silver_to_kg.py` | Pushes silver canonical rows into Neo4j |
| `score_targets` | `score_targets.py` | Re-runs target scoring; broadcasts `workspace-data-updated` |
| `backup_postgres / backup_neo4j / backup_qdrant / backup_redis / backup_seaweedfs` | one each | Daily backups, write to SeaweedFS `georag-backups/*` |
| `cold_tier_archive_workflow` | `cold_tier_archive.py` | Storage Tiering Agent — moves old bronze objects between `tier-hot/warm/cold` |
| `evaluate_workspace` | `evaluate_workspace.py` | On-demand workspace eval against golden queries |
| `external_notification` | `external_notification.py` | HMAC-signed external-notification fan-out |
| `flow_jwt_key_reaper` | `flow_jwt_key_reaper.py` | Per-flow JWT key rotation |
| `generate_report` | `generate_report.py` | NI 43-101-style report generation |
| `idempotency_keys_cleanup` | `idempotency_keys_cleanup.py` | Reaps stale `workspace.idempotency_keys` |
| `phase2_smoke` | `phase2_smoke.py` | E2E smoke test for the pipeline |
| `public_geoscience_pull` | `public_geoscience_pull.py` | Kestra-triggered government-data refresh |
| `restore_workspace` | `restore_workspace.py` | DR — restore a workspace from backups |
| `shadow_diff` | `shadow_diff.py` | A/B compare ingest paths |
| `support_replay` | `support_replay.py` | Support Cockpit replay |
| `continuous_learning_loop` | `continuous_learning_loop.py` | Closes the loop on field-outcome → re-rank feedback |
| `field_outcome_learning` | `field_outcome_learning.py` | Field outcome ingestion |
| `eval_real_rag_nightly` | `eval_real_rag_nightly.py` | Nightly RAG quality eval |
| `phase0_agents` | `phase0_agents.py` | Phase 0 agent registry (Index Health, Storage Tiering, …) |
| `workspace_export` | `workspace_export.py` | Workspace data export |
| `tiff_normalize` | `tiff_normalize.py` | ADR-0005 — normalise multi-page TIFFs to PDF, then dispatch `ingest_pdf` |
| `train_source_trust` | `train_source_trust.py` | Experimental — write `silver.source_trust_scores` from feedback signals |
| `train_target_model` | `train_target_model.py` | Experimental — refresh target-scoring model artifacts |
| `what_changed_detector` | `what_changed_detector.py` | Detect workspace state diffs for the activity feed |
| `what_changed_weekly` | `what_changed_weekly.py` | Weekly aggregate that backs the WhatChangedFeed page |

## 5. CSV / XLSX / GPKG / LAS / SEG-Y ingestion

Non-PDF formats go through **Dagster** assets, not Hatchet — they’re bulk
imports where the orchestrator owns the run, not the user. The bronze→silver
assets live under
[src/dagster/georag_dagster/assets/bronze_to_silver/](../../../src/dagster/georag_dagster/assets/bronze_to_silver/).

Notable assets ([src/dagster/georag_dagster/assets/](../../../src/dagster/georag_dagster/assets/)):

| Asset | Source | Target |
|---|---|---|
| `bronze.py` | Generic raw-file → bronze rows | `bronze.upload_files`, `bronze.ingest_manifest` |
| `bronze_xlsx.py` | XLSX workbooks | `bronze.raw_*` tables; sheet-type classifier reuses CSV aliases |
| `bronze_lithology.py` | Drill log CSVs | `bronze.raw_lithology_logs` |
| `bronze_samples.py` | Sample CSVs | `bronze.raw_samples` |
| `bronze_surveys.py` | Downhole survey CSVs | `bronze.raw_surveys` |
| `bronze_geophysics.py` | Geophysics CSV/SEG-Y | `bronze.raw_geophysical_runs` |
| `bronze_seismic.py` | SEG-Y volumes | `bronze.seismic_*` |
| `bronze_spatial.py` | GPKG/GeoJSON/shapefile | `bronze.spatial_*` |
| `bronze_well_logs.py` | LAS files | `bronze.well_log_curves` |
| `bronze_xyz.py` | Point clouds | `bronze.spatial_features` |
| `bronze_public_geoscience.py` | Provincial open-data feeds | `public_geo.*` |
| `silver_collars_canonicalize_backfill.py` | Bronze → canonical collars | `silver.collars` |
| `silver_lithology.py` | Bronze → canonical | `silver.lithology` (+ legacy `silver.lithology_logs`) |
| `silver_samples.py` | Bronze → canonical | `silver.samples` |
| `silver_drill_traces.py` | Survey + collar → LineString | `silver.drill_traces` |
| `silver_geophysics.py` | Geophysics canonical | `silver.geophysics_surveys` |
| `silver_geochronology.py` | Geochronology samples | `silver.geochronology_samples` |
| `silver_reports.py` | Already-parsed PDFs → canonical | `silver.reports`, `silver.report_pages` |
| `silver_raster.py` | Raster ingest | `silver.raster_layers` |
| `silver_cog_rasters.py` | COG production | `silver.cog_rasters` |
| `silver_entity_ner_backfill.py` | NER over text rows | `silver.entities` |
| `gold_h3_density.py` | H3 aggregation | `gold.h3_density_mineral` |
| `gold_cross_section_panels.py` | Section line ↔ collar projection | `gold.cross_section_panels` |
| `gold_drillhole_intervals_visual.py` | Drill interval projection | `gold.drillhole_intervals_visual` |
| `gold_structure_measurements_visual.py` | Structure projection | `gold.structure_measurements_visual` |
| `gold_cross_corpus_linker.py` | Entity linking across corpora | `silver.entity_links` |
| `silver_to_gold/assay_composites.py` | Weighted-avg grade over intervals | `gold.assay_composites` |
| `silver_to_gold/significant_intersections.py` | Notable grade intercepts | `gold.significant_intersections` |
| `silver_to_gold/drill_summaries.py` | One row per hole rollup | `gold.drill_summaries` |
| `silver_to_gold/zone_statistics.py` | Grade/thickness per mineralisation zone | `gold.zone_statistics` |
| `silver_to_gold/qaqc_statistics.py` | Pass-rate rollups by lab/element | `gold.qaqc_statistics` |
| `silver_to_gold/campaign_summaries.py` | One row per drilling campaign | `gold.campaign_summaries` |
| `silver_to_gold/element_correlations.py` | Pearson r between element pairs | `gold.element_correlations` |
| `index_neo4j.py` | Silver → Neo4j graph | Neo4j |
| `index_public_geoscience.py` | public_geo → Qdrant | Qdrant `public_geoscience` collection |
| `index_reports.py` | silver.document_passages → Qdrant | Qdrant `reports` collection |
| `index_document_passages.py` | silver.document_passages → Qdrant | Qdrant `georag_chunks` (canonical, ADR-0010). ⚠️ declares 384-dim — see [Ch 18 §2.1 re-index hazard](18-model-stack-evolution.md) |
| `silver_nl_summaries.py` | assays_v2 / lithology / collars → NL passages | `silver.document_passages` (chunk_kind=`structured_summary`, ADR-0012) |
| `silver_samples_nl_summary.py` | silver.samples → NL passages | `silver.document_passages` (ADR-0012) |
| `data_dictionary_dump.py` | information_schema → catalog | `s3://catalogs/data_dictionary/` (Appendix F — generator shipped) |
| `reranker_labels.py` + `reranker_labels_helpers.py` | Synthetic label generation | `eval.reranker_training_pairs` |
| `commit_ingestion_run.py` | Marks `bronze.ingest_runs` completed + broadcasts | Reverb `workspace-data-updated.{workspace_id}` |

## 6. The outbox pattern

A silver write is atomic with an `outbox.pending_propagations` insert in the
same transaction. The Hatchet `outbox_dispatcher` workflow polls
`SELECT … FOR UPDATE SKIP LOCKED` every minute and fans out to:

- **Qdrant** — for newly embedded passages
- **Neo4j** — for entity / relationship updates
- **SeaweedFS** — for any object payload mirror

Each attempt is recorded in `outbox.propagation_attempts`. After 3 transient
failures the row is dead-lettered. Idempotency is per-target — re-dispatch is
safe.

## 7. Embed dispatch race + the verify-task fix

[project_pipeline_resilience_2026_05_22](../notes/INDEX.md#project_pipeline_resilience_2026_05_22):
- The `embed_verify` step in `ingest_pdf` schedules a verify task that
  re-checks the embed sweep status.
- A 10-min cron sweep (`embed_pending_passages_wf` heartbeat) catches any
  rows the inline trigger missed if a worker crashed mid-flight.

## 8. Completion-write fix

[project_ingest_completion_terminal_2026_05_25](../notes/INDEX.md#project_ingest_completion_terminal_2026_05_25):
- Embed sweep now writes `status='completed'`.
- `stale_run_sweep` recovers any stuck runs.
- Tests must reuse the state-machine-tests workspace under RLS.

## 9. Reverb broadcast surface (post-ingest)

After `commit_ingestion_run`:

| Channel | Event | Listened by |
|---|---|---|
| `ingestion-progress.{workspace_id}` | `IngestProgress` | `IngestionRuns.tsx`, `DrillReview.tsx` |
| `workspace-data-updated.{workspace_id}` | `WorkspaceDataUpdated` | Every page that re-fetches on data change |
| `query.streaming.{run_id}` | `QueryToken` / `QueryCitation` | `Chat.tsx` for SSE-over-WS streaming |

`laravel_bridge.post_workspace_data_updated()` in
[src/fastapi/app/services/laravel_bridge.py](../../../src/fastapi/app/services/laravel_bridge.py)
is the helper FastAPI / Dagster / Hatchet all call — it POSTs to
`http://laravel-octane/api/internal/v1/broadcast/...` with the shared
`FASTAPI_SERVICE_KEY`.
