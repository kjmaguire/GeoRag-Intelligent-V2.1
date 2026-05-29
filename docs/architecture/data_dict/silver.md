# Schema `silver` — Data Dictionary (skeleton)

See [Ch 03 §§ 2-4](../manual/03-schemas.md) for the curated reference.
80+ tables; this skeleton groups them by purpose.

## Tenancy

| Table | Created by | Status |
|---|---|---|
| `silver.workspaces` | [2026_04_20_100000](../../../database/migrations/2026_04_20_100000_create_workspaces_and_data_version.php) | Live |
| `silver.projects` | early; FK + `data_version` extended in [2026_04_20_100000](../../../database/migrations/2026_04_20_100000_create_workspaces_and_data_version.php) | Live |

## RAG audit trail

| Table | Created by | Status |
|---|---|---|
| `silver.answer_runs` | [2026_04_21_100000](../../../database/migrations/2026_04_21_100000_create_answer_runs.php) | Live |
| `silver.answer_retrieval_items` | [2026_04_21_110000](../../../database/migrations/2026_04_21_110000_create_answer_retrieval_items.php) | Live |
| `silver.answer_citation_items` | [2026_04_21_150000](../../../database/migrations/2026_04_21_150000_create_answer_citation_items.php) | Live |
| `silver.answer_citation_spans` | [2026_04_21_160000](../../../database/migrations/2026_04_21_160000_create_answer_citation_spans.php) | Live |
| `silver.evidence_items` | [2026_04_20_140000](../../../database/migrations/2026_04_20_140000_create_evidence_items.php) | Live |
| `silver.document_passages` | [2026_04_20_110000](../../../database/migrations/2026_04_20_110000_create_document_passages.php) | Live |
| `silver.message_feedback` | [2026_04_22_120000](../../../database/migrations/2026_04_22_120000_create_message_feedback.php) | Live |
| `silver.structured_record_lineage` | [2026_04_20_150000](../../../database/migrations/2026_04_20_150000_create_structured_record_lineage.php) | Live |

## Geological domain

| Table | Created by | Status |
|---|---|---|
| `silver.collars` | [2026_04_09_180100](../../../database/migrations/2026_04_09_180100_create_collars_table.php); spatial uncertainty in [2026_05_23_050000](../../../database/migrations/2026_05_23_050000_add_spatial_uncertainty_to_collars_and_spatial_features.php) | Live |
| `silver.assays_v2`, `silver.lithology` | [2026_05_20_060300](../../../database/migrations/2026_05_20_060300_create_silver_assays_v2_and_lithology.php) | Live (canonical) |
| `silver.assays`, `silver.lithology_logs` | original Apr-09 batch | Live (legacy; coexist with `_v2` / `lithology`) |
| `silver.samples`, `silver.geochemistry`, `silver.structures`, `silver.alterations`, `silver.surveys` | Apr-09 batch | Live |
| `silver.well_log_curves` | [2026_04_10_120000](../../../database/migrations/2026_04_10_120000_create_well_log_curves_table.php) | Live |
| `silver.spatial_features` | [2026_04_10_120100](../../../database/migrations/2026_04_10_120100_create_spatial_features_table.php); extended [2026_05_22_010000](../../../database/migrations/2026_05_22_010000_extend_silver_spatial_features.php) | Live |
| `silver.seismic_surveys` | [2026_04_10_120200](../../../database/migrations/2026_04_10_120200_create_seismic_surveys_table.php) | Live |
| `silver.drill_traces` | [2026_04_20_170000](../../../database/migrations/2026_04_20_170000_create_silver_drill_traces.php) | Live |
| `silver.reports` | [2026_04_09_180800](../../../database/migrations/2026_04_09_180800_create_reports_table.php) + versioning [2026_04_13_000000](../../../database/migrations/2026_04_13_000000_add_report_versioning.php) | Live |
| `silver.review_queue` | [2026_05_24_120000](../../../database/migrations/2026_05_24_120000_create_silver_review_queue.php) | Live |
| `silver.ingest_progress` | [2026_05_24_230000](../../../database/migrations/2026_05_24_230000_create_silver_ingest_progress.php); extended [2026_05_25_020532](../../../database/migrations/2026_05_25_020532_extend_silver_ingest_progress_per_run_rows.php) | Live |
| `silver.geophysics_surveys` | [2026_05_21_030000](../../../database/migrations/2026_05_21_030000_create_silver_geophysics_surveys.php) | Live |
| `silver.geological_formations`, `silver.project_boundaries`, `silver.historic_workings` | [2026_04_22_140000](../../../database/migrations/2026_04_22_140000_create_silver_boundary_formation_working_geochem.php) | Live |
| `silver.raster_layers` | [2026_04_18_140000](../../../database/migrations/2026_04_18_140000_create_silver_raster_layers_table.php) | Live |
| `silver.cog_rasters` | (in flight; see Dagster `silver_cog_rasters`) | Live |
| `silver.geochronology_samples` | recent batch | Live |

## §04p PDF quality track (May 2026)

`silver.ocr_page_quality`, `silver.table_extraction_quality`,
`silver.parser_run_artifacts`, `silver.document_ingestion_quality`,
`silver.low_confidence_page_reviews`, `silver.ingest_layouts`,
`silver.ingest_extractions`, `silver.ingest_ocr_results` — all created
in [2026_05_12_180000…180007](../../../database/migrations/) batch. Live.

## Decisions / hypothesis / collab

`silver.decision_records`, `silver.decision_evidence_links`,
`silver.decision_options`, `silver.decision_outcomes`,
`silver.decision_lessons_learned` — [2026_05_13_130000](../../../database/migrations/2026_05_13_130000_create_decision_intelligence_schema.php). Live.

`silver.hypotheses`, `silver.hypothesis_evidence_links` — [2026_05_13_120000](../../../database/migrations/2026_05_13_120000_create_silver_hypotheses.php). Live.

`silver.collab_anchors`, `silver.collab_comments` — [2026_05_16_120200](../../../database/migrations/2026_05_16_120200_create_collab_anchors_and_comments.php). Live.

`silver.target_rationales` — [2026_05_16_120000](../../../database/migrations/2026_05_16_120000_create_target_rationales_table.php). Live.

`silver.tier3_unlock_requests` — [2026_05_16_120100](../../../database/migrations/2026_05_16_120100_create_tier3_unlock_requests_table.php). Live.

`silver.saved_map_views` — [2026_05_13_090000](../../../database/migrations/2026_05_13_090000_create_silver_saved_map_views.php). Live.

`silver.support_packets` — [phase0/120-phase0-step6-support-packets.sql](../../../database/raw/phase0/120-phase0-step6-support-packets.sql). Live.

## Other

`silver.shadow_runs` — [phase1/20-shadow-runs-and-feature-flags.sql](../../../database/raw/phase1/20-shadow-runs-and-feature-flags.sql).
`silver.qp_credentials`, `silver.workspace_settings` — [phase0/101-phase-h4-ui-tables.sql](../../../database/raw/phase0/101-phase-h4-ui-tables.sql).
`silver.claim_ledger` — [phase0/110-section7-4-claim-ledger.sql](../../../database/raw/phase0/110-section7-4-claim-ledger.sql).
`silver.store_reconciliation_findings`, `silver.corpus_health_findings`, `silver.storage_tier_policy` — [phase0/70-layer-g-findings.sql](../../../database/raw/phase0/70-layer-g-findings.sql).
`silver.geological_ontology_terms`, `silver.geological_ontology_synonyms` — [2026_05_13_110000](../../../database/migrations/2026_05_13_110000_create_geological_ontology_schema.php).
`silver.source_trust_scores`, `silver.source_trust_features` — [2026_05_13_150000](../../../database/migrations/2026_05_13_150000_create_source_trust_schema.php).
`silver.section_lines`, `silver.structure_measurements` — phase5 SQL.

## Tables that do NOT exist (despite older references)

- `silver.entities` — never created. Use `workspace.entities`.
- `silver.lithology_intervals` — never created. Use `silver.lithology` (new) or `silver.lithology_logs` (legacy).

## Triggers

- `workspaces_data_version_monotonic`, `projects_data_version_monotonic` — see [Ch 03 §10](../manual/03-schemas.md).

## RLS

- Coverage chain documented in [Ch 11 §5](../manual/11-tenancy-and-rls.md).
- Backstop: [tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php](../../../tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php).
