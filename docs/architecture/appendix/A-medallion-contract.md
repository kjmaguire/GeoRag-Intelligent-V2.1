# Appendix A — Medallion Contract

Status: **Live for the contract; some tables in column-drift remediation.**

Defines the *contract* between bronze, silver, and gold beyond the table
inventory in [Ch 03](../manual/03-schemas.md): lineage fields, QA gates,
human review gates, reprocessing semantics, deletion/retention semantics,
fan-out posture, and the lineage-and-replayability test envelope.

> Everything below is binding regardless of which workflow (Hatchet,
> Dagster, Laravel queue, manual psql) wrote the row. If a writer cannot
> satisfy a required field, it must NOT write the row.

## 1. Required lineage fields (any silver / gold row)

Every silver and gold row must carry the following, either as columns on
the row itself or as a one-to-one row in `bronze.provenance`. The
[`bronze.provenance` trigger](../manual/03-schemas.md#10-triggers--load-bearing-invariants)
auto-fills `workspace_id` from the silver target if omitted.

| Field | Carrier | Notes |
|---|---|---|
| `workspace_id` | column on the row | NOT NULL on silver tables; NOT NULL on bronze writes after the May-25 hardening |
| `project_id` | column on the row (where applicable) | NULL for cross-project lakehouse artefacts |
| `source_file_id` | `bronze.provenance.source_file` + `source_file_sha256` | sha256 binds across runs |
| `source_file_sha256` | `bronze.provenance.source_file_sha256` | CHAR(64); the same file across uploads has the same value |
| `source_page` | `bronze.provenance.source_col_map.page` (JSONB) | For PDFs |
| `source_row` | `bronze.provenance.source_row INTEGER` | For tabular |
| `source_column` | `bronze.provenance.source_col_map.column` (JSONB) | For tabular |
| `ingest_run_id` | `bronze.provenance.ingest_run_id UUID` | FK → `bronze.ingest_runs.run_id` |
| `parser_run_id` | `silver.parser_run_artifacts.run_id` (PDF) | One row per PDF parse |
| `parser_name` | `bronze.provenance.parser_name VARCHAR(64)` | e.g., `pdf_report`, `csv_collar`, `xlsx_parser` |
| `parser_version` | `bronze.provenance.parser_version VARCHAR(32)` | semver |
| `data_version` | `silver.workspaces.data_version` snapshot at write | Captured per-row on hot rows; otherwise via workspace pointer |
| `confidence` | per-table (`silver.review_queue.confidence_record`, `silver.assays_v2.qaqc_flag`, …) | Range 0-1 where present |
| `review_state` | `silver.review_queue.lifecycle` | NULL when no review required |

## 2. Bronze tables — canonical inventory

| Table | Created in | Purpose |
|---|---|---|
| `bronze.provenance` | [2026_04_18_130000](../../../database/migrations/2026_04_18_130000_create_bronze_provenance_table.php) | The lineage spine: target → source mapping |
| `bronze.ingest_runs` | [2026_05_14_130000](../../../database/migrations/2026_05_14_130000_create_bronze_ingest_manifest.php) | One per user-triggered ingest action |
| `bronze.ingest_manifest` | same migration | One per file inside a run |
| `bronze.ingest_triage_samples` | same migration | OCR samples + SME labels |
| `bronze.raw_assay_submissions` | [2026_05_20_060000](../../../database/migrations/2026_05_20_060000_create_bronze_drillhole_tables.php) | Drillhole assay CSV/XLSX bronze |
| `bronze.raw_lithology_logs` | same | Drillhole lithology bronze |
| `bronze.raw_surveys` | same | Downhole survey bronze |
| `bronze.raw_geophysical_runs` | same | Downhole geophysics bronze |
| `bronze.raw_collar_entries` | same | Collar bronze |
| `bronze.source_files` | same | Per-file metadata + sha256 (drillhole flow) |
| `bronze.manifest` | [2026_05_25_020540](../../../database/migrations/2026_05_25_020540_create_bronze_manifest.php) | May-25 ingest UI track |

**Status gap.** This manual previously referenced `bronze.upload_files` and
`bronze.raw_samples`. Neither table exists. Action: either create them in
a follow-up migration or rename the references. Tracked in
[appendix Z](Z-roadmap.md).

## 3. Silver — derivation map (by source format)

| Source format | Bronze | Silver target | Writer |
|---|---|---|---|
| PDF technical report | `bronze.ingest_manifest` + SeaweedFS `bronze/<sha>/...` | `silver.reports`, `silver.report_pages`, `silver.report_figures`, `silver.report_tables`, `silver.parser_run_artifacts`, `silver.document_passages`, `silver.evidence_items` | Hatchet `ingest_pdf` |
| Collar CSV | `bronze.raw_collar_entries` | `silver.collars` | Dagster `silver_collars_canonicalize_backfill` |
| Lithology CSV | `bronze.raw_lithology_logs` | `silver.lithology` (canonical) + `silver.lithology_logs` (legacy) | Dagster `silver_lithology` |
| Assay CSV/XLSX | `bronze.raw_assay_submissions` | `silver.assays_v2` (canonical) + `silver.assays` (legacy) | Dagster (silver-side asset) |
| Survey CSV | `bronze.raw_surveys` | `silver.surveys` + `silver.drill_traces` (LineString) | Dagster |
| Sample CSV | (currently routed to `silver.samples` direct via Hatchet) | `silver.samples` | Hatchet/Dagster |
| LAS | (`bronze.well_log_curves`-staging) | `silver.well_log_curves` | Dagster `bronze_well_logs` → `silver_well_logs` |
| SEG-Y | bronze raw | `silver.seismic_surveys` | Dagster `bronze_seismic` |
| GPKG / GeoJSON / Shapefile | bronze raw | `silver.spatial_features`, `silver.project_boundaries`, `silver.geological_formations`, `silver.historic_workings` (where matched) | Dagster `bronze_spatial` |
| GeoTIFF / raster | bronze raw | `silver.raster_layers`, `silver.cog_rasters` | Dagster `bronze_*` → `silver_raster`, `silver_cog_rasters` |
| XYZ point cloud | bronze raw | `silver.spatial_features` (point class) | Dagster `bronze_xyz` |
| Provincial open-data | Kestra → `bronze.public_geoscience_*` | `public_geo.pg_*` | Dagster `bronze_public_geoscience`, `silver_public_geoscience` |
| Multi-sheet XLSX | bronze raw + sheet classifier | branch by sheet type → above silver targets | Dagster `bronze_xlsx` |

**Note.** Older docs reference `silver.lithology_intervals`. There is no
such table — the canonical interval table is `silver.lithology` (with a
generated `interval_length` column) and the legacy `silver.lithology_logs`.

## 4. Gold — materialisation map

| Gold table / view | Source silver | Refresher | Status |
|---|---|---|---|
| `gold.h3_density_mineral` | `silver.collars` + `public_geo.pg_mineral_occurrences` | Dagster `gold_h3_density` schedule | Live |
| `gold.drillhole_intervals_visual` | `silver.collars` + `silver.lithology` + `silver.assays_v2` | Dagster `gold_drillhole_intervals_visual` | Live |
| `gold.cross_section_panels` | `silver.section_lines` + intervals | Dagster `gold_cross_section_panels` | Live |
| `gold.structure_measurements_visual` | `silver.structure_measurements` | Dagster `gold_structure_measurements_visual` | Live |
| `gold.assay_composites` | `silver.assays_v2` + `silver.collars` | Dagster `silver_to_gold/assay_composites` | Live ([2026_05_20_060700](../../../database/migrations/2026_05_20_060700_create_gold_drillhole_tables.php) §1) |
| `gold.significant_intersections` | `silver.assays_v2` + `silver.collars` | Dagster `silver_to_gold/significant_intersections` | Live ([2026_05_20_060700](../../../database/migrations/2026_05_20_060700_create_gold_drillhole_tables.php) §2). The Martin function [2026_05_20_061000](../../../database/migrations/2026_05_20_061000_create_martin_significant_intersections_function.php) reads from this persisted table. |
| `gold.drill_summaries` | `silver.collars` + assays/lithology rollup | Dagster `silver_to_gold/drill_summaries` | Live |
| `gold.zone_statistics` | `silver.assays_v2` + zone polygons | Dagster `silver_to_gold/zone_statistics` | Live |
| `gold.qaqc_statistics` | `silver.assays_v2` QAQC flags | Dagster `silver_to_gold/qaqc_statistics` | Live |
| `gold.campaign_summaries` | `silver.collars.drill_date` rollup | Dagster `silver_to_gold/campaign_summaries` | Live |
| `gold.element_correlations` | `silver.assays_v2` element-pair Pearson r | Dagster `silver_to_gold/element_correlations` | Live |
| `gold.mv_refresh_log` | (self-written) | every gold-asset run | Live |

## 5. SeaweedFS bronze object key rules

- Bucket `bronze` is **immutable**. Objects are never overwritten — a new
  upload of the same logical file gets a new sha256 prefix.
- Key shape: `bronze/<workspace_id>/<project_id>/<sha256>/<original-filename>`.
  When `project_id` is unknown at upload (system-wide imports), it is
  `_unassigned/`.
- Generated rasters: `bronze-raster/<workspace_id>/<document_sha256>/page-<NNNN>.png`.
- Exports: `exports/<workspace_id>/<export_id>/<filename>`.
- Backups: `georag-backups/postgres/{base,wal}/...`, `georag-backups/neo4j/...`,
  `georag-backups/qdrant/...`, `georag-backups/redis/...`.
- Lifecycle policies move objects between `tier-hot`, `tier-warm`,
  `tier-cold` based on `silver.storage_tier_policy` decisions.

## 6. QA gates

A silver row may be written only when:

1. CRS is known and normalised (target stored in WGS84 EPSG:4326 for
   geographic columns or EPSG:32613 for UTM Zone 13N collars).
2. Units are present and normalised (`value_ppm` for assays, depths in
   metres, lengths in metres).
3. For numeric ranges, `to_depth > from_depth` (enforced by CHECK on
   `silver.assays_v2` line 56 + `silver.lithology`).
4. For PDFs, parser run produced ≥ 1 `silver.report_pages` row.
5. `bronze.provenance` row exists for the target (verified post-hoc by
   `nightly_ingestion_integrity` Hatchet workflow).

## 7. Human review gates

For drill-data uploads (CC-01 Item 1 flow):

- Parser confidence < threshold → row routed to `silver.review_queue`
  with `lifecycle='pending'`.
- UI: [DrillReview.tsx](../../../resources/js/Pages/Foundry/DrillReview.tsx).
- Approval transitions `lifecycle` through `in_review → decided → committed`.
- Rejection transitions to `archived` and emits an audit ledger row.
- The silver target row is **not** materialised until lifecycle reaches
  `committed`.

## 8. Reprocessing semantics

- Re-running a parser against the same sha256:
  - **Silver rows are not duplicated** — the upsert key is
    `(workspace_id, parser_name, source_file_sha256, source_row)` or the
    equivalent natural key per table.
  - **`bronze.provenance` gets a new row** every run with the new
    `parser_version` — historical lineage is preserved.
- Re-processing different content under the same logical name produces a
  new sha256, hence a new silver row, hence a new bronze object — no
  collision possible.

## 9. Deletion / retention

- Bronze objects: never deleted by the application. Storage tiering moves
  them across buckets, not out of existence. Operator-initiated deletes
  are audit-logged.
- Silver rows: cascade-deleted only on workspace deletion. Project deletes
  default to SET NULL on the project FK; row stays for replay.
- `audit.audit_ledger`: retained 24 months (pg_partman policy
  [phase0/20-layer-b-audit-ledger.sql:67-83](../../../database/raw/phase0/20-layer-b-audit-ledger.sql)).
- Qdrant points: cascade-deleted via the outbox dispatcher when a silver
  passage is deleted.
- Neo4j nodes: same.

## 10. Outbox fan-out

The outbox pattern guarantees write durability across stores:

1. Silver write + `outbox.pending_propagations` insert happen in the same
   transaction.
2. `outbox_dispatcher` Hatchet workflow polls `FOR UPDATE SKIP LOCKED`
   every minute.
3. For each pending row, it calls the appropriate target dispatcher:
   - Qdrant: upsert/delete the corresponding point.
   - Neo4j: upsert/delete the corresponding node/edge.
   - SeaweedFS: copy-on-write where a mirror is needed.
4. Each attempt is recorded in `outbox.propagation_attempts`. Three
   transient failures → dead-lettered.
5. Idempotency is per-target — replay is safe.

## 11. Tests proving lineage + tenant isolation + replayability

Required test coverage for any new silver/gold table:

1. **Lineage round-trip** — write a silver row, assert `bronze.provenance`
   has a paired row with `target_id` matching the silver PK.
2. **Workspace fence** — write rows in two workspaces; with
   `app.workspace_id` set to A, assert workspace B rows are invisible.
3. **Replay** — given the bronze object + `parser_run_id`, re-run the
   parser produces a bit-identical silver row (for deterministic
   parsers) or an equivalent row with matching natural key (for
   probabilistic parsers).
4. **MVT determinism** (if the table backs a Martin function) — pgTAP
   golden snapshot test ([database/tests/pgtap/10_golden_mvt_snapshots.sql:152](../../../database/tests/pgtap/10_golden_mvt_snapshots.sql)).
5. **RLS coverage** — included by `WorkspaceRlsCoverageTest`
   ([tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php](../../../tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php)).

## 12. Open gaps tracked here

| Item | State | Action |
|---|---|---|
| `bronze.upload_files` | Referenced in earlier prose; **does not exist in code**. The upload flow writes `bronze.ingest_runs` + `bronze.ingest_manifest` + the SeaweedFS object. | Either land a `bronze.upload_files` migration that holds per-upload metadata distinct from a run, OR remove every reference. Recommendation: remove — the existing two tables already cover the use cases. |
| `bronze.raw_samples` | Does not exist (drillhole bronze is per-kind: `raw_assay_submissions`, `raw_lithology_logs`, `raw_surveys`, `raw_geophysical_runs`, `raw_collar_entries`). | Remove the generic name from all docs; it was never meant. |
| `silver.entities` | Does not exist as a single table. Entity bag-of-rows live in `workspace.entities` (workspace-scoped) + derived `:Entity` nodes in Neo4j. | Replace `silver.entities` references with `workspace.entities` everywhere. |
| `silver.lithology_intervals` | Does not exist. Interval data lives in `silver.lithology` (new, with generated `interval_length` GENERATED ALWAYS AS …) and the legacy `silver.lithology_logs`. | Remove all references; clarify in Ch 03 which one to read. |
| `silver.report_pages` / `report_figures` / `report_tables` | All three present; column set has drifted across migrations. | Land a one-shot consolidation migration that re-asserts the column contract + add a pgTAP shape test. |
| `gold.significant_intersections` | ✅ **Persisted table** — created by [2026_05_20_060700](../../../database/migrations/2026_05_20_060700_create_gold_drillhole_tables.php). The Dagster `silver_to_gold/significant_intersections` asset upserts. Earlier "in-flight inside the MVT function" wording was wrong. | Closed. |
| `bronze.manifest` vs `bronze.ingest_manifest` | Both exist. `bronze.ingest_manifest` (Phase A) is the per-file manifest inside an ingest run. `bronze.manifest` (May 25 ingest UI track) is a newer parallel surface. | Decide whether to migrate readers from `manifest` to `ingest_manifest` or rename one. Until decided, document both. |
