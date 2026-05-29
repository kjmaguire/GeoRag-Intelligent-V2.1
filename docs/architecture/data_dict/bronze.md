# Schema `bronze` — Data Dictionary (skeleton)

See [Ch 03 §5](../manual/03-schemas.md) for the curated reference, and
[Appendix A](../appendix/A-medallion-contract.md) for the bronze→silver
contract.

## Tables

| Table | Created by | Purpose | Live? |
|---|---|---|---|
| `bronze.provenance` | [2026_04_18_130000](../../../database/migrations/2026_04_18_130000_create_bronze_provenance_table.php) | Per-silver-row source mapping (target_schema/table/id → source_file/page/row + parser meta) | Live |
| `bronze.ingest_runs` | [2026_05_14_130000](../../../database/migrations/2026_05_14_130000_create_bronze_ingest_manifest.php) | One row per user-triggered ingest action | Live |
| `bronze.ingest_manifest` | same | One row per file inside an ingest run; carries TIFF/cluster metadata | Live |
| `bronze.ingest_triage_samples` | same | OCR samples + SME labels per ingest | Live |
| `bronze.raw_assay_submissions` | [2026_05_20_060000](../../../database/migrations/2026_05_20_060000_create_bronze_drillhole_tables.php) | Bronze for assay CSV/XLSX | Live |
| `bronze.raw_lithology_logs` | same | Bronze for lithology CSV | Live |
| `bronze.raw_surveys` | same | Bronze for downhole survey CSV | Live |
| `bronze.raw_geophysical_runs` | same | Bronze for downhole geophysics | Live |
| `bronze.raw_collar_entries` | same | Bronze for collar CSV | Live |
| `bronze.source_files` | same | Per-file SHA256 + metadata for drillhole flow | Live |
| `bronze.manifest` | [2026_05_25_020540](../../../database/migrations/2026_05_25_020540_create_bronze_manifest.php) | May-25 ingest UI track manifest (parallel to `bronze.ingest_manifest`) | Live (consolidation pending — see [Appendix A §12](../appendix/A-medallion-contract.md#12-open-gaps-tracked-here)) |

## Tables that do NOT exist (despite old references)

- `bronze.upload_files` — never created. Use `bronze.ingest_runs` +
  `bronze.ingest_manifest`.
- `bronze.raw_samples` — never created. Bronze is per-kind (assay /
  lithology / surveys / collars).

## Triggers

- `provenance_autofill_workspace_id_trg` (BEFORE INSERT on `bronze.provenance`) — auto-fills `workspace_id` from the target silver row. See [Ch 03 §10](../manual/03-schemas.md).

## RLS

- All bronze tenancy tables enabled RLS in [2026_05_25_170825](../../../database/migrations/2026_05_25_170825_enable_rls_on_bronze_tenancy_tables.php).
- `workspace_id` is NOT NULL after [2026_05_25_183115](../../../database/migrations/2026_05_25_183115_tighten_bronze_tenancy_columns_to_not_null.php).
