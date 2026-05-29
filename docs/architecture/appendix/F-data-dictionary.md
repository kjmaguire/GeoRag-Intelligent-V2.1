# Appendix F — Data Dictionary + ERD

> **Status (2026-05-29)**: **Live (initial cut)** — generator implemented per Z.7 punchlist.
>
> - `data_dictionary_dump` Dagster asset at `src/dagster/georag_dagster/assets/data_dictionary_dump.py` walks `silver.*` + `gold.*`, dumps the per-table JSON shape defined in §3 below, and persists to `s3://catalogs/data_dictionary/<UTC-date>/`.
> - `data_dictionary_drift_check` sibling asset_check fails on column-removed / type-changed / PK-changed and forward-compat-passes on column-added.
> - ERD via `eralchemy2` with a hand-rolled Graphviz DOT fallback (always produced); SchemaSpy still on the future-enhancement list.
> - 22 tests in `src/dagster/tests/test_data_dictionary_dump.py`, all passing.
>
> The original draft below is preserved for spec-vs-implementation traceability. The implementation diverges from the draft in three places worth flagging:
>
> 1. The agent only walks `silver.*` + `gold.*` schemas (not the full 14-schema list the draft envisaged). Wider coverage is the obvious follow-up.
> 2. Output is one **single** `data_dictionary.json` array, not per-schema `docs/architecture/data_dict/<schema>.md` files. The MinIO-as-canonical pattern is simpler for the drift check.
> 3. The CI drift guard is a Dagster asset_check, not a standalone GitHub Action workflow. Same semantics, different runner.

Status (legacy heading, retained for diff continuity): **Draft.** Defines the contract for an *auto-generated* data
dictionary + ERD, plus a manual seed for the highest-value tables. The
generator is the source of truth once it lands; the seed is what to ship
in the interim.

## 1. Generator design

Dagster asset `data_dictionary_dump` (planned, group=`docs`):

1. Connects via `PostgresResource` (direct, owner role `georag` for full
   visibility).
2. Queries `information_schema.columns`, `pg_constraint`, `pg_index`,
   `pg_trigger`, `pg_policies` joined to `pg_class` / `pg_namespace`.
3. For each schema (`bronze`, `silver`, `gold`, `audit`, `usage`,
   `outbox`, `workflow`, `workspace`, `public_geo`, `interpretation`,
   `targeting`, `ops`, `eval`, `public`), writes
   `docs/architecture/data_dict/<schema>.md` with a section per table.
4. Per-table section contains the contract template in §3 below.

ERD:
- For each schema-group, run `eralchemy2` (or SchemaSpy) against the
  filtered subset → `docs/architecture/erd/<group>.svg`.
- Groups: `medallion-core` (bronze+silver+gold), `audit-and-usage`,
  `workflow-and-outbox`, `workspace-and-tenancy`, `public-geo`,
  `interpretation-and-targeting`.

CI gate:
- `data_dict_drift_check.yml` runs the generator in dry-run mode against
  the test DB; fails the build if the committed files diverge.

## 2. File layout

```
docs/architecture/
  data_dict/
    INDEX.md              ← list of schemas + counts
    bronze.md
    silver.md
    gold.md
    audit.md
    usage.md
    outbox.md
    workflow.md
    workspace.md
    public_geo.md
    interpretation.md
    targeting.md
    ops.md
    eval.md
    public.md
  erd/
    medallion-core.svg
    audit-and-usage.svg
    workflow-and-outbox.svg
    workspace-and-tenancy.svg
    public-geo.svg
    interpretation-and-targeting.svg
```

## 3. Per-table contract template

Every table entry MUST include:

```yaml
schema: silver
table: collars
purpose: |
  Drillhole collar canonical table. One row per (project_id, hole_id).
  Geometry stored in UTM Zone 13N + a derived EPSG:4326 column.
status: live
owner_service: dagster (silver_collars_canonicalize_backfill)
producers:
  - dagster.silver_collars_canonicalize_backfill
  - hatchet.ingest_drillhole_bundle (per-hole upsert)
  - laravel.api/v1/projects/{p}/datasets/drillhole/{id}/update (manual edit)
consumers:
  - martin.silver.pg_collars_by_project
  - frontend.DrillholeDetail, HoleCompare, Lakehouse, Workspace
  - graph.index_neo4j → :DrillHole node
  - rag.bm25_search / agent.query_collar_details
columns:
  - {name: collar_id,   type: UUID,    null: false, default: gen_random_uuid(), pk: true}
  - {name: workspace_id,type: UUID,    null: false, fk: silver.workspaces(workspace_id), rls: true}
  - {name: project_id,  type: UUID,    null: false, fk: silver.projects(project_id) ON DELETE CASCADE}
  - {name: hole_id,     type: VARCHAR(50), null: false}
  - {name: easting,     type: FLOAT,   null: true}
  - {name: northing,    type: FLOAT,   null: true}
  - {name: elevation,   type: FLOAT,   null: true}
  - {name: total_depth, type: FLOAT,   null: true}
  - {name: hole_type,   type: VARCHAR(20), null: true}
  - {name: status,      type: VARCHAR(20), null: true}
  - {name: azimuth,     type: FLOAT,   null: true}
  - {name: dip,         type: FLOAT,   null: true}
  - {name: drill_date,  type: DATE,    null: true}
  - {name: geom,        type: GEOMETRY(Point, 32613), null: true, gist_index: idx_collars_geom}
  - {name: spatial_uncertainty_m, type: NUMERIC(8,2), null: true}
  - {name: data_version,type: BIGINT,  null: false, default: 1}
  - {name: created_at,  type: TIMESTAMPTZ, null: false, default: clock_timestamp()}
  - {name: updated_at,  type: TIMESTAMPTZ, null: false, default: clock_timestamp()}
indexes:
  - {name: idx_collars_geom, kind: GIST, cols: (geom)}
  - {name: silver_collars_project_hole_key, kind: UNIQUE, cols: (project_id, hole_id)}
  - {name: silver_collars_workspace_idx, kind: BTREE, cols: (workspace_id, project_id)}
constraints:
  - {kind: CHECK, expr: "total_depth IS NULL OR total_depth >= 0"}
  - {kind: FK, target: silver.workspaces, on_delete: CASCADE}
triggers:
  - {name: silver_collars_touch_updated_at, when: BEFORE UPDATE, fn: silver.touch_updated_at()}
rls:
  enabled: true
  forced: true
  policy: |
    workspace_id = current_setting('app.workspace_id', true)::uuid
audit:
  - on INSERT/UPDATE/DELETE → audit.audit_ledger via app-side writes (no row-level trigger)
sensitive_fields: []
lifecycle:
  - retention: indefinite
  - cascade: ON DELETE silver.projects → restrict (project deletion blocked while collars exist)
references:
  schema_source: database/migrations/2026_04_09_180100_create_collars_table.php:15
  shape_test: database/tests/pgtap/silver_collars_shape.sql
```

## 4. Seed entries (manual until generator lands)

The highest-value tables — implement now, replace when the generator
ships. The pre-existing `docs/architecture/data_quality_flags_design.md`
and `document_versioning_design.md` complement this.

### 4.1 Tenancy / workspaces
- `silver.workspaces` — see [Ch 03 §2](../manual/03-schemas.md).
- `silver.projects` — workspace-scoped project root.
- `workspace.memberships` — user ↔ workspace role assignment.
- `workspace.workspace_roles` — system + per-workspace roles.

### 4.2 RAG audit
- `silver.answer_runs` — every chat turn → one row.
- `silver.answer_citation_items`, `silver.answer_citation_spans`,
  `silver.evidence_items`, `silver.document_passages`,
  `silver.message_feedback`.

### 4.3 Drillhole spine
- `silver.collars`, `silver.assays_v2`, `silver.lithology`,
  `silver.surveys`, `silver.drill_traces`, `silver.review_queue`.

### 4.4 Reports
- `silver.reports`, `silver.report_pages`, `silver.report_figures`,
  `silver.report_tables`, `silver.parser_run_artifacts`,
  `silver.ocr_page_quality`, `silver.table_extraction_quality`,
  `silver.document_ingestion_quality`,
  `silver.low_confidence_page_reviews`.

### 4.5 Geospatial
- `silver.spatial_features`, `silver.raster_layers`, `silver.cog_rasters`,
  `silver.geophysics_surveys`, `silver.seismic_surveys`,
  `silver.well_log_curves`.

### 4.6 Public geoscience
- `public_geo.pg_mines`, `pg_mineral_occurrences`,
  `pg_drillhole_collars`, `pg_rock_samples`, `pg_assessment_surveys`,
  `pg_resource_potential`, `pg_mineral_dispositions`,
  `pg_bedrock_geology`.
- `public.smdi_deposits`.

### 4.7 Bronze
- `bronze.provenance`, `bronze.ingest_runs`, `bronze.ingest_manifest`,
  `bronze.ingest_triage_samples`,
  `bronze.raw_assay_submissions`, `bronze.raw_lithology_logs`,
  `bronze.raw_surveys`, `bronze.raw_geophysical_runs`,
  `bronze.raw_collar_entries`, `bronze.source_files`,
  `bronze.manifest`.

### 4.8 Gold
- `gold.h3_density_mineral`, `gold.assay_composites`,
  `gold.drillhole_intervals_visual`, `gold.cross_section_panels`,
  `gold.structure_measurements_visual`, `gold.significant_intersections`
  (planned persistent), `gold.mv_refresh_log`.

### 4.9 Audit / observability
- `audit.audit_ledger` (partitioned),
  `audit.audit_ledger_verification_runs`,
  `audit.audit_ledger_chain_fork_quarantine`,
  `audit.query_audit_log`, `audit.integration_credentials_audit`.

### 4.10 Decisions / hypothesis
- `silver.decision_records`, `silver.decision_evidence_links`,
  `silver.decision_options`, `silver.decision_outcomes`,
  `silver.decision_lessons_learned`.
- `silver.hypotheses`, `silver.hypothesis_evidence_links`.

### 4.11 Targeting
- `targeting.target_backtests`, `targeting.target_score_factors`,
  `targeting.target_uncertainties`.
- `silver.target_rationales`.

### 4.12 Outbox / workflow
- `outbox.pending_propagations`, `outbox.propagation_attempts`.
- `workflow.workflow_runs` (partitioned),
  `workflow.workflow_run_events`, `workflow.workflow_run_steps`,
  `workflow.flow_registry`.

### 4.13 Usage / cost
- `usage.usage_events`, `usage.usage_aggregates_daily`,
  `usage.workspace_cost_ceilings`, `usage.workspace_cost_quotas`.

### 4.14 New (Ch 13)
- `silver.data_categories`, `silver.dataset_categories` — **planned**.

## 5. ERD groupings (proposed)

```
medallion-core
  bronze.ingest_runs ←─── ingest_manifest ←── raw_* drillhole + source_files
  bronze.provenance ──→  silver.collars, silver.lithology, silver.assays_v2, silver.reports, ...
  silver.collars ────→ silver.assays_v2, silver.lithology, silver.surveys, silver.drill_traces
  silver.reports ───→ silver.report_pages → silver.report_figures + silver.report_tables
                                          → silver.document_passages → silver.evidence_items
  silver.* ──────→ gold.* materialisations + outbox.pending_propagations

audit-and-usage
  audit.audit_ledger (monthly partitions)
  audit.audit_ledger_verification_runs (referenced by daily verifier)
  audit.audit_ledger_chain_fork_quarantine
  usage.usage_events → usage.usage_aggregates_daily
  usage.workspace_cost_quotas

workflow-and-outbox
  workflow.workflow_runs → workflow.workflow_run_events / _steps
  outbox.pending_propagations → outbox.propagation_attempts

workspace-and-tenancy
  silver.workspaces ←── silver.projects ←── all silver.* via workspace_id
  workspace.users, workspace.memberships, workspace.workspace_roles
  workspace.agent_permissions, workspace.approval_requirements
  workspace.idempotency_keys, workspace.dry_run_outputs

public-geo
  public_geo.pg_* canonical tables ← public_geo.v_pg_*_mvt views (Martin)

interpretation-and-targeting
  interpretation.* ← silver.collars + silver.drill_traces
  targeting.* ← silver.assays_v2 + gold.significant_intersections
```

## 6. CI drift guard

`.github/workflows/data-dict-drift.yml` (planned):
```yaml
on: [pull_request]
jobs:
  drift:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: scripts/regen_data_dict.sh --check
      - run: scripts/regen_erd.sh --check
```

Failure means a migration changed a table but no doc commit accompanied
it. Reviewer instruction: run `scripts/regen_data_dict.sh --apply` and
include the resulting Markdown diff in the PR.

## 7. Sensitive-field tagging

Some columns carry user PII or third-party-licensed data. Tag them via a
Postgres comment that the generator picks up:

```sql
COMMENT ON COLUMN bronze.ingest_triage_samples.sme_label_project IS
    'sensitive=pii';  -- harvested by the data dictionary generator
```

Tagging conventions:
- `sensitive=pii` — personal information.
- `sensitive=license:<source>` — third-party licensed reference data.
- `sensitive=internal` — internal-only operator notes.

The generator surfaces these as ⚠️ in the per-schema Markdown.
