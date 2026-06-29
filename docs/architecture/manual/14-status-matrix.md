# Chapter 14 — Status Matrix

A single place to look up "is this thing real today?" for every component
the rest of the manual mentions.

## Status legend

| Marker | Meaning |
|---|---|
| **Live** | In production-ready state on the current main branch. Verified by tests + manual smoke. |
| **Live (dev-only)** | Wired and used on the dev workstation; not yet hardened for prod. |
| **Partial** | Shipping but with explicit gaps documented in the relevant chapter. |
| **Planned** | Documented + designed; not yet implemented. |
| **Deprecated** | Was live; replaced by something else; still present in the tree for rollback. |
| **Stub** | Function/table/view exists but raises or returns empty. |
| **Experimental** | Behind a feature flag, opt-in. |

## Services

| Service | Status | Notes |
|---|---|---|
| caddy | Live | Edge only for Kestra PAT/WS path |
| laravel-octane | Live | |
| laravel-horizon | Live | |
| laravel-reverb | Live | 60s channel-drop bug fixed 2026-05-21 |
| fastapi | Live | |
| hatchet-worker-ingestion | Live | |
| hatchet-worker-ai | Live | GPU passthrough live 2026-05-22 |
| hatchet-lite | Live | |
| kestra | Live | 3 active flows |
| dagster-daemon / webserver | Live (dev-only) | `dev-ingest` profile |
| postgresql | Live | |
| pgbouncer | Live | |
| redis | Live | |
| neo4j (+ warmup) | Live | Community Edition only |
| qdrant | Live | Auth off in dev — see Ch 02 §3 |
| minio (SeaweedFS) | Live | Replaces MinIO per ADR-0001 |
| martin | Live | Currently uses `georag_app`; `martin_ro` planned (Ch 02 §1.2) |
| vllm (+ warmup) | Live (dev-only) | `gpu-llm` profile; prod needs separate hardening |
| otel-collector | Live | |
| tempo | Live | |
| prometheus | Live (dev-only) | `dev-monitor` profile |
| alertmanager | Live (dev-only) | Webhook receiver not wired |
| redis_exporter | Live (dev-only) | |
| postgres_exporter | Live (dev-only) | |
| neo4j_exporter | Live (dev-only) | Custom JMX bridge |
| loki | Live (dev-only) | |
| promtail | Live (dev-only) | |
| grafana | Live (dev-only) | |
| ofelia | Live (dev-only) | |
| backup-agent | Live (dev-only) | |
| langfuse-web / -worker / clickhouse | Partial | Compose override `docker/compose.langfuse.yml`; not in default up |
| ollama | Deprecated | Removed 2026-05-17; Modelfiles archived under `docker/_deprecated/ollama/` |
| activepieces | Deprecated | Sunset Phase 3 Step 7; superseded by Kestra |
| georag-phase-e-ocr (TIFF bulk OCR) | Deprecated | Superseded by `tiff_normalize` (ADR-0005) |

## Postgres schemas

| Schema | Status |
|---|---|
| `bronze`, `silver`, `gold`, `public`, `audit`, `usage`, `outbox`, `workflow`, `workspace`, `partman`, `interpretation`, `targeting`, `ops`, `eval`, `topology`, `backups` | Live |
| `public_geo` | Live; rename to `public_geoscience` planned (see [martin.yaml:5](../../../docker/martin/martin.yaml)) |
| `silver.data_categories`, `silver.dataset_categories` | **Planned** (Ch 13) |

## Key tables

| Table | Status |
|---|---|
| `silver.workspaces`, `silver.projects`, `silver.collars`, `silver.assays_v2`, `silver.lithology`, `silver.samples`, `silver.reports`, `silver.drill_traces` | Live |
| `silver.review_queue` | Live (added 2026-05-24) |
| `silver.ingest_progress` | Partial (Phase A live; Phase B writes from Hatchet steps planned) |
| `silver.geophysics_surveys` | Live (added 2026-05-21) |
| `silver.query_traces` | Live (2026-05-26, extended 2026-05-28) — see [Ch 16 §4](16-algorithmic-spines.md) |
| `silver.data_quality_flags` | Live schema; 4 DQ writers live; rule engine partial |
| `silver.document_versions` | Live (2026-05-26) — closes document_versioning_design |
| `silver.entity_aliases`, `silver.entity_gaps` | Live (2026-05-26) — backs Spine A entity_resolver |
| `gold.repair_shadow_daily` | Partial — `repair_shadow_aggregate` workflow writes rows; Grafana dashboard owed |
| `silver.tenant_isolation_audit` | Live (2026-05-30) — Z.9 nightly verifier run log; RLS off (admin-gated); see [Ch 18 §8](18-model-stack-evolution.md) |
| `silver.archive_ingest_runs` | Live (2026-06-03) — ZIP-archive upload parent row; RLS-scoped; closes `ingest_zip_archive` silent-failure gap |
| `silver.projects.lifecycle_state` | Live (2026-05-30) — **CC-03 Item 8 LANDED** (was deferred); active/hibernated/archived/past_due; billing still unbuilt; [Ch 18 §7](18-model-stack-evolution.md) |
| `silver.document_passages.contextualized_content` | Live (2026-05-30) — Anthropic contextual-retrieval header; written by `enrich_passage_context` workflow |
| `audit.query_audit_log` quality cols | Live (2026-05-30) — `faithfulness_score` + `context_precision_score` (Qwen3-as-judge) |
| `silver.answer_runs`, `silver.answer_citation_items`, `silver.answer_citation_spans`, `silver.message_feedback`, `silver.evidence_items`, `silver.document_passages` | Live |
| `silver.hypotheses`, `silver.decision_records` (+ children) | Live |
| `silver.report_pages`, `silver.report_figures`, `silver.report_tables` | Partial — created by `ingest_pdf.persist` but column set still drifts; see appendix A |
| `silver.entities` | **Not present.** Entity rows live in `workspace.entities`; references to `silver.entities` in older docs are wrong. Tracked in appendix Z. |
| `silver.lithology_intervals` | **Not present.** The canonical table is `silver.lithology` (new) coexisting with the legacy `silver.lithology_logs`. Tracked in appendix Z. |
| `bronze.provenance` | Live; auto-fill trigger 2026-05-25 |
| `bronze.ingest_runs`, `bronze.ingest_manifest`, `bronze.ingest_triage_samples` | Live |
| `bronze.upload_files` | **Mentioned in this manual but not yet a created table.** Currently the upload flow writes only to `bronze.ingest_runs` + `bronze.ingest_manifest` + the SeaweedFS `bronze` bucket. Tracked in appendix Z; either create `bronze.upload_files` or rename existing manual references. |
| `bronze.raw_samples` | **Planned.** Drillhole bronze tables landed in [2026_05_20_060000](../../../database/migrations/2026_05_20_060000_create_bronze_drillhole_tables.php) — assay / lithology / surveys / geophysical / collars; no `raw_samples` table yet. |
| `bronze.manifest` vs `bronze.ingest_manifest` | Both exist. `bronze.ingest_manifest` is the canonical per-file manifest inside an ingest run (Phase A). `bronze.manifest` is a newer table from [2026_05_25_020540](../../../database/migrations/2026_05_25_020540_create_bronze_manifest.php) used by the May 25 ingest UI track. Rename/consolidate tracked in appendix Z. |
| `audit.audit_ledger` (+ verification_runs, fork_quarantine, query_audit_log) | Live |
| `gold.h3_density_mineral`, `gold.cross_section_panels`, `gold.drillhole_intervals_visual`, `gold.structure_measurements_visual`, `gold.mv_refresh_log` | Live |
| `gold.significant_intersections` | **Live** — persisted table from [2026_05_20_060700](../../../database/migrations/2026_05_20_060700_create_gold_drillhole_tables.php); Dagster `silver_to_gold/significant_intersections` upserts. The Martin function [2026_05_20_061000](../../../database/migrations/2026_05_20_061000_create_martin_significant_intersections_function.php) reads from it. |
| `gold.drill_summaries`, `gold.zone_statistics`, `gold.qaqc_statistics`, `gold.campaign_summaries`, `gold.element_correlations` | Live — created in the same [2026_05_20_060700](../../../database/migrations/2026_05_20_060700_create_gold_drillhole_tables.php) batch; written by `silver_to_gold/*.py` assets |
| `public.smdi_deposits` | Live (6,012 SK deposits) |
| `public_geo.pg_*` + `v_pg_*_mvt` | Live (Tier 1); Tier 2/3 sources commented out in [martin.yaml](../../../docker/martin/martin.yaml) |

## Hatchet workflows

| Workflow | Status |
|---|---|
| `ingest_pdf` | Live |
| `embed_pending_passages_wf` | Live |
| `outbox_dispatcher` | Live |
| `audit_ledger_verify` | Live |
| `stale_run_detector`, `nightly_ingestion_integrity`, `reliability_metrics_publisher` | Live |
| `re_ocr_page` | Live |
| `ocr_quality_check_wf` | Live |
| `mv_refresh_silver`, `sync_silver_to_kg` | Live |
| `score_targets` | Live |
| `external_notification` | Live (Kestra-triggered) |
| `public_geoscience_pull` | Partial — Kestra flow live, but bc_minfile/nrcan_geo paths superseded by Dagster (see note in [worker.py](../../../src/fastapi/app/hatchet_workflows/worker.py)) |
| `backup_postgres / backup_neo4j / backup_qdrant / backup_redis / backup_seaweedfs` | Live (dev-only) |
| `cold_tier_archive_workflow` | Partial — bucket policies live, lifecycle automation tested only on small sets |
| `workspace_export`, `restore_workspace` | Partial — golden path verified, larger-than-RAM workspaces unproven |
| `evaluate_workspace`, `eval_real_rag_nightly` | Live (dev-only) |
| `continuous_learning_loop`, `field_outcome_learning` | Experimental |
| `tiff_ocr_cluster` | Deprecated (replaced by `tiff_normalize` per ADR-0005) |
| `repair_shadow_aggregate` | Live (added 2026-05-27 per ADR-0009 / [Ch 16 §2](16-algorithmic-spines.md)) — cron `15 2 * * *` UTC |
| `enrich_passage_context` | Live (2026-05-30) — contextual-retrieval header generation; daily 04:30 UTC; [Ch 18 §5](18-model-stack-evolution.md) |
| `score_answer_quality` | Live (2026-05-30) — LLM-as-judge faithfulness + context-precision; [Ch 18 §6](18-model-stack-evolution.md) |
| `ingest_zip_archive` | Live (2026-06-03) — ZIP fan-out with parent-run observability; [Ch 18 §8](18-model-stack-evolution.md) |
| `tiff_normalize` | Live (ADR-0005 — normalises TIFFs to PDF then routes through `ingest_pdf`) |
| `train_source_trust` | Experimental (writes `silver.source_trust_scores`) |
| `train_target_model` | Experimental (target-scoring model refresh) |
| `what_changed_detector`, `what_changed_weekly` | Live (drives the WhatChangedFeed page) |
| `phase2_smoke`, `phase0_agents`, `shadow_diff`, `support_replay`, `cost_burn_watcher`, `generate_report`, `flow_jwt_key_reaper`, `idempotency_keys_cleanup` | Live |

## Dagster assets

| Asset group | Status |
|---|---|
| `bronze.*`, `bronze_xlsx`, `bronze_lithology`, `bronze_samples`, `bronze_surveys`, `bronze_geophysics`, `bronze_seismic`, `bronze_spatial`, `bronze_well_logs`, `bronze_xyz`, `bronze_reports`, `bronze_public_geoscience` | Live |
| `silver_collars_canonicalize_backfill`, `silver_lithology`, `silver_samples`, `silver_drill_traces`, `silver_geophysics`, `silver_geochronology`, `silver_reports`, `silver_raster`, `silver_cog_rasters`, `silver_entity_ner_backfill`, `silver_public_geoscience` | Live |
| `gold_h3_density`, `gold_cross_section_panels`, `gold_drillhole_intervals_visual`, `gold_structure_measurements_visual`, `gold_cross_corpus_linker` | Live |
| `index_neo4j`, `index_public_geoscience`, `index_reports` | Live |
| `reranker_labels` (+ helpers) | Experimental (synthetic-label pipeline) |
| `commit_ingestion_run` | Live |

## LangGraph nodes / RAG path

| Node | Status |
|---|---|
| `classify_node`, `route_node`, `execute_node`, `assemble_node`, `validate_node`, `demote_node` | Live (behind `AGENTIC_RETRIEVAL_V2_ENABLED`) |
| `persist_node` | Partial — best-effort today; see [Ch 06 §2.1](06-retrieval-and-agents.md#21-persistence-is-currently-best-effort--fix-required) |
| Six hallucination layers | Live |
| OIUR parser | Live (behind `GEO_ANSWER_OIUR_ENABLED`) |
| Context envelope (Field/Office mode) | Live |
| Intent classifier (8 intents) | Live; `project_summary`/`coverage_gap` extractors **partial** (ADR-0007 PR-2) |
| Inline chat cards | Partial — schema ready; 5 card extractors partially shipped |

## Frontend pages

| Page | Status |
|---|---|
| Login, ForgotPassword, Onboarding | Live |
| Projects, NewProject, Overview, Portfolio | Live |
| Lakehouse | Live |
| DrillReview | Live (CC-01 Item 1 landed 2026-05-24) |
| DrillholeDetail | Live |
| HoleCompare | Live |
| IngestQuality | Live |
| IngestionRuns | Partial (Phase A live; Phase B uses `silver.ingest_progress` step writes — planned) |
| Chat | Live (OIUR cards live; ADR-0007 inline cards partial) |
| Investigations, Hypothesis, Decisions, Rationale, Reasoning | Live |
| Targets, TargetRecommendation | Live |
| SourceGraph | Live |
| Sources, Corpus | Live (data-hierarchy facets **planned**, Ch 13) |
| AuditLog | Live |
| Workspace (3D) | Live (9 sub-views as of 2026-05-25) |
| ProjectAnalytics | Live |
| RetrievalInspector | Live (dev) |
| Reporting, Report, ReportView | Partial — report builder UI live; persistent report binding **planned** |
| SavedMapViews | Live |
| SupportCockpit | Live |
| Settings | Live |
| Tier3Unlock | Live (gates via `usage.workspace_cost_quotas`) |
| WhatChangedFeed | Live |
| Inbox | Live |
| AssessmentSummary | Live |
| ChartsGallery | Live |
| InterpretationWorkspace | Partial |
| SearchQuery, Explorer | Live |
| DataImportWizard | Live |
| EvidenceQuality dashboard | Live |
| LlmCost dashboard | Live |
| PublicGeoOverlay dashboard | Live |
| Reporting dashboard | Live |
| VisualReadiness dashboard | Live |

## Agents (Pydantic-AI / Phase 0+5+)

| Agent | Status |
|---|---|
| Index Health (`phase0_agents`) | Live |
| Storage Tiering | Live (dev-only) |
| Store Reconciliation | Live |
| Support Packet | Live |
| LLM Incident Diagnosis | Experimental |
| Cost Burn Watcher | Live |
| Anomaly Detector (tool) | Live |
| Drill Targeting (tool) | Live |
| Decomposer, Anaphora, Followups | Live |
| Escalation, Agentic Escalation | Live |
| Confidence Computer | Live |

## Feature flags currently in play

| Flag | Default | Effect |
|---|---|---|
| `AGENTIC_RETRIEVAL_V2_ENABLED` | false (dev: true) | Use §04j LangGraph instead of legacy linear RAG |
| `GEO_ANSWER_OIUR_ENABLED` | false (dev: true) | Wrap answers in OIUR envelope |
| `PDF_PARSER_DOCLING_ENABLED` | true | Docling primary OCR engine |
| `DOCLING_OCR_ENABLED` | true | rapidocr backend on (GPU) |
| `PDF_PARSER_TESSERACT_FALLBACK_ENABLED` | true | Fall back to tesseract on docling failure |
| `P04P_DUAL_WRITE_ENABLED` | false | Run legacy parser in parallel for A/B |
| `CITATION_SPAN_RESOLVER_ENABLED` | false | Enable inline citation span resolver |
| `LLM_BACKEND` | `vllm` | `vllm` / `anthropic`; `anthropic` requires explicit profile gate (Appendix C) |
| `LLM_BACKEND_FALLBACK` | `downshift` | Cross-backend failover policy |
| `LLM_FALLBACK_ENABLED` | false | Enable cross-backend failover |

## Known security items (tracked, not closed)

See appendix C. Summary:

1. **`georag` Postgres role is SUPERUSER + BYPASSRLS** — operationally
   mitigated, structural fix tracked (Ch 02 §1.1).
2. **Martin uses `georag_app`** — should be `martin_readonly` (Ch 02 §1.2).
3. **Qdrant auth off in dev** — must be required in prod (Ch 02 §3).
4. **Anthropic / external-LLM data egress** — must be profile-gated;
   currently always available behind env (Ch 06).
5. **`persist_node` is best-effort** — answers can complete without an
   audit row (Ch 06 §2.1).
6. **`init-roles.sql` is outside the auto-init dir** — fresh clusters
   miss the read/write/audit roles (Ch 02).
7. **`docker commit` CMD trap** — explicit `command:` on every container
   protects against it, but a regression would silently swap entrypoints.
