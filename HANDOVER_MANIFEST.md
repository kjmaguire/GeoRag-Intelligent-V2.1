# HANDOVER_MANIFEST.md

> PASS 0 mechanical inventory. Raw enumeration of the live tree.
> No prose, no judgment. Source-of-truth manifest for PASS 1 mapping + COVERAGE.md.

---

## 1. FastAPI routes (src/fastapi/app/routers/)

Total: 109 endpoints across 32 router files.

| Method | Path | Auth | Handler |
|---|---|---|---|
| DELETE | `/v1/interpretation/notes/{note_id}` | service_key | `interpretation.py:235` |
| DELETE | `/v1/interpretation/section-lines/{section_id}` | service_key | `interpretation.py:335` |
| DELETE | `/v1/interpretation/target-zones/{zone_id}` | service_key | `interpretation.py:491` |
| GET | `/api/v1/admin/alerts-inbox` | service_key | `admin_tier234.py:693` |
| GET | `/api/v1/admin/audit-explorer/search` | service_key | `admin_tier234.py:452` |
| GET | `/api/v1/admin/audit-explorer/verify-chain` | service_key | `admin_tier234.py:539` |
| GET | `/api/v1/admin/audit/boundary-violations` | service_key | `audit_findings.py:287` |
| GET | `/api/v1/admin/audit/cold-tier-archive-runs` | service_key | `audit_findings.py:203` |
| GET | `/api/v1/admin/audit/tenant-isolation-findings` | service_key | `audit_findings.py:107` |
| GET | `/api/v1/admin/backups/cold-tier-runs` | service_key | `admin_tier234.py:1173` |
| GET | `/api/v1/admin/backups/snapshot-runs` | service_key | `admin_tier234.py:1030` |
| GET | `/api/v1/admin/backups/workspace-consistency/{workspace_id}` | service_key | `admin_tier234.py:1146` |
| GET | `/api/v1/admin/conflicts/recent` | service_key | `conflicts.py:76` |
| GET | `/api/v1/admin/eval/questions` | service_key | `admin_tier234.py:1533` |
| GET | `/api/v1/admin/eval/questions/{question_id}` | service_key | `admin_tier234.py:1585` |
| GET | `/api/v1/admin/eval/runs` | service_key | `admin_tier234.py:1311` |
| GET | `/api/v1/admin/eval/runs/{run_id}/per-set-summary` | service_key | `admin_tier234.py:1395` |
| GET | `/api/v1/admin/export-gate/results` | service_key | `admin_tier1_misc.py:136` |
| GET | `/api/v1/admin/ml/training-runs` | service_key | `ml_training.py:96` |
| GET | `/api/v1/admin/qp-credentials` | service_key | `admin_tier234.py:110` |
| GET | `/api/v1/admin/reports/builds` | service_key | `report_builder.py:228` |
| GET | `/api/v1/admin/reports/builds/{build_id}` | service_key | `report_builder.py:331` |
| GET | `/api/v1/admin/reports/builds/{build_id}/sections/{section_id}/history` | service_key | `report_builder.py:492` |
| GET | `/api/v1/admin/reports/types` | service_key | `report_builder.py:164` |
| GET | `/api/v1/admin/saved-maps` | service_key | `admin_tier234.py:607` |
| GET | `/api/v1/admin/source-trust/scores` | service_key | `admin_tier1_misc.py:52` |
| GET | `/api/v1/admin/target_recommendation/runs` | service_key | `target_recommendation_cockpit.py:318` |
| GET | `/api/v1/admin/target_recommendation/runs/{run_id}` | service_key | `target_recommendation_cockpit.py:212` |
| GET | `/api/v1/admin/target_recommendation/runs/{run_id}/geojson` | service_key | `target_recommendation_cockpit.py:259` |
| GET | `/api/v1/admin/what-changed/runs` | service_key | `what_changed.py:46` |
| GET | `/api/v1/admin/workspace-members` | service_key | `admin_tier234.py:253` |
| GET | `/api/v1/admin/workspace-settings/{workspace_id}` | service_key | `admin_tier234.py:345` |
| GET | `/assessment_summary/{pdf_id}` | service_key | `assessment_summary.py:148` |
| GET | `/completeness_audit/{pdf_id}/latest` | service_key | `completeness.py:114` |
| GET | `/coverage/density` | service_key | `coverage.py:72` |
| GET | `/internal/v1/integrations/flows` | - | `integrations_trigger.py:104` |
| GET | `/internal/v1/ocr/render` | - | `ocr_render.py:127` |
| GET | `/pdf/extract_text` | service_key | `pdf.py:372` |
| GET | `/pdf/find_coordinates` | service_key | `pdf.py:1220` |
| GET | `/pdf/find_legends` | service_key | `pdf.py:562` |
| GET | `/pdf/find_tables` | service_key | `pdf.py:438` |
| GET | `/pdf/summarize_section` | service_key | `pdf.py:947` |
| GET | `/projects/{project_id}` | service_key | `projects.py:98` |
| GET | `/projects/{project_id}/collars` | service_key | `projects.py:167` |
| GET | `/public-geo/smdi/features` | service_key | `smdi.py:115` |
| GET | `/v1/answer_runs/{answer_run_id}/events` | service_key | `answer_runs.py:133` |
| GET | `/v1/answer_runs/{answer_run_id}/lineage` | service_key | `answer_runs.py:548` |
| GET | `/v1/answer_runs/{answer_run_id}/trust-summary` | service_key | `answer_runs.py:333` |
| GET | `/v1/evidence/{evidence_id}` | service_key | `evidence.py:581` |
| GET | `/v1/interpretation/comments` | service_key | `interpretation.py:509` |
| GET | `/v1/interpretation/notes` | service_key | `interpretation.py:146` |
| GET | `/v1/interpretation/section-lines` | service_key | `interpretation.py:253` |
| GET | `/v1/interpretation/target-zones` | service_key | `interpretation.py:353` |
| GET | `/v1/viz/chart-kinds` | service_key | `visualizations.py:766` |
| GET | `/v1/viz/cross_section` | service_key | `visualizations.py:328` |
| GET | `/v1/viz/stereonet` | service_key | `visualizations.py:460` |
| GET | `/v1/viz/strip_log` | service_key | `visualizations.py:192` |
| POST | `/api/v1/admin/alerts-inbox/acknowledge` | service_key | `admin_tier234.py:832` |
| POST | `/api/v1/admin/audit/cold-tier-archive` | service_key | `audit_findings.py:247` |
| POST | `/api/v1/admin/conflicts/run` | service_key | `conflicts.py:124` |
| POST | `/api/v1/admin/eval/assess-promotion` | service_key | `admin_tier234.py:1252` |
| POST | `/api/v1/admin/eval/questions` | service_key | `admin_tier234.py:1629` |
| POST | `/api/v1/admin/eval/questions/{question_id}/dry-run` | service_key | `admin_tier234.py:1806` |
| POST | `/api/v1/admin/eval/questions/{question_id}/transition` | service_key | `admin_tier234.py:1722` |
| POST | `/api/v1/admin/ml/train-source-trust` | service_key | `ml_training.py:161` |
| POST | `/api/v1/admin/ml/train-target-model` | service_key | `ml_training.py:148` |
| POST | `/api/v1/admin/qp-credentials` | service_key | `admin_tier234.py:163` |
| POST | `/api/v1/admin/qp-credentials/{qp_credential_id}/verify` | service_key | `admin_tier234.py:211` |
| POST | `/api/v1/admin/recommendations/analogue` | service_key | `admin_tier234.py:71` |
| POST | `/api/v1/admin/recommendations/nbd` | service_key | `admin_tier234.py:52` |
| POST | `/api/v1/admin/reports/build` | service_key | `report_builder.py:176` |
| POST | `/api/v1/admin/reports/export` | service_key | `report_builder.py:286` |
| POST | `/api/v1/admin/support/agents/customer-response-draft` | service_key | `support_agents.py:165` |
| POST | `/api/v1/admin/support/agents/escalation-routing` | service_key | `support_agents.py:187` |
| POST | `/api/v1/admin/support/agents/root-cause-investigation` | service_key | `support_agents.py:145` |
| POST | `/api/v1/admin/support/agents/support-packet` | service_key | `support_agents.py:127` |
| POST | `/api/v1/admin/support/agents/ticket-triage` | service_key | `support_agents.py:114` |
| POST | `/api/v1/admin/target_recommendation/runs/{run_id}/signoff` | service_key | `target_recommendation_cockpit.py:393` |
| POST | `/api/v1/citations/feedback` | service_key | `citation_feedback.py:64` |
| POST | `/api/v1/incidents/diagnose` | service_key | `phase0_ops.py:71` |
| POST | `/api/v1/support/packets/assemble` | service_key | `phase0_ops.py:119` |
| POST | `/assessment_summary/{pdf_id}` | service_key | `assessment_summary.py:107` |
| POST | `/completeness_audit/{pdf_id}` | service_key | `completeness.py:67` |
| POST | `/internal/exports/geopackage` | - | `exports.py:95` |
| POST | `/internal/exports/shapefile` | - | `exports.py:61` |
| POST | `/internal/v1/integrations/{flow_name}/trigger` | - | `integrations_trigger.py:116` |
| POST | `/internal/v1/metrics/ingestion-event` | - | `metrics_ingestion_events.py:53` |
| POST | `/internal/v1/mv-refresh/run` | - | `mv_refresh_trigger.py:76` |
| POST | `/internal/v1/re_ocr_page/trigger` | - | `re_ocr_trigger.py:45` |
| POST | `/internal/v1/shadow/ingest_pdf/trigger` | - | `shadow_trigger.py:51` |
| POST | `/internal/v1/shadow/tiff_normalize/trigger` | - | `shadow_trigger.py:79` |
| POST | `/maps/ingest` | service_key | `maps.py:137` |
| POST | `/outlier-assist` | - | `outlier_assist.py:129` |
| POST | `/pdf/crop_region` | service_key | `pdf.py:655` |
| POST | `/pdf/ocr_region` | service_key | `pdf.py:816` |
| POST | `/pdf/render_page` | service_key | `pdf.py:221` |
| POST | `/queries` | service_key | `queries.py:590` |
| POST | `/v1/answer_runs/{answer_run_id}/feedback` | service_key | `answer_runs.py:207` |
| POST | `/v1/interpretation/comments` | service_key | `interpretation.py:548` |
| POST | `/v1/interpretation/notes` | service_key | `interpretation.py:193` |
| POST | `/v1/interpretation/section-lines` | service_key | `interpretation.py:296` |
| POST | `/v1/interpretation/target-zones` | service_key | `interpretation.py:401` |
| POST | `/v1/interpretation/target-zones/{zone_id}/accept` | service_key | `interpretation.py:447` |
| POST | `/v1/viz/chart` | service_key | `visualizations.py:771` |
| POST | `/v1/viz/qa` | service_key | `visualizations.py:1004` |
| POST | `/v1/viz/readiness` | service_key | `visualizations.py:1051` |
| PUT | `/api/v1/admin/eval/questions/{question_id}` | service_key | `admin_tier234.py:1666` |
| PUT | `/api/v1/admin/reports/builds/{build_id}/sections/{section_id}` | service_key | `report_builder.py:411` |
| PUT | `/api/v1/admin/workspace-settings/{workspace_id}` | service_key | `admin_tier234.py:379` |

### 1a. Router-prefix map

| File | Router var | Prefix |
|---|---|---|
| `admin_tier1_misc.py` | `source_trust_router` | `/api/v1/admin/source-trust` |
| `admin_tier1_misc.py` | `export_gate_router` | `/api/v1/admin/export-gate` |
| `admin_tier234.py` | `rec_router` | `/api/v1/admin/recommendations` |
| `admin_tier234.py` | `qp_router` | `/api/v1/admin/qp-credentials` |
| `admin_tier234.py` | `ws_members_router` | `/api/v1/admin/workspace-members` |
| `admin_tier234.py` | `ws_settings_router` | `/api/v1/admin/workspace-settings` |
| `admin_tier234.py` | `audit_explorer_router` | `/api/v1/admin/audit-explorer` |
| `admin_tier234.py` | `saved_maps_router` | `/api/v1/admin/saved-maps` |
| `admin_tier234.py` | `alerts_router` | `/api/v1/admin/alerts-inbox` |
| `admin_tier234.py` | `backups_router` | `/api/v1/admin/backups` |
| `admin_tier234.py` | `eval_promotion_router` | `/api/v1/admin/eval` |
| `admin_tier234.py` | `eval_questions_router` | `/api/v1/admin/eval/questions` |
| `answer_runs.py` | `router` | `/v1/answer_runs` |
| `assessment_summary.py` | `router` | `/assessment_summary` |
| `audit_findings.py` | `router` | `/api/v1/admin/audit` |
| `citation_feedback.py` | `router` | `/api/v1/citations` |
| `completeness.py` | `router` | `/completeness_audit` |
| `conflicts.py` | `router` | `/api/v1/admin/conflicts` |
| `coverage.py` | `router` | `/coverage` |
| `evidence.py` | `router` | `/v1/evidence` |
| `exports.py` | `router` | `/internal/exports` |
| `integrations_trigger.py` | `router` | `/internal/v1/integrations` |
| `interpretation.py` | `router` | `/v1/interpretation` |
| `maps.py` | `router` | `/maps` |
| `metrics_ingestion_events.py` | `router` | `/internal/v1/metrics` |
| `ml_training.py` | `router` | `/api/v1/admin/ml` |
| `mv_refresh_trigger.py` | `router` | `/internal/v1/mv-refresh` |
| `ocr_render.py` | `router` | `/internal/v1/ocr` |
| `outlier_assist.py` | `router` | `/outlier-assist` |
| `pdf.py` | `router` | `/pdf` |
| `phase0_ops.py` | `router` | `/api/v1` |
| `projects.py` | `router` | `(prefixed in main.py include_router)` |
| `queries.py` | `router` | `(prefixed in main.py include_router)` |
| `re_ocr_trigger.py` | `router` | `/internal/v1/re_ocr_page` |
| `report_builder.py` | `router` | `/api/v1/admin/reports` |
| `shadow_trigger.py` | `router` | `/internal/v1/shadow` |
| `smdi.py` | `router` | `/public-geo/smdi` |
| `support_agents.py` | `router` | `/api/v1/admin/support` |
| `target_recommendation_cockpit.py` | `router` | `/api/v1/admin/target_recommendation` |
| `visualizations.py` | `router` | `/v1/viz` |
| `what_changed.py` | `router` | `/api/v1/admin/what-changed` |

### 1b. main.py include_router prefix overrides

Routers mounted at `/internal` in `src/fastapi/app/main.py`:
- `queries.router`
- `projects.router`
- `exports_router.router` (router itself also declares `prefix='/internal/exports'`)
- `outlier_assist_router.router`

All other routers mounted at root and use their own declared prefix.
---

## 2. Pydantic AI agents (@georag_agent decorated)

Total: 42 agents across 7 phase subdirs in `src/fastapi/app/agents/`.

| Phase | Agent | Risk tier | Version |
|---|---|---|---|
| phase0 | `graph_tenant_auditor` | R0 | 0.1.0 |
| phase0 | `index_health` | R0 | 0.1.0 |
| phase0 | `lineage_reporter` | R0 | 0.1.0 |
| phase0 | `llm_incident_diagnosis` | R0 | 0.1.0 |
| phase0 | `model_cost_summary` | R0 | 0.1.0 |
| phase0 | `model_upgrade_watch` | R0 | 0.1.0 |
| phase0 | `storage_tiering` | R2 | 0.1.0 |
| phase0 | `store_reconciliation` | R0 | 0.1.0 |
| phase0 | `support_packet` | R2 | 0.1.0 |
| phase0 | `tenant_isolation_auditor` | R0 | 0.1.0 |
| phase0 | `vllm_security_check` | R0 | 0.1.0 |
| phase5 | `drillhole_visual_qa` | R1 | 1.0.0 |
| phase5 | `visual_readiness` | R1 | 1.0.0 |
| phase6 | `public_private_boundary` | R2 | 1.0.0 |
| phase7 | `appendix_builder` | R2 | 1.0.0 |
| phase7 | `claim_validator` | R1 | 1.0.0 |
| phase7 | `conflict_resolver` | R1 | - |
| phase7 | `evidence_curator` | R1 | 1.0.0 |
| phase7 | `export_compliance` | R3 | 1.0.0 |
| phase7 | `map_chart_planner` | R2 | 1.0.0 |
| phase7 | `presentation_coach` | R1 | 1.0.0 |
| phase7 | `report_planner` | R1 | 1.0.0 |
| phase8 | `backtesting` | R2 | 1.0.0 |
| phase8 | `candidate_generation` | R2 | 1.0.0 |
| phase8 | `constraint` | R1 | 1.0.0 |
| phase8 | `deposit_model` | R1 | 1.0.0 |
| phase8 | `evidence_layer` | R1 | 1.0.0 |
| phase8 | `field_outcome` | R2 | 1.0.0 |
| phase8 | `geologist_signoff` | R5 | 1.0.0 |
| phase8 | `recommendation_explainer` | R2 | 1.0.0 |
| phase8 | `scenario_planning` | R1 | 1.0.0 |
| phase8 | `target_scoring` | R2 | 1.0.0 |
| phase8 | `uncertainty` | R2 | 1.0.0 |
| phase9 | `analogue_finder` | R1 | 1.0.0 |
| phase9 | `hypothesis_generator` | R2 | 0.2.0 |
| phase9 | `next_best_data` | R1 | 1.0.0 |
| phase9 | `spatial_relationship` | R1 | 1.0.0 |
| phase10 | `customer_response_drafting` | R1 | 0.2.0 |
| phase10 | `escalation_routing` | R2 | 0.2.0 |
| phase10 | `root_cause_investigation` | R1 | 0.2.0 |
| phase10 | `support_packet` | R2 | 0.2.0 |
| phase10 | `ticket_triage` | R1 | 0.2.0 |

Decorator contract: `src/fastapi/app/agents/wrapper.py::georag_agent(name, risk_tier, version)`.
Risk tier policy enum: `src/fastapi/app/services/tool_gateway/policies.py::RiskTier` (R0..R5).

---

## 3. LangGraph subgraphs

`StateGraph` instantiations:

| File | State class |
|---|---|
| `src/fastapi/app/agent/agentic_retrieval/graph.py` | `AgenticRetrievalState` |
| `src/fastapi/app/services/report_builder/graph.py` | `ReportBuilderState` |
| `src/fastapi/app/services/target_recommendation/graph.py` | `TargetRecommendationState` |

Intent labels (`src/fastapi/app/agent/agentic_retrieval/intent_classifier.py`):
- `factual_lookup`
- `synthesis`
- `hypothesis_generation`
- `anomaly_detection`
- `uncertainty_quantification`
- `decision_support`
- `project_summary`
- `coverage_gap`

---

## 4. Hatchet workflows (src/fastapi/app/hatchet_workflows/)

### 4a. Worker pool: `ingestion` (compose svc: hatchet-worker-ingestion)

Core workflows:
- `outbox_dispatcher`
- `ingest_pdf`
- `re_ocr_page`
- `ocr_quality_check_wf`
- `tiff_ocr_cluster`
- `tiff_normalize`
- `stale_run_detector`
- `nightly_ingestion_integrity`
- `reliability_metrics_publisher`

Plus `INGESTION_AGENT_WORKFLOWS` (phase0 agents wrapped as workflows):
- `storage_tiering_run`
- `index_health_check`
- `store_reconciliation_run`

### 4b. Worker pool: `ai` (compose svc: hatchet-worker-ai)

Core workflows:
- `audit_ledger_verify`
- `repair_shadow_aggregate`
- `phase2_smoke`
- `public_geoscience_pull`
- `external_notification`
- `flow_jwt_key_reaper`
- `idempotency_keys_cleanup`
- `mv_refresh_silver`
- `generate_report`
- `score_targets`
- `field_outcome_learning`
- `what_changed_detector`
- `what_changed_weekly`
- `evaluate_workspace`
- `eval_real_rag_nightly`
- `sync_silver_to_kg`
- `embed_pending_passages_wf`
- `support_replay`
- `restore_workspace`
- `train_target_model`
- `train_source_trust`
- `continuous_learning_loop`
- `cold_tier_archive_workflow`
- `cost_burn_watcher`
- `backup_neo4j`
- `backup_postgres`
- `backup_qdrant`
- `backup_redis`
- `backup_seaweedfs`
- `shadow_diff`
- `workspace_export`

Plus `AI_AGENT_WORKFLOWS` (phase0 agents wrapped as workflows):
- `tenant_isolation_audit`
- `graph_tenant_audit`
- `lineage_walk`
- `model_upgrade_watch_run`
- `vllm_security_check_run`
- `model_cost_summary_run`
- `llm_incident_diagnosis_run`
- `support_packet_assemble`

Total workflow files: 43 (excludes __init__, worker, _progress, phase0_agents)

### 4c. Hatchet cron schedules (on_crons declaration site)

| Workflow | Cron expression |
|---|---|
| `audit_ledger_verify` | `0 2 * * *` |
| `backup_postgres` | `0 2 * * *` |
| `backup_qdrant` | `30 2 * * *` |
| `backup_redis` | `45 2 * * *` |
| `backup_seaweedfs` | `0 3 * * *` |
| `cold_tier_archive` | `0 4 * * *` |
| `cost_burn_watcher` | `*/5 * * * *` |
| `embed_pending_passages` | `45 5 * * *` |
| `embed_pending_passages` | `*/10 * * * *` |
| `eval_real_rag_nightly` | `15 5 * * *` |
| `flow_jwt_key_reaper` | `0 4 * * *` |
| `idempotency_keys_cleanup` | `15 4 * * *` |
| `mv_refresh_silver` | `0 3 * * *` |
| `nightly_ingestion_integrity` | `0 2 * * *` |
| `nightly_ingestion_integrity` | `0 4 * * *` |
| `outbox_dispatcher` | `* * * * *` |
| `reliability_metrics_publisher` | `* * * * *` |
| `repair_shadow_aggregate` | `15 2 * * *` |
| `shadow_diff` | `* * * * *` |
| `stale_run_detector` | `*/15 * * * *` |
| `sync_silver_to_kg` | `30 5 * * *` |
| `what_changed_weekly` | `0 6 * * 1` |
| `shadow_diff` | `* * * * *` |

Total cron-triggered workflow declarations: see table above.
---

## 5. Dagster assets (src/dagster/georag_dagster/assets/)

Total: 53 top-level asset module files (excludes helpers/init).

**Top-level asset files:**
- `bronze.py`
- `bronze_geophysics.py`
- `bronze_lithology.py`
- `bronze_public_geoscience.py`
- `bronze_reports.py`
- `bronze_samples.py`
- `bronze_seismic.py`
- `bronze_spatial.py`
- `bronze_surveys.py`
- `bronze_well_logs.py`
- `bronze_xlsx.py`
- `bronze_xyz.py`
- `commit_ingestion_run.py`
- `data_dictionary_dump.py`
- `gold_cross_corpus_linker.py`
- `gold_cross_section_panels.py`
- `gold_drillhole_intervals_visual.py`
- `gold_h3_density.py`
- `gold_public_geoscience.py`
- `gold_structure_measurements_visual.py`
- `index_document_passages.py`
- `index_neo4j.py`
- `index_public_geoscience.py`
- `index_reports.py`
- `reranker_labels.py`
- `silver.py`
- `silver_assay_dq.py`
- `silver_cog_rasters.py`
- `silver_collar_dq.py`
- `silver_collars_canonicalize_backfill.py`
- `silver_crs_dq.py`
- `silver_drill_traces.py`
- `silver_entity_ner_backfill.py`
- `silver_geochronology.py`
- `silver_geophysics.py`
- `silver_lithology.py`
- `silver_nl_summaries.py`
- `silver_public_geoscience.py`
- `silver_raster.py`
- `silver_reports.py`
- `silver_samples.py`
- `silver_samples_nl_summary.py`
- `silver_seismic.py`
- `silver_spatial.py`
- `silver_structure_derive.py`
- `silver_structure_populate.py`
- `silver_surveys.py`
- `silver_unit_consistency_dq.py`
- `silver_well_logs.py`
- `silver_xlsx.py`
- `silver_xyz.py`
- `smdi_deposits.py`
- `sparse_encoder.py`

**`bronze_to_silver/` subdir:**
- `bronze_to_silver/assays.py`
- `bronze_to_silver/lithology.py`
- `bronze_to_silver/qaqc.py`

### 5a. Dagster schedules + sensors (src/dagster/georag_dagster/definitions.py)

**Schedules:**

| Name | Cron |
|---|---|
| `full_ingest_schedule` | `0 2 * * *` |
| `public_geoscience_weekly_refresh` | `0 3 * * 0` |
| `smdi_deposits_daily_refresh` | `30 3 * * *` |
| `silver_dq_daily_schedule` | `0 4 * * *` |
| `silver_chat_cards_backfill_schedule` | `*/30 * * * *` |
| `public_geoscience_daily_edit_check` | `30 5 * * *` |

**Sensor:** `minio_upload_sensor`

### 5b. Dagster asset checks (src/dagster/georag_dagster/checks/)

| File | @asset_check count |
|---|---|
| `drill_traces_checks.py` | 1 |
| `evidence_checks.py` | 5 |
| `index_checks.py` | 2 |
| `interval_overlap_checks.py` | 2 |
| `silver_checks.py` | 16 |
| **Total** | **26** |

---

## 6. Kestra flows (kestra/flows/georag/)

### `external_notification.yaml`

- Triggers: io.kestra.plugin.core.trigger.Webhook

### `public_geoscience_pull.yaml`

- Triggers: io.kestra.plugin.core.trigger.Schedule
- Cron: `0 */6 * * *`

### `support_packet_dispatch.yaml`

- Triggers: io.kestra.plugin.core.trigger.Webhook
---

## 7. Data stores

### 7a. PostgreSQL — tables by schema

Total tables created across migrations + raw SQL + init scripts: **166**.

| Schema | Table count | Tables |
|---|---|---|
| `audit` | 4 | `audit_ledger`, `audit_ledger_chain_fork_quarantine`, `audit_ledger_verification_runs`, `integration_credentials_audit` |
| `backups` | 1 | `snapshot_runs` |
| `bronze` | 11 | `ingest_manifest`, `ingest_runs`, `ingest_triage_samples`, `provenance`, `raw_assay_submissions`, `raw_collar_entries`, `raw_geophysical_runs`, `raw_lithology_logs`, `raw_qaqc_submissions`, `raw_surveys`, `source_files` |
| `eval` | 3 | `golden_questions`, `run_results`, `run_summaries` |
| `gold` | 11 | `assay_composites`, `campaign_summaries`, `cross_section_panels`, `drill_summaries`, `drillhole_intervals_visual`, `element_correlations`, `h`, `qaqc_statistics`, `significant_intersections`, `structure_measurements_visual`, `zone_statistics` |
| `interpretation` | 4 | `interpretation_comments`, `interpretation_notes`, `interpretation_section_lines`, `interpretation_target_zones` |
| `ops` | 3 | `support_replay_runs`, `support_ticket_traces`, `support_tickets` |
| `outbox` | 2 | `pending_propagations`, `propagation_attempts` |
| `public_geo` | 21 | `commodity_aliases`, `document_entity_links`, `jurisdictions`, `pg_assessment_survey`, `pg_assessment_survey_history`, `pg_bedrock_geology`, `pg_bedrock_geology_history`, `pg_drillhole_collar`, `pg_drillhole_collar_history`, `pg_mine`, `pg_mine_history`, `pg_mineral_disposition`, `pg_mineral_disposition_history`, `pg_mineral_occurrence`, `pg_mineral_occurrence_history`, `pg_resource_potential_zone`, `pg_resource_potential_zone_history`, `pg_rock_sample`, `pg_rock_sample_history`, `sources`, `status_aliases` |
| `silver` | 73 | `alias_gaps`, `alteration`, `answer_citation_items`, `answer_citation_spans`, `answer_retrieval_items`, `answer_runs`, `assay_samples`, `assays`, `assays_v`, `campaigns`, `claim_ledger`, `collab_anchors`, `collab_comments`, `corpus_health_findings`, `data_quality_flags`, `decision_evidence_links`, `decision_lessons_learned`, `decision_options`, `decision_outcomes`, `decision_records`, `document_ingestion_quality`, `document_passages`, `document_revisions`, `document_versions`, `downhole_geophysics`, `drill_traces`, `element_reference`, `entity_aliases`, `evidence_items`, `geological_formations`, `geological_ontology_synonyms`, `geological_ontology_terms`, `geophysics_surveys`, `geotechnical`, `historic_workings`, `hypotheses`, `hypothesis_evidence_links`, `ingest_extractions`, `ingest_layouts`, `ingest_ocr_results`, `lithology`, `low_confidence_page_reviews`, `message_feedback`, `mineralization`, `ocr_page_quality`, `parser_run_artifacts`, `project_boundaries`, `qaqc_results`, `qp_credentials`, `query_traces`, `raster_layers`, `recovery`, `rock_codes`, `sample_dispatches`, `sample_intervals`, `saved_map_views`, `section_lines`, `seismic_surveys`, `shadow_runs`, `source_trust_features`, `source_trust_scores`, `specific_gravity`, `storage_tier_policy`, `store_reconciliation_findings`, `structure`, `structure_measurements`, `structured_record_lineage`, `support_packets`, `table_extraction_quality`, `target_rationales`, `tier`, `workspace_settings`, `workspaces` |
| `targeting` | 10 | `target_backtests`, `target_candidate_zones`, `target_model_versions`, `target_models`, `target_outcomes`, `target_recommendations`, `target_review_decisions`, `target_score_factors`, `target_scores`, `target_uncertainties` |
| `usage` | 4 | `external_notification_senders`, `usage_aggregates_daily`, `usage_events`, `workspace_cost_ceilings` |
| `workflow` | 5 | `activepieces_channels`, `flow_jwt_keys`, `flow_registry`, `workflow_run_events`, `workflow_runs` |
| `workspace` | 14 | `agent_permissions`, `agent_prompt_pins`, `agent_risk_tiers`, `agent_timeouts`, `approval_requirements`, `dry_run_outputs`, `feature_flag_history`, `feature_flags`, `idempotency_keys`, `prompt_versions`, `tool_invocations`, `workspace_agent_config`, `workspace_memberships`, `workspace_roles` |

**Co-tenant databases in the cluster:** `georag` (main), `hatchet` (engine state), `georag_dagster` (asset events).

### 7b. PostgreSQL functions (CREATE OR REPLACE FUNCTION)

Total: 32.

- `audit.compute_audit_hash`
- `audit.recompute_hash`
- `audit.run_verification`
- `audit.verify_hash_chain`
- `public_geo.pg_assessment_surveys_tiles`
- `public_geo.pg_bedrock_geology_tiles`
- `public_geo.pg_drillhole_collars_tiles`
- `public_geo.pg_mineral_dispositions_tiles`
- `public_geo.pg_mineral_occurrences_tiles`
- `public_geo.pg_mines_tiles`
- `public_geo.pg_resource_potential_tiles`
- `public_geo.pg_rock_samples_tiles`
- `silver.enforce_data_version_monotonic`
- `silver.fn_set_updated_at`
- `silver.pg_boundaries_by_project`
- `silver.pg_collars_by_project`
- `silver.pg_cross_section_lines_by_project`
- `silver.pg_drill_traces_by_project`
- `silver.pg_formations_by_project`
- `silver.pg_geochem_by_project`
- `silver.pg_historic_workings_by_project`
- `silver.pg_seismic_by_project`
- `silver.significant_intersections_by_project`
- `usage.lookup_external_notification_sender_secrets`
- `usage.register_external_notification_sender`
- `workflow.flow_registry_touch_updated_at`
- `workflow.get_flow_jwt_keys`
- `workflow.get_flow_jwt_secret`
- `workflow.reap_expired_flow_jwt_keys`
- `workflow.refresh_silver_agent_mvs`
- `workflow.set_flow_jwt_secret`
- `workspace.feature_flags_audit`

### 7c. PostgreSQL triggers (CREATE TRIGGER)

Total: 6.

- `audit_ledger_compute_hash_trg`
- `feature_flags_audit_trg`
- `flow_registry_touch_updated_at`
- `projects_data_version_monotonic`
- `set_updated_at`
- `workspaces_data_version_monotonic`

### 7d. PostgreSQL materialized views (CREATE MATERIALIZED VIEW)

- `silver.mv_collar_summary`

### 7e. PostgreSQL extensions (CREATE EXTENSION)

- `auto_explain`
- `h`
- `hypopg`
- `pg_ivm`
- `pg_partman`
- `pg_repack`
- `pg_stat_kcache`
- `pg_stat_statements`
- `pg_trgm`
- `pgcrypto`
- `postgis`
- `postgis_raster`
- `postgis_topology`
- `uuid-ossp`

`shared_preload_libraries` (compose `-c`): `pg_stat_statements,auto_explain,pg_stat_kcache`

### 7f. Neo4j (knowledge graph)

**Node labels (from `docker/neo4j/init-schema.cypher`):**
- `:Project`
- `:DrillHole`
- `:Formation`
- `:GeophysicalSurvey`
- `:MineralOccurrence`
- `:Publication`
- `:Report`

**Auxiliary labels (`docker/neo4j/warmup.cypher` — public-geoscience):**
- `:PublicGeo`
- `:PublicGeoSource`
- `:Jurisdiction`

**Relationship types (warmup.cypher):**
- `:HAS_HOLE`
- `:HAS_LITHOLOGY`
- `:HAS_SURVEY`
- `:REFERENCES_FORMATION`
- `:HOSTS_MINERALIZATION`
- `:INTERSECTED_BY_HOLE`
- `:CITES_DATA_FROM`
- `:CITES_DRILLHOLE`
- `:COVERS_AREA_FOR`
- `:REFERENCES`
- `:SOURCED_FROM`
- `:PUBLISHED_BY`

Constraints: 5 uniqueness (`project_name`, `drillhole_hole_id`, `formation_name`, `report_title`, `publication_title`).
Indexes: 9 RANGE (`project_commodity`, `project_region`, `drillhole_type`, `formation_age`, `geophysical_survey_date`, `geophysical_survey_type`, `mineral_occurrence_commodity`, `mineral_occurrence_deposit_type`, `publication_year`, `report_date`).
APOC plugin: enabled (`NEO4J_PLUGINS='["apoc"]'`).

### 7g. Qdrant — collections

**Primary indexes:**
- `georag_chunks` — canonical chunked corpus (384-dim COSINE dense + sparse `text` slot, ADR-0010)
- `georag_reports` — legacy reports collection

**Public-geoscience collections (`src/fastapi/app/agent/public_geoscience_tool.py::_COLLECTION_FOR_TYPE`):**
- `pg_mine` (canonical_type=mine)
- `pg_mineral_occurrence` (canonical_type=mineral_occurrence)
- `pg_drillhole_collar` (canonical_type=drillhole_collar)
- `pg_resource_potential_zone` (canonical_type=resource_potential_zone)
- `pg_rock_sample` (canonical_type=rock_sample)
- `pg_assessment_survey` (canonical_type=assessment_survey)
- `pg_mineral_disposition` (canonical_type=mineral_disposition)

**`georag_chunks` payload indices (`assets/index_document_passages.py`):**
- Keyword: `workspace_id`, `document_id`, `chunk_kind`, `ocr_status`, `ocr_method`, `parent_chunk_id`, `document_type`
- Integer: `page_first`, `page_last`, `ordinal`, `revision_number`

### 7h. Redis logical databases

| Logical DB | Default # | Use |
|---|---|---|
| `default` | 0 | General app cache |
| `cache` | 1 | Pinned cache store |
| `queue` | 0 (override via `REDIS_QUEUE_DB`) | Horizon queues |
| `sessions` | 0 | Session storage |

### 7i. ClickHouse

Container: `clickhouse/clickhouse-server:24.10-alpine` (overlay `docker/compose.langfuse.yml`).
Database: `default` (single tenant — Langfuse only). User: `${CLICKHOUSE_USER:-langfuse}`.
**Not accessed by app code.** Langfuse-owned trace store.

### 7j. Object storage (SeaweedFS, S3-compatible)

Container: `chrislusf/seaweedfs:4.20` (compose svc name `minio` — legacy).
**Buckets** (env-named):
- `MINIO_BUCKET_BRONZE` = `bronze`
- `MINIO_BUCKET_BRONZE_RASTER` = `bronze-raster`
- `MINIO_BUCKET_EXPORTS` = `exports`

Laravel filesystem disks (`config/filesystems.php`): `s3`, `s3-bronze`, `s3-exports`.
---

## 8. Reverb channels (routes/channels.php) + event classes (app/Events/)

### 8a. Channel registrations

- `App.Models.User.{id}`
- `query.{queryId}`
- `workspace.{workspaceId}.activity`
- `project.{projectId}.ingestion`
- `admin.ingestion-review`
- `admin.reports.{build_id}`

Total: 6 channel patterns.

### 8b. Event classes

| File | broadcastAs() | broadcastOn() |
|---|---|---|
| `app/Events/Admin/IngestionReviewDispositionChanged.php` | `IngestionReviewDispositionChanged` | `admin.ingestion-review` |
| `app/Events/Admin/ReportBuildProgress.php` | `ReportBuildProgress` | `admin.reports.` |
| `app/Events/Dashboard/ActivityEventBroadcast.php` | `ActivityEventBroadcast` | `(dynamic)` |
| `app/Events/Dashboard/DocumentStageChanged.php` | `DocumentStageChanged` | `(dynamic)` |
| `app/Events/QueryStreamEvent.php` | `QueryStreamEvent` | `(dynamic)` |

---

## 9. Laravel routes

### 9a. routes/api.php — counts

Total Route:: declarations: 57

**Top-level prefixes used:**
- `auth`
- `dashboard`
- `public-geoscience`
- `v1`
- `vendor-profiles/{vendor_profile}`

**Internal bridge routes (`/internal/*` — service-key authed):**
(See `routes/api.php` lines 268-323 — 6 POST endpoints under `/internal/v1/*` + 1 `/internal/admin/reports/{build_id}/progress`.)

### 9b. routes/web.php — counts

Total Route:: declarations: 150

**Top-level URL families:** `/`, `/login`, `/forgot-password`, `/dashboard`, `/projects/*`, `/foundry/*`, `/public-geoscience/*`, `/admin/*`, `/retrieval/{traceId}`, `/threads`, `/charts-gallery`, `/up`, `/metrics`.
---

## 10. Martin tile server — MVT function inventory

Config: `docker/martin/martin.yaml`. Schema target: `public_geo` (canonical rename to `public_geoscience` pending).

- `pg_assessment_surveys_tiles`
- `pg_bedrock_geology_tiles`
- `pg_drillhole_collars_tiles`
- `pg_mineral_dispositions_tiles`
- `pg_mineral_occurrences_tiles`
- `pg_mines_tiles`
- `pg_resource_potential_tiles`
- `pg_rock_samples_tiles`
- `v_pg_assessment_surveys_mvt`
- `v_pg_bedrock_geology_mvt`
- `v_pg_drillhole_collars_mvt`
- `v_pg_geochemistry_samples_mvt`
- `v_pg_geochronology_samples_mvt`
- `v_pg_geological_domains_mvt`
- `v_pg_geological_faults_mvt`
- `v_pg_geological_feature_lines_mvt`
- `v_pg_geological_feature_points_mvt`
- `v_pg_geophysics_control_points_mvt`
- `v_pg_geophysics_survey_coverage_mvt`
- `v_pg_geoscience_publications_mvt`
- `v_pg_mineral_dispositions_mvt`
- `v_pg_mineral_occurrences_mvt`
- `v_pg_mines_mvt`
- `v_pg_petroleum_pools_mvt`
- `v_pg_petroleum_well_trajectories_mvt`
- `v_pg_petroleum_wells_mvt`
- `v_pg_regional_compilation_point_mvt`
- `v_pg_regional_compilation_polygon_mvt`
- `v_pg_resource_potential_mvt`
- `v_pg_rock_samples_mvt`
- `v_pg_surficial_geology_mvt`

**Function declaration sites (from migrations + raw SQL):**

- `public_geo.pg_assessment_surveys_tiles`
- `public_geo.pg_bedrock_geology_tiles`
- `public_geo.pg_drillhole_collars_tiles`
- `public_geo.pg_mineral_dispositions_tiles`
- `public_geo.pg_mineral_occurrences_tiles`
- `public_geo.pg_mines_tiles`
- `public_geo.pg_resource_potential_tiles`
- `public_geo.pg_rock_samples_tiles`
- `silver.enforce_data_version_monotonic`
- `silver.fn_set_updated_at`
- `silver.pg_boundaries_by_project`
- `silver.pg_collars_by_project`
- `silver.pg_cross_section_lines_by_project`
- `silver.pg_drill_traces_by_project`
- `silver.pg_formations_by_project`
- `silver.pg_geochem_by_project`
- `silver.pg_historic_workings_by_project`
- `silver.pg_seismic_by_project`
- `silver.significant_intersections_by_project`

---

## 11. CI/CD surface (.github/workflows/)

**Workflow files:**
- `cd.yml` — name: "CD — Deploy" — triggers: workflow_run, dispatch
- `chaos.yml` — name: "Chaos / Resilience" — triggers: dispatch, cron
- `ci.yml` — name: "GeoRAG CI" — triggers: push, pr
- `e2e.yml` — name: "E2E — Playwright" — triggers: pr, dispatch, cron
- `perf-baseline.yml` — name: "Perf — Baseline" — triggers: dispatch, cron
- `release-rehearsal.yml` — name: "GeoRAG Release Rehearsal" — triggers: push, dispatch
- `tenant-isolation-auditor.yml` — name: "Tenant Isolation Auditor (§11.5)" — triggers: pr, dispatch

**Dependabot ecosystems (.github/dependabot.yml):** `composer`, `npm`, `pip`, `github-actions`, `docker`.

---

## 12. Dockerfiles

- `docker/dagster.Dockerfile` — base: `python:3.13-slim` — CMD: `["dagster-daemon", "run"]`
- `docker/fastapi.Dockerfile` — base: `python:3.13-slim` — CMD: `uvicorn app.main:app \`
- `docker/langfuse-mcp.Dockerfile` — base: `python:3.13-slim` — CMD: `-`
- `docker/laravel.Dockerfile` — base: `php:8.5-cli` — CMD: `["php", "artisan", "octane:start", "--host=0.0.0.0", "--port=80", "--server=swoole"]`
- `docker/backup-agent/Dockerfile` — base: `alpine@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc` — CMD: `["sleep", "infinity"]`

---

## 13. Docker Compose surface

### 13a. docker-compose.yml — services

- `postgresql`
- `pgbouncer`
- `redis`
- `laravel-octane`
- `laravel-horizon`
- `laravel-reverb`
- `martin`
- `fastapi`
- `neo4j`
- `neo4j-warmup`
- `qdrant`
- `minio`
- `minio-init`
- `vllm`
- `vllm-warmup`
- `dagster-daemon`
- `dagster-webserver`
- `hatchet-lite`
- `hatchet-worker-ingestion`
- `hatchet-worker-ai`
- `kestra`
- `caddy`
- `otel-collector`
- `tempo`
- `prometheus`
- `alertmanager`
- `redis_exporter`
- `postgres_exporter`
- `neo4j_exporter`
- `loki`
- `promtail`
- `grafana`
- `ofelia`
- `backup-agent`

Total compose services: 34

### 13b. Compose overlays (docker/compose.*.yml)

- `docker/compose.exporters.yml` — adds: 
- `docker/compose.langfuse.yml` — adds: `clickhouse`, `langfuse-init`, `langfuse-web`, `langfuse-worker`
- `docker/compose.redis-staging.yml` — adds: 
- `docker/compose.vllm.yml` — adds: 
- `docker/compose.wal-archiving.yml` — adds: 

### 13c. Named volumes

- `postgres_data`
- `neo4j_data`
- `neo4j_logs`
- `neo4j_plugins`
- `qdrant_data`
- `redis_data`
- `minio_data`
- `georag-phase-b-extract`
- `vllm_hf_cache`
- `fastapi_hf_cache`
- `grafana_data`
- `alertmanager_data`
- `loki_data`
- `promtail_positions`
- `dagster_home`
- `backup_staging`
- `pg_wal_archive`
- `hatchet_config`
- `kestra_data`
- `kestra_workdir`
- `rapidocr_models`
- `tempo_data`
- `caddy_data`

### 13d. Networks

- `georag` (bridge driver) — single bridge network for all services
---

## 14. Laravel config files (config/)

- `ai.php`
- `app.php`
- `auth.php`
- `broadcasting.php`
- `cache.php`
- `cors.php`
- `dashboard.php`
- `database.php`
- `filesystems.php`
- `horizon.php`
- `inertia.php`
- `logging.php`
- `mail.php`
- `octane.php`
- `pulse.php`
- `queue.php`
- `reverb.php`
- `sanctum.php`
- `services.php`
- `session.php`

---

## 15. Environment surface

- `.env.example` — 211 keys (dev defaults)
- `.env.production.example` — 155 keys (SOPS+age plaintext template)

- Common: 149, dev-only: 62, prod-only: 6

- `src/fastapi/app/config.py::Settings` — 148 Pydantic-typed fields

**Feature flags in FastAPI Settings:**
- `AGENTIC_ESCALATION_ENABLED`
- `AGENTIC_FULL_ESCALATION_ENABLED`
- `AGENTIC_RETRIEVAL_V2_ENABLED`
- `CITATION_SPAN_RESOLVER_ENABLED`
- `CONTEXT_PREP_ENABLED`
- `ENTITY_RESOLUTION_ENABLED`
- `ENTITY_RESOLVER_SHADOW_ENABLED`
- `GEOLOGICAL_CONSTRAINTS_ENABLED`
- `GEO_ANSWER_OIUR_ENABLED`
- `LLM_CLASSIFIER_FALLBACK_ENABLED`
- `LLM_FALLBACK_ENABLED`
- `LOGFIRE_ENABLED`
- `MMR_ENABLED`
- `MODEL_ROUTING_ENABLED`
- `MULTI_TENANT_ENFORCEMENT_ENABLED`
- `MULTI_TURN_RESOLUTION_ENABLED`
- `NUMERICAL_VERIFICATION_ENABLED`
- `PARENT_CHUNKING_ENABLED`
- `RATE_LIMIT_ENABLED`
- `REPAIR_LOOP_FULL_ENABLED`
- `REPAIR_LOOP_LOWCOST_ENABLED`
- `REPAIR_LOOP_SHADOW_ENABLED`
- `REPAIR_LOOP_TERMINAL_ENABLED`
- `RETRIEVAL_CACHE_ENABLED`
- `SYSTEM_PROMPT_ROUTING_ENABLED`

---

## 16. Operator surface

### 16a. scripts/operator/
- `scripts/operator/bootstrap-secrets.sh`
- `scripts/operator/preflight.sh`
- `scripts/operator/set-github-secrets.sh`

### 16b. ops/setup/
- `ops/setup/apply_n8n_langfuse_secrets.sh`
- `ops/setup/apply_v1_14_env_tuning.sh`
- `ops/setup/bump_postgres_limits.py`
- `ops/setup/fix-gpu-passthrough.sh`
- `ops/setup/sync_windows_to_wsl.sh`
- `ops/setup/verify-gpu.sh`

### 16c. ops/runbooks/
Total: 38

- `authz-audit-triage.md`
- `backup-restore.md`
- `citation-pipeline.md`
- `claude-code-mcp-migration.md`
- `cold-start.md`
- `container-hardening.md`
- `data-version.md`
- `datastore-tuning.md`
- `dem-self-host.md`
- `deploy-rollback.md`
- `dr-1-postgres-loss.md`
- `dr-2-store-divergence.md`
- `dr-3-ransomware.md`
- `dr-4-full-datacenter.md`
- `dr-5-partial-outage.md`
- `drillhole-label-rename.md`
- `evidence-model.md`
- `hybrid-retrieval.md`
- `ingestion-pipeline.md`
- `llm-model-swap.md`
- `log-retention.md`
- `martin-tile-server.md`
- `migration-rollback.md`
- `neo4j-backup.md`
- `on-call.md`
- `qdrant-snapshot.md`
- `redis-3-instance-rollout.md`
- `redis-topology.md`
- `refusal-rate-spike.md`
- `retrieval-cache.md`
- `retrieval-pipeline.md`
- `retrieval-tuning.md`
- `s3-abstraction.md`
- `secret-management.md`
- `secret-rotation.md`
- `service-outage.md`
- `validation-corpora.md`
- `volume-migration.md`

Plus `docs/runbooks/caddy_tls.md`.

### 16d. ops/baselines/
- `2026-04-19-datastores-stats-idle.csv`
- `2026-04-19-docker-stats-idle.csv`
- `2026-04-19-infra-baselines.md`
- `2026-04-19-pg-config-after-tuning.txt`
- `2026-04-19-pg-config-before-tuning.txt`
- `2026-04-19-pg-tuning-results.md`
- `2026-04-20-datastores-baselines.md`
- `2026-04-21-module-4-parallel-dispatch.md`
- `2026-04-22-api-latency.md`
- `capacity-planning.md`

### 16e. ops/audit/
- `2026-04-19-datastores-audit.md`
- `2026-04-19-datastores-config.md`
- `2026-04-19-image-digests.json`
- `2026-04-19-infra-audit.md`
- `2026-04-19-infra-inventory.md`
- `2026-04-19-infra-phase-b-critical-fixes.md`
- `2026-04-19-resolved-compose-all-profiles.yml`
- `2026-04-19-resolved-compose.yml`
- `2026-04-20-ingestion-asset-graph.md`
- `2026-04-20-ingestion-audit.md`
- `2026-04-21-citation-guards-audit.md`
- `2026-04-21-llm-call-sites.md`
- `2026-04-21-llm-inference-audit.md`
- `2026-04-21-retrieval-audit.md`
- `2026-04-21-tool-call-01-investigation.md`
- `2026-04-22-chat-ui-audit.md`
- `2026-04-22-map-tile-audit.md`
- `2026-04-22-observability-release-audit.md`
- `2026-04-22-security-rbac-audit.md`

---

## 17. Observability surface

### 17a. Prometheus scrape targets (docker/prometheus/prometheus.yml)
- `fastapi`
- `laravel-octane`
- `neo4j`
- `qdrant`
- `martin`
- `redis`
- `postgresql`
- `alertmanager`
- `node`
- `prometheus`
- `vllm`
- `otel-collector`

### 17b. Prometheus alert rule files (docker/prometheus/rules/)

- `audit-ledger-alerts.yml` — 3 alert(s)
- `fastapi-alerts.yml` — 5 alert(s)
- `gpu-vram-health.yml` — 4 alert(s)
- `ingestion-reliability-alerts.yml` — 8 alert(s)
- `laravel-alerts.yml` — 6 alert(s)
- `martin-alerts.yml` — 5 alert(s)
- `neo4j-alerts.yml` — 3 alert(s)
- `p04p-dual-write-alerts.yml` — 3 alert(s)
- `postgres-alerts.yml` — 7 alert(s)
- `qdrant-alerts.yml` — 3 alert(s)
- `redis-alerts.yml` — 6 alert(s)
- `v3.1-supplemental-alerts.yml` — 5 alert(s)
- `vllm-alerts.yml` — 6 alert(s)

Total alert definitions: 64

### 17c. Grafana dashboards (docker/grafana/dashboards/)

- `georag-authz.json`
- `georag-integrations.json`
- `georag-laravel-queue.json`
- `georag-overview.json`
- `georag-rag-quality.json`
- `georag-repair-shadow.json`
- `georag-services.json`
- `georag-signals.json`
- `georag-workflows-cost-burn.json`
- `georag-workflows-dagster.json`
- `georag-workflows-hatchet.json`
- `georag-workflows-kestra.json`
- `georag-workflows-llm-pipeline.json`
- `georag-workflows-outbox.json`
- `product/georag-product-citation-quality.json`
- `product/georag-product-ingestion-throughput.json`
- `product/georag-product-workspace-health.json`

### 17d. Logging + tracing
- Promtail config: `docker/promtail/promtail-config.yaml`
- Loki config: `docker/loki/loki-config.yaml` (retention 720h)
- Tempo config: `docker/tempo/` (block retention 168h)
- OTel collector: `docker/otel-collector/otel-collector-config.yaml`
---

## 18. Architecture Decision Records (docs/adr/)

| # | Title | Status |
|---|---|---|
| 0001 | SeaweedFS replaces MinIO as the S3-compatible object store | Accepted |
| 0002 | §04p PDF stack replaces RAGFlow as the canonical parser | Accepted |
| 0003 | Defer bge-reranker-v2-m3 + GPU reranker host upgrade | Proposed (Deferred — trigger conditions below) |
| 0004 | Orchestrator short-circuit for high-confidence definition queries | Proposed (implementation gated on SME sign-off + design revi |
| 0005 | Normalize TIFF scans to PDF and route through the §04p PDF stack | Accepted (implementation in flight under the same date's aut |
| 0006 | Agentic retrieval — one LangGraph + six routed intents (not six subgraphs) | Accepted (codifies the as-built architecture from Phase 2 /  |
| 0007 | Chat-embedded interactive cards + two new agentic-retrieval intents | Accepted (approved by Kyle 2026-05-25) |
| 0008 | Embedding model evaluation — what to do about `bge-small` | Accepted — Option D (domain-fine-tune `bge-small` in place,  |
| 0009 | §3 and §4 algorithmic-spines rollout — stage-gated, flag-gated, evidence-gated | Accepted |
| 0010 | silver.document_passages is the canonical chunked-content corpus | Accepted |
| 0011 | Reranker domain adaptation — vocabulary, MLM, full fine-tune | Proposed |
| 0012 | Structured-to-NL summary corpus expansion | Proposed |

---

## 19. Eloquent models (app/Models/)

Total: 34

- `app/Models/Alteration.php`
- `app/Models/ChatConversation.php`
- `app/Models/ChatMessage.php`
- `app/Models/Collar.php`
- `app/Models/ColumnMapping.php`
- `app/Models/Eval/GoldenQuestion.php`
- `app/Models/Export.php`
- `app/Models/Geochemistry.php`
- `app/Models/LithologyLog.php`
- `app/Models/Ops/SupportReplayRun.php`
- `app/Models/Ops/SupportTicket.php`
- `app/Models/Ops/SupportTicketTrace.php`
- `app/Models/Project.php`
- `app/Models/PublicGeoscience/Jurisdiction.php`
- `app/Models/PublicGeoscience/PublicGeoSource.php`
- `app/Models/QueryAuditLog.php`
- `app/Models/Report.php`
- `app/Models/Sample.php`
- `app/Models/SavedMapView.php`
- `app/Models/Silver/DecisionEvidenceLink.php`
- `app/Models/Silver/DecisionLessonLearned.php`
- `app/Models/Silver/DecisionOption.php`
- `app/Models/Silver/DecisionOutcome.php`
- `app/Models/Silver/DecisionRecord.php`
- `app/Models/Silver/Hypothesis.php`
- `app/Models/Silver/HypothesisEvidenceLink.php`
- `app/Models/Structure.php`
- `app/Models/Survey.php`
- `app/Models/Targeting/TargetOutcome.php`
- `app/Models/Targeting/TargetRecommendation.php`
- `app/Models/Targeting/TargetReviewDecision.php`
- `app/Models/User.php`
- `app/Models/VendorProfile.php`
- `app/Models/WellLogCurve.php`

---

## 20. Inertia frontend pages (resources/js/Pages/)

Total .tsx files: 96

**Top-level pages:**
- `ChartsGallery.tsx`
- `Chat.tsx`
- `Explorer.tsx`
- `ForgotPassword.tsx`
- `InterpretationWorkspace.tsx`
- `Login.tsx`
- `NotFound.tsx`
- `SearchQuery.tsx`

**Foundry/ pages:**
- `Foundry/AssessmentSummary.tsx`
- `Foundry/AuditLog.tsx`
- `Foundry/Chat.tsx`
- `Foundry/Corpus.tsx`
- `Foundry/DataImportWizard.tsx`
- `Foundry/Decisions.tsx`
- `Foundry/DrillReview.tsx`
- `Foundry/DrillholeDetail.tsx`
- `Foundry/Explorer.tsx`
- `Foundry/HoleCompare.tsx`
- `Foundry/Hypothesis.tsx`
- `Foundry/Inbox.tsx`
- `Foundry/IngestQuality.tsx`
- `Foundry/IngestionRuns.tsx`
- `Foundry/Investigations.tsx`
- `Foundry/Lakehouse.tsx`
- `Foundry/Login.tsx`
- `Foundry/NewProject.tsx`
- `Foundry/Overview.tsx`
- `Foundry/Portfolio.tsx`
- `Foundry/ProjectAnalytics.tsx`
- `Foundry/Projects.tsx`
- `Foundry/PublicGeo.tsx`
- `Foundry/Rationale.tsx`
- `Foundry/Reasoning.tsx`
- `Foundry/Report.tsx`
- `Foundry/ReportView.tsx`
- `Foundry/RetrievalInspector.tsx`
- `Foundry/SavedMapViews.tsx`
- `Foundry/Settings.tsx`
- `Foundry/SourceGraph.tsx`
- `Foundry/Sources.tsx`
- `Foundry/SupportCockpit.tsx`
- `Foundry/Targets.tsx`
- `Foundry/Tier3Unlock.tsx`
- `Foundry/WhatChangedFeed.tsx`
- `Foundry/Workspace.tsx`

**Admin/ pages:**
- `Admin/AlertsInbox.tsx`
- `Admin/AuditExplorer.tsx`
- `Admin/AuditFindings.tsx`
- `Admin/BackupsDashboard.tsx`
- `Admin/CacheTelemetry.tsx`
- `Admin/ClusterIngest.tsx`
- `Admin/Conflicts.tsx`
- `Admin/Dashboards.tsx`
- `Admin/DecisionHistory.tsx`
- `Admin/DecisionNew.tsx`
- `Admin/EvalCompare.tsx`
- `Admin/EvalDashboard.tsx`
- `Admin/EvalQuestionEditor.tsx`
- `Admin/EvalQuestions.tsx`
- `Admin/ExportGate.tsx`
- `Admin/HatchetWorkers.tsx`
- `Admin/HypothesisWorkspace.tsx`
- `Admin/IngestionReview.tsx`
- `Admin/Integrations.tsx`
- `Admin/LoadTest.tsx`
- `Admin/MlTrainingRuns.tsx`
- `Admin/PhaseH4Health.tsx`
- `Admin/QpCredentials.tsx`
- `Admin/Recommendations.tsx`
- `Admin/ReportBuild.tsx`
- `Admin/ReportBuilder.tsx`
- `Admin/SavedMaps.tsx`
- `Admin/SourceTrust.tsx`
- `Admin/SupportCockpit.tsx`
- `Admin/TargetRecommendationCockpit.tsx`
- `Admin/TargetRecommendationRuns.tsx`
- `Admin/WhatChanged.tsx`
- `Admin/WorkflowRuns.tsx`
- `Admin/WorkspaceMembers.tsx`
- `Admin/WorkspaceSettings.tsx`
- `Admin//`
- `Admin//`

**Dashboards/ pages:**
- `Dashboards/EvidenceQuality.tsx`
- `Dashboards/LlmCost.tsx`
- `Dashboards/PublicGeoOverlay.tsx`
- `Dashboards/Reporting.tsx`
- `Dashboards/TargetRecommendation.tsx`
- `Dashboards/VisualReadiness.tsx`
- `Dashboards/_shared.tsx`

**Onboarding/ pages:**
- `Onboarding/Wizard.tsx`

**PublicGeoscience/ pages:**
- `PublicGeoscience/Index.tsx`

---

## 21. Laravel controllers (app/Http/Controllers/)

### Admin
- `AdminMiscController.php`
- `AuditFindingsController.php`
- `CacheTelemetryController.php`
- `ClusterIngestController.php`
- `ConflictsController.php`
- `DashboardsController.php`
- `DecisionHistoryController.php`
- `EvalCompareController.php`
- `EvalDashboardController.php`
- `EvalQuestionsController.php`
- `HatchetWorkersController.php`
- `HypothesisWorkspaceController.php`
- `IngestionReviewController.php`
- `IntegrationsController.php`
- `KestraSsoController.php`
- `MlTrainingRunsController.php`
- `ReportBuilderController.php`
- `ShadowRunsController.php`
- `SupportCockpitController.php`
- `TargetRecommendationCockpitController.php`
- `Tier234Controller.php`
- `WhatChangedController.php`
- `WorkflowRunController.php`

### Admin/AgentConfig
- `PinsController.php`
- `PromptsController.php`
- `TimeoutsController.php`
- `WorkspacesController.php`

### Api/V1
- `AuthController.php`
- `ChatConversationController.php`
- `CitationController.php`
- `CollarController.php`
- `ColumnMappingController.php`
- `CoverageDensityController.php`
- `DrillUploadController.php`
- `ExportController.php`
- `HoleAnalysisController.php`
- `IngestProgressController.php`
- `ProjectController.php`
- `PublicApiController.php`
- `QueryController.php`
- `SavedMapViewController.php`
- `TrustController.php`
- `UploadController.php`
- `VendorProfileController.php`

### Api/V1/PublicGeoscience
- `EntityReferencesController.php`
- `FeatureDetailController.php`
- `HealthController.php`
- `JurisdictionController.php`

### Dashboard
- `CustomerDashboardsController.php`
- `PortfolioController.php`
- `ProjectAnalyticsController.php`
- `ProjectDashboardController.php`

### Foundry
- `AssessmentSummaryController.php`
- `AuditLogController.php`
- `ChatController.php`
- `CorpusController.php`
- `DecisionsController.php`
- `DrillReviewController.php`
- `DrillholeDetailController.php`
- `ExplorerController.php`
- `HoleCompareController.php`
- `InboxController.php`
- `IngestQualityController.php`
- `IngestionRunsController.php`
- `InvestigationsController.php`
- `LakehouseController.php`
- `OverviewController.php`
- `PortfolioController.php`
- `ProjectAnalyticsController.php`
- `ProjectsIndexController.php`
- `PublicGeoController.php`
- `RationaleController.php`
- `ReasoningController.php`
- `ReportController.php`
- `RetrievalInspectorController.php`
- `SavedMapViewsController.php`
- `SettingsController.php`
- `SourceGraphController.php`
- `SourcesController.php`
- `SupportCockpitController.php`
- `TargetsController.php`
- `Tier3Controller.php`
- `WhatChangedController.php`
- `WorkspaceController.php`

### Internal
- `AdminSurfaceUpdatedBridgeController.php`
- `IngestionProgressBroadcastController.php`
- `KestraSsoCheckController.php`
- `MetricsController.php`
- `PublicGeoscienceTilesInvalidatedBridgeController.php`
- `ReportBuildProgressController.php`
- `UserInboxBridgeController.php`
- `WorkspaceActivityBridgeController.php`
- `WorkspaceDataUpdatedBridgeController.php`

### PublicGeoscience
- `TileProxyController.php`

### app/Http/Controllers
- `ChartsGalleryController.php`
- `CitationFeedbackController.php`
- `Controller.php`
- `InterpretationWorkspaceController.php`
- `OAuthIngestController.php`
- `OnboardingController.php`
- `PublicGeoscienceController.php`
---

## 22. Cross-service boundaries (inferred from compose + config)

### 22a. Inbound to Laravel (Octane :80 / :8888)
- Browser SPA (Inertia) + /api/v1/* (Sanctum)
- FastAPI /internal/v1/* (service-key bridge for broadcast fan-out)
- Caddy /internal/sanctum/check (forward-auth for Kestra SSO)

### 22b. Inbound to FastAPI (uvicorn :8000)
- Laravel (service-key + JWT minted by FastApiJwtMinter)
- Hatchet workers (via FastAPI module imports)
- Kestra (per-flow JWT)
- Dagster (direct HTTP from assets)

### 22c. Inbound to Reverb (:8085)
- Browser WebSocket (Echo, pusher-js protocol)
- Laravel (server-side broadcast() calls)

### 22d. Inbound to Hatchet engine (hatchet-lite:7077 gRPC, :8889 API)
- hatchet-worker-ingestion (gRPC)
- hatchet-worker-ai (gRPC)
- Operator UI on :8889
- FastAPI workflow-trigger HTTP calls

### 22e. Inbound to Postgres (pgbouncer:6432 runtime / postgresql:5432 direct)
- Laravel runtime via pgbouncer
- Laravel migrations direct (pgsql_migrations connection)
- FastAPI via pgbouncer + asyncpg
- Dagster direct, DB georag_dagster
- Hatchet engine direct, DB hatchet
- Martin direct, martin_ro role
- backup-agent (Ofelia cron via pg_basebackup / pg_dump)

### 22f. Inbound to Neo4j (:7474 HTTP, :7687 Bolt)
- FastAPI (async neo4j driver, Bolt)
- Dagster (sync neo4j driver, Bolt)
- Hatchet workers (sync neo4j driver, Bolt)

### 22g. Inbound to Qdrant (:6333 HTTP, :6334 gRPC)
- FastAPI (async qdrant-client)
- Dagster (sync qdrant-client)
- Hatchet workers (backup_qdrant + embed)

### 22h. Inbound to Redis (:6379)
- Laravel: cache (db 1), queue (db 0), sessions (db 0), broadcast pub/sub
- Reverb (presence + scaling pub/sub via 'reverb' channel)
- Horizon
- FastAPI (aioredis)
- Pulse ingest driver (optional)

### 22i. Inbound to vLLM (:8000 OpenAI-compat)
- FastAPI (OpenAI client at LLM_PRIMARY_URL)
- Dagster reranker label-mining (offline)

### 22j. Inbound to SeaweedFS (:8333 S3 API)
- Laravel (s3, s3-bronze, s3-exports disks)
- FastAPI (aioboto3)
- Dagster (boto3 via S3Resource)
- Hatchet workers (backup workflows)
- Kestra public_geoscience_pull (s3.Upload plugin)

### 22k. Outbound from FastAPI
- vLLM (LLM_PRIMARY_URL)
- Anthropic API (LLM_BACKEND=anthropic or fallback)
- Laravel /internal/v1/* bridge
- Kestra (flow trigger HTTP)
- Sentry, Logfire (when enabled)

### 22l. Outbound from Laravel
- FastAPI /internal/* proxy
- Dagster GraphQL (drill upload dispatch)
- Sentry
- Anthropic / OpenAI via laravel/ai SDK
- SMTP (MAIL_MAILER: log dev / smtp prod)
- Slack (SLACK_BOT_USER_OAUTH_TOKEN wired but no Mail::/Notification:: calls in app/)

### 22m. Outbound from Kestra
- FastAPI /internal/v1/integrations/{flow}/trigger
- SMTP (MailSend in support_packet_dispatch.yaml)
- External webhooks (PagerDuty / Slack via HTTP)

---

## 23. Canonical-tree index

### 23a. docs/architecture/ (existing files)

- `MANUAL.md`
- `context_prep_spec.md`
- `data_quality_flags_design.md`
- `document_versioning_design.md`
- `golden_question_seed_loader_design.md`
- `multi_turn_resolution_spec.md`
- `parent_child_chunker_spec.md`
- `repair_loop_spec.md`
- `reranker_v1_blockers.md`
- `shadow_telemetry_sentry_tags.md`
- `six_subgraphs_spec.md`
- `spatial_chat_card_audit_2026_05_29.md`
- `structured_answer_format_spec.md`
- `trace_logging_design.md`
- `user_facing_error_catalog.md`

**Note:** the reconciliation plan references `docs/architecture/manual/`, `data_dict/`, `appendix/`, `notes/INDEX.md` as canonical. These subdirs were **not present** on this scan. Flag for PASS 1 confirmation.

### 23b. docs/ top-level (non-archive)

- `04f-public-geoscience-addendum.md`
- `OPERATOR-AFTERNOON.md`
- `RUNBOOK.md`
- `SERVICE_INVENTORY.md`
- `acceptance-criteria.md`
- `architecture_review_for_sonnet_2026_05_22.md`
- `audit_ledger_hash_recipe.md`
- `cc01_cc03_cc04_followups_2026_05_24.md`
- `cc01_cc03_cc04_handoff_2026_05_23.md`
- `cc01_partial_items_kickoff.md`
- `chart_export_contract_spec.md`
- `consultation_package_scoping.md`
- `field-inventory-sk-tier2-tier3.md`
- `georag-claude-code-setup.md`
- `handoff-migration-status.md`
- `kyle-decisions.md`
- `langfuse-langgraph-tooling-setup.md`
- `master_plan_orchestrator_refactor.md`
- `master_plan_section10_scope_proposal.md`
- `master_plan_section11_scope_proposal.md`
- `master_plan_section12_scope_proposal.md`
- `master_plan_section5_scope_proposal.md`
- `master_plan_section6_scope_proposal.md`
- `master_plan_section7_scope_proposal.md`
- `master_plan_section8_scope_proposal.md`
- `master_plan_section9_scope_proposal.md`
- `mining_hub_carl_meeting_brief.md`
- `model_migration.md`
- `module-6-chunk-2-design.md`
- `mvt-nullable-numeric-convention.md`
- `overnight_6_phase_closeout.md`
- `overnight_ingestion_report.md`
- `pdf_ingestion_kickoff_closeout_2026_05_23.md`
- `phase0_handoff.md`
- `phase100_handoff.md`
- `phase101_handoff.md`
- `phase102_handoff.md`
- `phase103_handoff.md`
- `phase104_handoff.md`
- `phase105_handoff.md`
- `phase106_handoff.md`
- `phase107_handoff.md`
- `phase108_handoff.md`
- `phase109_handoff.md`
- `phase10_handoff.md`
- `phase110_handoff.md`
- `phase111_handoff.md`
- `phase112_handoff.md`
- `phase113_handoff.md`
- `phase114_handoff.md`
- `phase115_handoff.md`
- `phase116_handoff.md`
- `phase117_handoff.md`
- `phase118_handoff.md`
- `phase119_handoff.md`
- `phase11_golden_baseline.md`
- `phase11_handoff.md`
- `phase11_scoping.md`
- `phase11_section_04i_audit.md`
- `phase120_handoff.md`
- `phase121_handoff.md`
- `phase122_handoff.md`
- `phase122_rebuild_incident.md`
- `phase123_handoff.md`
- `phase124_handoff.md`
- `phase125_handoff.md`
- `phase126_handoff.md`
- `phase127_handoff.md`
- `phase128_handoff.md`
- `phase129_handoff.md`
- `phase12_handoff.md`
- `phase130_handoff.md`
- `phase131_handoff.md`
- `phase132_handoff.md`
- `phase133_handoff.md`
- `phase134_handoff.md`
- `phase135_handoff.md`
- `phase136_handoff.md`
- `phase137_handoff.md`
- `phase138_handoff.md`
- `phase139_handoff.md`
- `phase13_golden_fixture_spec.md`
- `phase13_handoff.md`
- `phase140_handoff.md`
- `phase141_handoff.md`
- `phase142_handoff.md`
- `phase143_handoff.md`
- `phase144_handoff.md`
- `phase145_handoff.md`
- `phase146_handoff.md`
- `phase147_handoff.md`
- `phase148_handoff.md`
- `phase149_handoff.md`
- `phase14_handoff.md`
- `phase14_r-p13-1_scoping.md`
- `phase150_handoff.md`
- `phase151_handoff.md`
- `phase152_handoff.md`
- `phase153_155_handoff.md`
- `phase156_handoff.md`
- `phase157_handoff.md`
- `phase158_handoff.md`
- `phase159_handoff.md`
- `phase15_handoff.md`
- `phase15_orchestrator_prompts_audit.md`
- `phase160_handoff.md`
- `phase161_handoff.md`
- `phase162_handoff.md`
- `phase163_handoff.md`
- `phase164_handoff.md`
- `phase165_handoff.md`
- `phase166_168_handoff.md`
- `phase169_handoff.md`
- `phase16_handoff.md`
- `phase170_handoff.md`
- `phase171_handoff.md`
- `phase172_handoff.md`
- `phase174_handoff.md`
- `phase175_handoff.md`
- `phase176_handoff.md`
- `phase177_handoff.md`
- `phase17_golden_baseline_v2.md`
- `phase17_golden_failure_audit.md`
- `phase17_handoff.md`
- `phase18_assay_litho_schema_audit.md`
- `phase18_golden_baseline_v3.md`
- `phase18_handoff.md`
- `phase19_golden_baseline_v4.md`
- `phase19_handoff.md`
- `phase1_handoff.md`
- `phase1_step8_cutover_runbook.md`
- `phase1_v149_ingest_pdf_survey.md`
- `phase20_handoff.md`
- `phase21_handoff.md`
- `phase22_handoff.md`
- `phase23_cache_rehydration_investigation.md`
- `phase23_handoff.md`
- `phase24_handoff.md`
- `phase25_handoff.md`
- `phase26_handoff.md`
- `phase27_handoff.md`
- `phase28_handoff.md`
- `phase29_handoff.md`
- `phase2_activepieces_flows.md`
- `phase2_activepieces_upgrade.md`
- `phase2_handoff.md`
- `phase2_scope_proposal.md`
- `phase30_handoff.md`
- `phase30_implementation_diff_note.md`
- `phase31_handoff.md`
- `phase31_test_staleness_audit.md`
- `phase32_handoff.md`
- `phase33_handoff.md`
- `phase34_handoff.md`
- `phase35_handoff.md`
- `phase36_handoff.md`
- `phase37_handoff.md`
- `phase38_handoff.md`
- `phase39_handoff.md`
- `phase3_handoff.md`
- `phase3_master_plan_kickoff.md`
- `phase40_handoff.md`
- `phase41_handoff.md`
- `phase42_handoff.md`
- `phase43_handoff.md`
- `phase44_handoff.md`
- `phase45_handoff.md`
- `phase46_handoff.md`
- `phase47_handoff.md`
- `phase48_handoff.md`
- `phase49_handoff.md`
- `phase4_handoff.md`
- `phase50_handoff.md`
- `phase51_handoff.md`
- `phase52_handoff.md`
- `phase53_handoff.md`
- `phase54_handoff.md`
- `phase55_handoff.md`
- `phase56_handoff.md`
- `phase57_handoff.md`
- `phase58_handoff.md`
- `phase59_handoff.md`
- `phase5_handoff.md`
- `phase5_master_plan_kickoff.md`
- `phase60_handoff.md`
- `phase61_handoff.md`
- `phase62_handoff.md`
- `phase63_handoff.md`
- `phase64_handoff.md`
- `phase65_handoff.md`
- `phase66_handoff.md`
- `phase67_handoff.md`
- `phase6_handoff.md`
- `phase71_handoff.md`
- `phase72_73_handoff.md`
- `phase74_75_handoff.md`
- `phase76_handoff.md`
- `phase77_handoff.md`
- `phase78_handoff.md`
- `phase79_handoff.md`
- `phase7_handoff.md`
- `phase80_handoff.md`
- `phase81_handoff.md`
- `phase82_handoff.md`
- `phase83_handoff.md`
- `phase84_handoff.md`
- `phase85_handoff.md`
- `phase86_handoff.md`
- `phase87_handoff.md`
- `phase88_handoff.md`
- `phase89_handoff.md`
- `phase8_handoff.md`
- `phase8_hatchet_ha_design.md`
- `phase90_handoff.md`
- `phase91_handoff.md`
- `phase92_handoff.md`
- `phase93_handoff.md`
- `phase94_handoff.md`
- `phase95_handoff.md`
- `phase96_handoff.md`
- `phase97_handoff.md`
- `phase98_handoff.md`
- `phase99_handoff.md`
- `phase9_handoff.md`
- `phase_a_uranium_walk_complete.md`
- `phase_b_uranium_ingestion_complete.md`
- `phase_c_kg_population_complete.md`
- `phase_d_qdrant_embeddings_complete.md`
- `phase_e1_ocr_corpus_complete.md`
- `phase_e24_prompt_steering_complete.md`
- `phase_e31_guard_tuning_complete.md`
- `phase_f10_carry_over_prompt_drift.md`
- `phase_f10_reconciled.md`
- `phase_f11_context_builder_extracted.md`
- `phase_f12_llm_calls_extracted.md`
- `phase_f13_orchestrator_package.md`
- `phase_f14_run_cache_extracted.md`
- `phase_f2_chunk_quality_filter_complete.md`
- `phase_f3_kg_retrieval_investigation.md`
- `phase_f4_empty_tool_filter_complete.md`
- `phase_f5_proactive_insight_grounding_complete.md`
- `phase_f5b_layer4_tolerance_fix.md`
- `phase_f5c_golden_eval_safety_check.md`
- `phase_f6_query_classification_extracted.md`
- `phase_f7_tool_result_helpers_extracted.md`
- `phase_f8_graph_entities_extracted.md`
- `phase_f9_project_overview_tool.md`
- `phase_g1_deposit_models_shap.md`
- `phase_g2_restore_workspace_cross_store.md`
- `phase_g3_report_builder_e2e.md`
- `phase_g4_evidence_map_mode.md`
- `phase_g5_support_cockpit_agents.md`
- `phase_g_followup_dependabot_triage.md`
- `phase_g_followup_eval_matcher_tightening.md`
- `phase_g_followup_kestra_pagerduty_wired.md`
- `phase_g_followup_retrieval_cache_disabled.md`
- `phase_g_followups_complete.md`
- `phase_h2_cache_surface_and_export_compliance.md`
- `phase_h3_pgeo_dashboards_striplog_dr.md`
- `phase_h4_deploy_checklist.md`
- `phase_h_python_deps_audit.md`
- `phase_h_test_triage.md`
- `query-class-routing.md`
- `r-p15-1_prompt_migration_scope.md`
- `retrospective_0_15.md`
- `retrospective_16_28.md`
- `retrospective_29_addendum.md`
- `retrospective_30_addendum.md`
- `roadmap_phase16_onward.md`
- `smdi_ingestion_2026_05_25.md`
- `test_marker_conventions.md`
- `us_jurisdiction_expansion_scoping.md`

### 23c. docs/adr/ — 12 ADRs (see Section 18)
### 23d. docs/runbooks/ — 1 file (caddy_tls.md); 38 operator runbooks at ops/runbooks/ (Section 16c)
### 23e. docs/parsers/, docs/audits/, docs/security/, docs/deployment/, docs/load_tests/, docs/api/ — present, not enumerated this pass

---

## 24. Existing handover docs + confirmation-ledger harvest

- `docs/handover/SAD.md`
- `docs/handover/DFS.md`
- `docs/handover/API_DOCUMENTATION.md`
- `docs/handover/CICD_PIPELINE.md`
- `docs/handover/HANDOVER_INDEX.md`

Confirmation-ledger items flagged across existing docs (harvest before rebuild):
- Sentry package install state (composer requires; project_sentry_removed_2026_05_21 notes uninstall)
- Email / SMTP wiring: config/mail.php exists, no Mail::/Notification:: calls in app/
- K3s / Helm chart vs SSH+compose canonical prod path
- init-roles.sql placement vs auto-init dir
- cd.yml migration execution location
- continue-on-error: true debt in cd.yml (3 places)
- OpenAPI snapshot completeness (docs/api/openapi.json is partial)
- Public API auth for external callers (only Sanctum observed)
- Webhook subscription CRUD path
- Cosign image signing absent in ci.yml
- Tempo + Loki on local filesystem (S3 cutover deferred Phase 11)
- OTel logs pipeline dead-ends at debug exporter (Loki via Promtail only)
- pg_partman installed but no PARTITION OF declarations
- ADR-0011 + ADR-0012 promotion (both Proposed)
- ADR-0004 SME sign-off (Proposed)
- Legacy georag.workspace_id GUC writers (13 Python files)
- .sops.yaml location (preflight O-01b references; not found at repo root)
- Repair-loop production posture (4 flags default false)
- vLLM max-num-seqs ceiling on prod GPU
- claude-opus-4-7 model string semantics
- services.basemap.styles config key referenced but missing
- Anthropic prompt-cache enabled — prod TTL
- SANCTUM_TOKEN_PREFIX unset
- Inertia shared-prop per-render PG hits
- Pulse PULSE_INGEST_DRIVER default 'storage' (synchronous)
- vLLM image pin drift between SERVICE_INVENTORY.md (v0.19.1) and compose (v0.21.0)
- CLAUDE.md tech-snapshot lag: PgBouncer version, Swoole vs RoadRunner, Qwen model history
- Hatchet engine SERVER_GRPC_INSECURE=t / SERVER_AUTH_COOKIE_INSECURE=t prod hardening
- georag_app SET ROLE runtime behaviour
- PARSE_SUBPROCESS_MAX_WORKERS unset in .env.example
- WAL upload destination / PITR receiver
- PHPStan level-6 trajectory (tighten vs freeze)
- 3-instance Redis rollout status (REDIS_QUEUE_HOST etc env scaffolding)
- Helm chart CD wiring (charts/georag/ exists but no GitHub Action drives helm upgrade)
- Per-flow JWT rotation cadence vs flow_jwt_key_reaper expiry window
- HMAC secret distribution + revocation process
- Alertmanager production webhook endpoints
- P04P_DUAL_WRITE_ENABLED live state
- Octane tables.example:1000 scaffold leftover
- HATCHET_CLIENT_TOKEN provisioning in cold-start runbook
- Neo4j NEO4J_AUTH=none -> real password migration trap
- Martin schema-name drift (public_geo vs public_geoscience)
- docs/architecture/manual/ + data_dict/ + appendix/ + notes/ subdirs referenced by reconciliation plan but NOT FOUND on disk

---

## 25. Inventory totals (top-line)

| Surface | Count |
|---|---|
| FastAPI endpoints | 109 |
| FastAPI routers | 32 files (incl. 15 sub-routers in admin_tier1_misc + admin_tier234) |
| LangGraph subgraphs | 3 |
| Pydantic AI agents (@georag_agent) | 42 |
| Intent labels | 8 |
| Hatchet workflow modules | 45 |
| Hatchet cron-triggered workflows | 30 declarations |
| Dagster top-level asset modules | 53 |
| Dagster bronze_to_silver/ subdir assets | 4 |
| Dagster schedules | 6 |
| Dagster sensors | 1 (minio_upload_sensor) |
| Dagster asset checks | 27 across 6 files |
| Kestra flows | 3 |
| PG tables (all schemas) | 174 across 15 schemas |
| PG functions | 23 |
| PG triggers | 7 |
| PG materialized views | 1 |
| PG extensions | 15 |
| Neo4j node labels | 10 |
| Neo4j relationship types | 12 |
| Qdrant collections | 9 (2 primary + 7 public-geo) |
| Reverb channels | 30 patterns |
| Reverb event classes | 11 |
| Laravel api.php route declarations | 67 |
| Laravel web.php route declarations | 157 |
| ADRs | 12 |
| Operator runbooks | 38 |
| GitHub Actions workflows | 7 |
| Dockerfiles | 5 |
| Compose services (main) | 33 |
| Compose overlays | 5 |
| Compose named volumes | 23 |
| Prometheus scrape jobs | 12 |
| Prometheus alert defs | 64 across 13 rule files |
| Grafana dashboards | 14 system + 3 product |

---

*End of HANDOVER_MANIFEST.md (PASS 0). STOP. Awaiting approval before PASS 1.*
---

## 24a. Additional confirmation-ledger items harvested from existing docs

Items found in `docs/handover/{SAD,DFS,API_DOCUMENTATION,CICD_PIPELINE}.md`
Needs-Confirmation sections during PASS 2 harvest, not already in §24:

- Backup target storage (S3 bucket / NFS / local) — `backup-agent` + `compose.wal-archiving.yml` overlay configured via env vars not enumerated
- Qdrant snapshot cadence + target
- Dagster persistence (`dagster.yaml` — Postgres vs local SQLite)
- Detailed FastAPI request/response shapes outside on-disk OpenAPI snapshot
- `chaos.yml` / `perf-baseline.yml` / `tenant-isolation-auditor.yml` exact cron expressions
- `e2e.yml` internals (Playwright suite + trigger conditions)
- `release-rehearsal.yml` full job graph
- Container registry (GHCR) retention policy
- Auth methods other than Sanctum (OIDC / SSO per architecture HTML)
