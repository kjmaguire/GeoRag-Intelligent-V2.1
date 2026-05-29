# Chapter 03 — Schemas and Tables

This is the per-table map of `georag` (the main application logical DB).
File:line citations point either into `database/migrations/` (Laravel-managed)
or `database/raw/phase{0,1,2,3,4,5}/*.sql` (raw SQL bootstrap). The base
medallion split is created by
[docker/postgresql/init/init-postgis.sql](../../../docker/postgresql/init/init-postgis.sql)
and extended by
[10-phase0-extensions-and-schemas.sql](../../../docker/postgresql/init/10-phase0-extensions-and-schemas.sql).

> Database-level `search_path` is set to
> `silver, bronze, gold, index, audit, public`
> ([init-postgis.sql:101](../../../docker/postgresql/init/init-postgis.sql)).

## 1. Schema map

| Schema | Created at | What it holds |
|---|---|---|
| `public` | (Postgres default) | Laravel-managed tables (users, jobs, sessions, cache, migrations, password_reset_tokens), extension functions (postgis, pgcrypto), and the standalone `public.smdi_deposits` SK occurrences table. |
| `bronze` | [init-postgis.sql:59](../../../docker/postgresql/init/init-postgis.sql) | Raw, immutable ingest records. CRS can be anything; data is "as observed". Append-only. |
| `silver` | [init-postgis.sql:65](../../../docker/postgresql/init/init-postgis.sql) | Cleaned, CRS-normalised, FK-enforced domain tables. Primary read layer. ~80 tables. |
| `gold` | [init-postgis.sql:72](../../../docker/postgresql/init/init-postgis.sql) | Pre-computed aggregations / materialisations refreshed by Dagster. |
| `index` | [init-postgis.sql:79](../../../docker/postgresql/init/init-postgis.sql) (quoted — `index` is a reserved word elsewhere) | tsvector full-text indexes, entity-resolution lookup tables. Isolated so REINDEX/REFRESH ops don't lock core data. |
| `audit` | [init-postgis.sql:88](../../../docker/postgresql/init/init-postgis.sql) | Audit + compliance: `audit_ledger` (monthly partitioned, hash-chained), verification runs, fork quarantine, integration_credentials_audit, query_audit_log (moved here from public in [2026_05_07_120000](../../../database/migrations/2026_05_07_120000_move_query_audit_log_to_audit_schema.php)). |
| `partman` | [10-phase0-extensions-and-schemas.sql:58](../../../docker/postgresql/init/10-phase0-extensions-and-schemas.sql) | `pg_partman` extension's own catalog. |
| `usage` | [10-phase0-extensions-and-schemas.sql:82](../../../docker/postgresql/init/10-phase0-extensions-and-schemas.sql) | `usage_events`, `usage_aggregates_daily`, `workspace_cost_ceilings`, `workspace_cost_quotas`. |
| `outbox` | [10-phase0-extensions-and-schemas.sql:83](../../../docker/postgresql/init/10-phase0-extensions-and-schemas.sql) | `pending_propagations`, `propagation_attempts` — the transactional outbox the `outbox_dispatcher` Hatchet workflow polls. |
| `workflow` | [10-phase0-extensions-and-schemas.sql:84](../../../docker/postgresql/init/10-phase0-extensions-and-schemas.sql) | Hatchet workflow state mirror: `workflow_runs` (partitioned), `workflow_run_events`, `workflow_run_steps`. |
| `workspace` | [10-phase0-extensions-and-schemas.sql:85](../../../docker/postgresql/init/10-phase0-extensions-and-schemas.sql) | Tenancy + RBAC: `workspaces`, `users`, `memberships`, `workspace_roles`, `agent_permissions`, `approval_requirements`, `tool_invocations`, `agent_timeouts`, `prompt_versions`, `idempotency_keys`, `dry_run_outputs`. |
| `interpretation` | [phase0/107-section19-3-interpretation-schema.sql:13](../../../database/raw/phase0/107-section19-3-interpretation-schema.sql) | Geologist annotations: notes, section_lines, target_zones, comments. |
| `targeting` | [2026_05_13_100000_create_targeting_schema.php:38](../../../database/migrations/2026_05_13_100000_create_targeting_schema.php) | target_backtests, target_score_factors, target_uncertainties. |
| `ops` | [2026_05_13_140100_create_ops_support_schema.php:24](../../../database/migrations/2026_05_13_140100_create_ops_support_schema.php) | support_tickets, support_ticket_traces, support_replay_runs. |
| `eval` | [2026_05_13_140000_create_eval_schema.php:29](../../../database/migrations/2026_05_13_140000_create_eval_schema.php) | Reranker / golden-query eval harness tables. |
| `public_geo` | [2026_04_14_000000_create_public_geoscience_schema.php:24](../../../database/migrations/2026_04_14_000000_create_public_geoscience_schema.php) | Public geoscience reference layers (provincial mines/occurrences/bedrock geology) + MVT views `v_pg_*_mvt`. Schema rename to `public_geoscience` planned ([docker/martin/martin.yaml:5](../../../docker/martin/martin.yaml)). |
| `backups` | [phase0/103-section11-backups-schema.sql:22](../../../database/raw/phase0/103-section11-backups-schema.sql) | Backup-run bookkeeping. |
| `topology` | (implicit, by `postgis_topology`) | PostGIS topology layer registry. |

Phase 0 verification block logs `Phase 0 init: 10/10 extensions, 8/8 namespaces`
([10-phase0-extensions-and-schemas.sql:104-120](../../../docker/postgresql/init/10-phase0-extensions-and-schemas.sql)).

---

## 2. Tenancy spine (silver + workspace)

### silver.workspaces

[2026_04_20_100000_create_workspaces_and_data_version.php:34](../../../database/migrations/2026_04_20_100000_create_workspaces_and_data_version.php)

- PK `workspace_id UUID`, `slug UNIQUE`, monotonic `data_version BIGINT`.
- Seeded with stable default `a0000000-0000-0000-0000-000000000001` (line 81).
- **RLS exempt** — tenant policies depend on reading it. Trigger
  `workspaces_data_version_monotonic` (BEFORE UPDATE) refuses any non-monotonic
  bump (line 70 + helper at line 50).

### silver.projects

Created in earlier migration; `workspace_id` FK + `data_version` added in
[2026_04_20_100000:97-118](../../../database/migrations/2026_04_20_100000_create_workspaces_and_data_version.php).
Has RLS. PK `project_id UUID`.

### public.users

Laravel auto-increment BIGINT `id` PK. Referenced from FastAPI-domain tables
as `BIGINT NULL REFERENCES public.users(id)` (e.g.,
[silver.answer_runs.user_id:63](../../../database/migrations/2026_04_21_100000_create_answer_runs.php)).
No `workspace_id` — a user can belong to many workspaces via
`workspace.memberships`.

---

## 3. RAG audit trail (silver)

### silver.answer_runs

[2026_04_21_100000_create_answer_runs.php:56](../../../database/migrations/2026_04_21_100000_create_answer_runs.php).
PK `answer_run_id UUID`. Every chat turn writes exactly one row.

Key columns:
- `workspace_id` FK CASCADE, `project_id` FK SET NULL, `user_id` FK RESTRICT.
- `query_text`, `query_class` — CHECK in `factual|spatial|document|computation|viz|unknown` (line 107).
- `embedding_model_version`, `sparse_model_version`, `reranker_version`.
- `fusion_method` — CHECK in `rrf|dbsf` (line 117).
- `workspace_data_version_at_query` — captured at query time so the answer
  can be replayed against the workspace’s frozen state.
- `backend_used` — CHECK in `vllm|ollama|anthropic` (line 120).
- `prompt_tokens`, `completion_tokens`, `total_tokens`.
- `citation_lifecycle_state` (line 123).
- `trace_id`, `root_span_id` — for joins with Tempo.

Indexes: `(workspace_id)`, `(project_id)`, `(created_at DESC)`,
partial `(trace_id) WHERE trace_id IS NOT NULL`, `(query_class)`.

Subsequent migrations:
- [2026_05_20_020000_add_hallucination_guard_results_to_answer_runs.php](../../../database/migrations/2026_05_20_020000_add_hallucination_guard_results_to_answer_runs.php)
- [2026_05_21_010000_add_lineage_columns_to_answer_runs.php](../../../database/migrations/2026_05_21_010000_add_lineage_columns_to_answer_runs.php)
- [2026_05_25_200000_add_confidence_and_latency_ms_to_answer_runs.php](../../../database/migrations/2026_05_25_200000_add_confidence_and_latency_ms_to_answer_runs.php)

### silver.answer_citation_items

[2026_04_21_150000_create_answer_citation_items.php:58](../../../database/migrations/2026_04_21_150000_create_answer_citation_items.php).
PK `answer_citation_item_id UUID`. FKs to `answer_runs` (CASCADE),
`workspaces` (CASCADE), `evidence_items` (SET NULL), `document_passages`
(SET NULL). `marker_text VARCHAR(64)` accepts both legacy `[DATA-N]` and
`[ev:<8-char>]` formats — see Hard Rule #4 (citations are mandatory).

### silver.answer_citation_spans

[2026_04_21_160000_create_answer_citation_spans.php](../../../database/migrations/2026_04_21_160000_create_answer_citation_spans.php).
Resolves each marker to a character span in the answer text — drives the
inline citation pill UI in the chat view.

### silver.evidence_items + silver.document_passages

Canonical evidence-anchor and passage tables (FK targets above). Created in
the April 20 batch.

### silver.message_feedback

[2026_04_22_120000_create_message_feedback.php:58](../../../database/migrations/2026_04_22_120000_create_message_feedback.php) — thumbs/comments per assistant turn.

---

## 4. Geological domain (silver) — the §04e contracts

These are the **§04e schema contracts** (Hard Rule #6 — don’t invent fields).

### silver.collars

[2026_04_09_180100_create_collars_table.php:15](../../../database/migrations/2026_04_09_180100_create_collars_table.php).
PK `collar_id UUID`. UTM Zone 13N geometry by default (EPSG:32613).

Columns include: `hole_id VARCHAR(50)`, `project_id` (CASCADE),
`easting`/`northing`/`elevation`/`total_depth` FLOAT, `hole_type`/`status`
VARCHAR(20), `azimuth`/`dip`/`drill_date`. Unique `(project_id, hole_id)`.
GIST index `idx_collars_geom` (line 35). `workspace_id` added in
[phase0/96-rls-tenant-isolation-block1.sql:79-84](../../../database/raw/phase0/96-rls-tenant-isolation-block1.sql).
Extended by
[2026_05_20_060200_extend_silver_collars_drillhole.php](../../../database/migrations/2026_05_20_060200_extend_silver_collars_drillhole.php) and
[2026_05_23_050000_add_spatial_uncertainty_to_collars_and_spatial_features.php](../../../database/migrations/2026_05_23_050000_add_spatial_uncertainty_to_collars_and_spatial_features.php).
RLS enabled + FORCE.

### silver.assays_v2

[2026_05_20_060300_create_silver_assays_v2_and_lithology.php:35](../../../database/migrations/2026_05_20_060300_create_silver_assays_v2_and_lithology.php).
Replacement long-form assay table:
- PK `id UUID`, `workspace_id NOT NULL`, `collar_id` FK → `silver.collars`,
  `from_depth`/`to_depth NUMERIC`, generated `interval_length`,
  `element TEXT`, `value`, `unit`, `value_ppm`, `detection_limit`,
  `over_detection`/`under_detection BOOLEAN`,
  `qaqc_flag DEFAULT 'pass'`, `bronze_source_id` FK → `bronze.raw_assay_submissions`.
- CHECK `to_depth > from_depth` (line 56).
- Indexes: `(workspace_id, collar_id)`, `(workspace_id, element)`,
  `(collar_id, from_depth, to_depth)`.

> Coexists with legacy `silver.assays`. Cameco gotcha from memory: U₃O₈ rolls
> up to `silver.samples`, not gold composite tables
> ([project_workspace_3d_expansion_2026_05_25](../notes/INDEX.md#project_workspace_3d_expansion_2026_05_25)).

### silver.lithology

[2026_05_20_060300_create_silver_assays_v2_and_lithology.php:73](../../../database/migrations/2026_05_20_060300_create_silver_assays_v2_and_lithology.php).
PK `id UUID`. `rock_code`, `rock_name`, `description`, `colour`, `grain_size`,
`texture`, `weathering`, `hardness`, `logged_by`/`logged_date`. GIN tsvector
on `description` (line 98). Coexists with legacy `silver.lithology_logs`.

### silver.samples, silver.geochemistry, silver.structures, silver.alterations, silver.surveys, silver.well_log_curves, silver.seismic_surveys, silver.spatial_features

All created in the April 2026 batch
([2026_04_09_180200..180800](../../../database/migrations/),
[2026_04_10_120000..120200](../../../database/migrations/)) and progressively
extended.

### silver.reports

[2026_04_09_180800_create_reports_table.php](../../../database/migrations/2026_04_09_180800_create_reports_table.php).
The NI 43-101 / technical report anchor table. `workspace_id` backfilled with
orphan handling at
[phase0/96-rls-tenant-isolation-block1.sql:89-128](../../../database/raw/phase0/96-rls-tenant-isolation-block1.sql).
FK to `silver.workspaces ON DELETE CASCADE`. RLS enabled + FORCE.

### silver.drill_traces

[2026_04_20_170000_create_silver_drill_traces.php](../../../database/migrations/2026_04_20_170000_create_silver_drill_traces.php).
LineString geometry in EPSG:4326. GIST index `idx_drill_traces_geom`. Drives
the MapLibre drill trace overlay via Martin.

### silver.review_queue

[2026_05_24_120000_create_silver_review_queue.php:61](../../../database/migrations/2026_05_24_120000_create_silver_review_queue.php). PK `queue_id UUID`. Powers the drill-data upload review flow (CC-01 Item 1).
- `target_table`/`target_record_kind`
- `bronze_uri`, `payload JSONB`
- `confidence_record NUMERIC(4,3)` (range CHECK line 71)
- Custom ENUMs: `routing_decision review_routing_enum`
  (`auto_pass|review_required|auto_reject` at line 42),
  `lifecycle review_lifecycle_enum`
  (`pending|in_review|decided|committed|archived` at line 47),
  `decision_kind review_decision_enum`
  (`approve_as_parsed|approve_with_corrections|reject|defer` at line 52).
- Cross-row CHECK constraints (lines 87-96).

### silver.geophysics_surveys

[2026_05_21_030000_create_silver_geophysics_surveys.php](../../../database/migrations/2026_05_21_030000_create_silver_geophysics_surveys.php).
PK `survey_id UUID`. Holds magnetic/gravity/electromagnetic survey metadata.

### silver.ingest_progress

[2026_05_24_230000_create_silver_ingest_progress.php](../../../database/migrations/2026_05_24_230000_create_silver_ingest_progress.php) + extension
[2026_05_25_020532_extend_silver_ingest_progress_per_run_rows.php](../../../database/migrations/2026_05_25_020532_extend_silver_ingest_progress_per_run_rows.php).
Per-run Hatchet step status — drives the Ingestion Runs UI (Phase B).

### §04p quality-track tables (May 2026 batch)

All created in [2026_05_12_180000..180007](../../../database/migrations/):
- `silver.ocr_page_quality`
- `silver.table_extraction_quality`
- `silver.parser_run_artifacts`
- `silver.document_ingestion_quality`
- `silver.low_confidence_page_reviews`
- `silver.ingest_layouts`
- `silver.ingest_extractions`
- `silver.ingest_ocr_results`

### Decision intelligence (silver)

[2026_05_13_130000_create_decision_intelligence_schema.php](../../../database/migrations/2026_05_13_130000_create_decision_intelligence_schema.php):
- `silver.decision_records`, `silver.decision_evidence_links`,
  `silver.decision_options`, `silver.decision_outcomes`,
  `silver.decision_lessons_learned`.

### Geological ontology

[2026_05_13_110000_create_geological_ontology_schema.php](../../../database/migrations/2026_05_13_110000_create_geological_ontology_schema.php):
- `silver.geological_ontology_terms` (PK `term_id UUID`)
- `silver.geological_ontology_synonyms`

### Source trust + hypotheses + saved map views

- `silver.source_trust_scores`, `silver.source_trust_features` —
  [2026_05_13_150000](../../../database/migrations/2026_05_13_150000_create_source_trust_schema.php)
- `silver.hypotheses`, `silver.hypothesis_evidence_links` —
  [2026_05_13_120000](../../../database/migrations/2026_05_13_120000_create_silver_hypotheses.php)
- `silver.saved_map_views` —
  [2026_05_13_090000](../../../database/migrations/2026_05_13_090000_create_silver_saved_map_views.php)
- `silver.target_rationales` —
  [2026_05_16_120000](../../../database/migrations/2026_05_16_120000_create_target_rationales_table.php)
- `silver.collab_anchors`, `silver.collab_comments` —
  [2026_05_16_120200](../../../database/migrations/2026_05_16_120200_create_collab_anchors_and_comments.php)
- `silver.tier3_unlock_requests` —
  [2026_05_16_120100](../../../database/migrations/2026_05_16_120100_create_tier3_unlock_requests_table.php)

---

## 5. Bronze (raw ingest)

### bronze.provenance

[2026_04_18_130000_create_bronze_provenance_table.php:22](../../../database/migrations/2026_04_18_130000_create_bronze_provenance_table.php).
The single point of truth for "where did this silver row come from?"

- PK `provenance_id UUID`
- `target_schema VARCHAR(32)`, `target_table VARCHAR(64)`, `target_id UUID`
- `source_file TEXT`, `source_file_sha256 CHAR(64)`
- `source_row INTEGER`, `source_col_map JSONB`
- `parser_name VARCHAR(64)`, `parser_version VARCHAR(32)`
- `ingested_at TIMESTAMPTZ`, `ingest_run_id UUID NULL`
- Indexes on `(target_schema, target_table, target_id)`,
  `source_file_sha256`, `ingested_at DESC`,
  partial `(ingest_run_id) WHERE ingest_run_id IS NOT NULL`.

`workspace_id` added later + auto-populated via the trigger described in
§7 below.

### bronze.ingest_runs, bronze.ingest_manifest, bronze.ingest_triage_samples

[2026_05_14_130000_create_bronze_ingest_manifest.php](../../../database/migrations/2026_05_14_130000_create_bronze_ingest_manifest.php):
- `bronze.ingest_runs` (line 42) — PK `run_id UUID`. Status CHECK
  `running|completed|failed|cancelled` (line 55).
- `bronze.ingest_manifest` (line 67) — one row per file inside a zip.
  Captures TIFF metadata, `guessed_project`, `cluster_key`.
- `bronze.ingest_triage_samples` (line 103) — OCR samples + SME labels
  (FK to `public.users`).

### bronze.raw_assay_submissions, raw_lithology_logs, raw_surveys, raw_geophysical_runs, raw_collar_entries, source_files

[2026_05_20_060000_create_bronze_drillhole_tables.php](../../../database/migrations/2026_05_20_060000_create_bronze_drillhole_tables.php).
Bronze tables specifically for the drill-data upload flow.

### bronze.manifest

[2026_05_25_020540_create_bronze_manifest.php:33](../../../database/migrations/2026_05_25_020540_create_bronze_manifest.php).
Distinct from `bronze.ingest_manifest` — used by the May 25 ingest UI track.

---

## 6. Gold (materialisations)

| Table | Defined in | Refreshed by |
|---|---|---|
| `gold.assay_composites` | (legacy SQL bootstrap) | `dagster_gold_assay_composites` asset |
| `gold.h3_density_mineral` | [phase0/104-section6-h3-density-table.sql:23](../../../database/raw/phase0/104-section6-h3-density-table.sql) | Dagster `gold_h3_density` |
| `gold.cross_section_panels` | [phase5/20-cross-section-panels.sql:59](../../../database/raw/phase5/20-cross-section-panels.sql) + [2026_05_13_080001](../../../database/migrations/2026_05_13_080001_create_gold_cross_section_panels.php) | `gold_cross_section_panels` |
| `gold.drillhole_intervals_visual` | [phase5/10-drillhole-intervals-visual.sql:33](../../../database/raw/phase5/10-drillhole-intervals-visual.sql) + [2026_05_13_080000](../../../database/migrations/2026_05_13_080000_create_gold_drillhole_intervals_visual.php) | `gold_drillhole_intervals_visual` |
| `gold.structure_measurements_visual` | [phase5/30-structure-measurements-visual.sql:67](../../../database/raw/phase5/30-structure-measurements-visual.sql) + [2026_05_13_080002](../../../database/migrations/2026_05_13_080002_create_gold_structure_measurements_visual.php) | `gold_structure_measurements_visual` |
| `gold.mv_refresh_log` | [2026_05_25_020546_create_gold_mv_refresh_log.php:28](../../../database/migrations/2026_05_25_020546_create_gold_mv_refresh_log.php) | Written by every Dagster MV refresh |

---

## 7. Audit (hash-chained ledger)

### audit.audit_ledger

[phase0/20-layer-b-audit-ledger.sql:25](../../../database/raw/phase0/20-layer-b-audit-ledger.sql).
The append-only audit log.

- Composite PK `(id, created_at)` — table is `PARTITION BY RANGE (created_at)`
  with monthly partitions managed by `pg_partman`
  (lines 67-83; 24-month retention, infinite partitions).
- `workspace_id NULL`, `actor_id BIGINT NULL`, `actor_kind` CHECK in
  `user|system|agent|workflow|external` (line 30).
- `action_type TEXT`, `target_schema/table/id`, `payload JSONB`,
  `previous_hash BYTEA`, `hash BYTEA`, `trace_id`,
  `created_at TIMESTAMPTZ DEFAULT clock_timestamp()`.
- `clock_timestamp()` instead of `now()` deliberately — multiple rows in the
  same transaction get strictly-monotonic timestamps so chain ordering
  works (lines 39-43).
- Indexes: `(workspace_id, created_at DESC)`,
  `(action_type, created_at DESC)`,
  `(target_schema, target_table, target_id)`,
  partial `(trace_id) WHERE trace_id IS NOT NULL`.
- RLS enabled (Phase 0 tighten at
  [phase0/99-rls-block3-policy-tighten.sql:14](../../../database/raw/phase0/99-rls-block3-policy-tighten.sql)).

### audit.audit_ledger_verification_runs

[phase0/20-layer-b-audit-ledger.sql:92](../../../database/raw/phase0/20-layer-b-audit-ledger.sql).
Written nightly by the `audit_ledger_verify` Hatchet workflow.

### audit.audit_ledger_chain_fork_quarantine

[2026_05_19_180400_audit_ledger_chain_fork_quarantine.php:50](../../../database/migrations/2026_05_19_180400_audit_ledger_chain_fork_quarantine.php).
Rows that fail the chain check go here for forensics.

### audit.query_audit_log

Moved from `public` by
[2026_05_07_120000_move_query_audit_log_to_audit_schema.php](../../../database/migrations/2026_05_07_120000_move_query_audit_log_to_audit_schema.php).
Has `workspace_id`
([2026_04_22_180000](../../../database/migrations/2026_04_22_180000_add_workspace_id_to_query_audit_log.php)).

### audit.integration_credentials_audit

[phase0/80-layer-h-credentials-audit.sql:15](../../../database/raw/phase0/80-layer-h-credentials-audit.sql).
Every secret rotation / integration-credential change writes a row.

---

## 8. Outbox + workflow + usage

### outbox.pending_propagations

Polled by the `outbox_dispatcher` Hatchet workflow
([app/hatchet_workflows/outbox_dispatcher.py](../../../src/fastapi/app/hatchet_workflows/outbox_dispatcher.py))
using `FOR UPDATE SKIP LOCKED`. One row per (silver write → Qdrant/Neo4j/SeaweedFS
target) pair. Dead-letters after 3 transient failures.

### outbox.propagation_attempts

Every dispatch attempt is recorded — successes and failures both.

### workflow.workflow_runs / workflow_run_events / workflow_run_steps

Monthly-partitioned via `pg_partman`. The Hatchet engine writes its own state
to the `hatchet` DB; this schema is the **app-side mirror** with `workspace_id`,
so RLS still applies and the Hatchet Worker Dashboard view in Laravel can
display per-workspace history.

### usage.usage_events, usage_aggregates_daily, workspace_cost_ceilings, workspace_cost_quotas

LLM token + tool invocation counters per workspace. Drives the Tier 3 unlock
flow and the cost-burn watchdog Hatchet workflow.

### workspace.idempotency_keys + workspace.dry_run_outputs

Used by FastAPI endpoints that need idempotent semantics (uploads, exports);
`georag_app` has explicit DELETE on just these two tables
([phase1/10-georag-app-role.sql:55](../../../database/raw/phase1/10-georag-app-role.sql)).

---

## 9. public.smdi_deposits + public_geo.* (reference data)

### public.smdi_deposits

[2026_05_25_050000_create_smdi_deposits.php:37](../../../database/migrations/2026_05_25_050000_create_smdi_deposits.php).
Saskatchewan Mineral Deposit Index (6,012 points). PK `objectid INTEGER`
(mirrors upstream ArcGIS), `smdi VARCHAR(20)`, `geom GEOMETRY(Point, 4326)`
GIST `smdi_deposits_geom_idx`. `GRANT SELECT … TO martin_readonly` (line 90).
Standalone — parallel to the multi-jurisdiction `public_geo.pg_mineral_occurrence`
table. Reconciliation question noted in the migration header.

### public_geo.*

Created by [2026_04_14_*](../../../database/migrations/) batch. Holds:
- `pg_mines`, `pg_mineral_occurrence`, `pg_drillhole_collars`,
  `pg_rock_samples`, `pg_assessment_surveys`, `pg_resource_potential`,
  `pg_mineral_dispositions`, `pg_bedrock_geology`, plus their `v_pg_*_mvt`
  views (consumed by Martin).
- `pg_jurisdictions` — drives MVT `etag_hash` freshness contract.

Loaded by the Kestra flow [kestra/flows/georag/public_geoscience_pull.yaml](../../../kestra/flows/georag/public_geoscience_pull.yaml)
which calls FastAPI endpoints which call the Hatchet `public_geoscience_pull`
workflow.

---

## 10. Triggers — load-bearing invariants

### Audit hash-chain trigger

[phase0/90-audit-hash-chain-trigger.sql:71](../../../database/raw/phase0/90-audit-hash-chain-trigger.sql).
BEFORE INSERT on `audit.audit_ledger`. Function
`audit.compute_audit_hash()` (line 32):

1. `SELECT … FOR UPDATE` on the previous workspace row to serialise inserts (lines 40-45).
2. `NEW.hash = public.digest(<canonical message>, 'sha256')` where the
   canonical message is:
   ```
   hex(previous_hash) | actor_id | actor_kind | action_type |
   target_schema | target_table | target_id | payload::text |
   created_at_iso_utc
   ```
   (lines 49-58).
3. `digest` is schema-qualified to `public` so PgBouncer-pooled sessions resolve it regardless of `search_path`.

Recipe is documented in [docs/audit_ledger_hash_recipe.md](../../audit_ledger_hash_recipe.md).
The nightly verifier (`audit.run_verification` at
[phase1/10-georag-app-role.sql:69](../../../database/raw/phase1/10-georag-app-role.sql))
replays the recipe and writes to `audit.audit_ledger_verification_runs`.

### bronze.provenance auto-fill workspace_id

[2026_05_25_175601_add_workspace_id_autopopulation_trigger_to_bronze_provenance.php:112](../../../database/migrations/2026_05_25_175601_add_workspace_id_autopopulation_trigger_to_bronze_provenance.php).
BEFORE INSERT on `bronze.provenance`. Function
`bronze.provenance_autofill_workspace_id()` (line 49):

- If `NEW.workspace_id IS NOT NULL`, no-op.
- Otherwise resolves `workspace_id` from the target silver row via a CASE on
  `target_schema.target_table` covering 8 tables: `silver.collars`,
  `silver.samples`, `silver.lithology_logs`, `silver.reports`,
  `silver.spatial_features`, `silver.raster_layers`,
  `silver.geophysics_surveys`, `silver.assays_v2` (lines 64-89).
- `EXCEPTION WHEN OTHERS RETURN NEW` (line 104) — a missing/changed target
  PK can never block a provenance INSERT.

Covers all 10+ existing provenance writers (6 Dagster assets + 4 FastAPI
services) with **zero code changes**.

### Monotonic data_version triggers

`workspaces_data_version_monotonic` and `projects_data_version_monotonic`
([2026_04_20_100000](../../../database/migrations/2026_04_20_100000_create_workspaces_and_data_version.php))
raise if `NEW.data_version < OLD.data_version`. Fires only when distinct.

### feature_flags_audit_trg

[phase1/30-feature-flag-history.sql:140](../../../database/raw/phase1/30-feature-flag-history.sql).
History row on any feature-flag change.

---

## 11. Eval / targeting / ops (cross-cutting)

- `eval.*` — eval harness for the reranker and golden-query regressions
  ([2026_05_13_140000_create_eval_schema.php](../../../database/migrations/2026_05_13_140000_create_eval_schema.php)).
- `targeting.target_backtests`, `target_score_factors`, `target_uncertainties` —
  Phase 5 target scoring
  ([2026_05_13_100000](../../../database/migrations/2026_05_13_100000_create_targeting_schema.php)).
- `ops.support_tickets`, `support_ticket_traces`, `support_replay_runs` —
  the Support Cockpit replay flow
  ([2026_05_13_140100](../../../database/migrations/2026_05_13_140100_create_ops_support_schema.php)).

---

## 12. Migration / test-DB parity

[project_test_db_parity_gap](../notes/INDEX.md#project_test_db_parity_gap):
the 120 migrations that touch raw-SQL tables ship a sibling
`*_provision_*_for_test_db.php` mirror so the PHPUnit test DB picks them up.
The mirror chain was clean as of 2026-05-21 (and reconciled again on
2026-05-25 — `EXEMPT_TEST_DB_ONLY_TABLES` is now empty in the RLS coverage
test).
