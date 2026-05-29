# GeoRAG Ingestion Pipeline — Phase A Audit Report
<!-- Produced by: data-engineer agent (Claude Sonnet 4.6) -->
<!-- Date: 2026-04-20 (Module 3 Phase A) -->
<!-- Authority: iterative read of src/dagster/, database/migrations/, ops/ source files -->
<!-- Global invariants 4, 12, 14 applied throughout -->
<!-- Status: READ-ONLY PASS — no code, migrations, configs, or services were modified -->

---

## Subsection IDs

| ID | Topic | Result |
|---|---|---|
| ING-01 | Dagster asset graph inventory | Findings |
| DAG-01..04 | Asset graph detail findings | Findings |
| PARSE-01..08 | Parser coverage | Mixed |
| CRS-01..05 | CRS detection | Partial |
| DSV-01..04 | Desurvey pipeline | Critical gap |
| PASG-01..04 | Passage store | Critical gap |
| EVID-01..04 | Evidence-model pre-migration | Not started |
| DVER-01..03 | Data-version contract | Not started |
| CORP-01..08 | Validation corpora | Mixed |
| IDEMP-01..03 | Idempotency | Partial |
| POSTING-01 | Post-ingest tuning invocation | Finding |

---

## A1 — Dagster Asset Graph Inventory

See `ops/audit/2026-04-20-ingestion-asset-graph.md` for the full table and Mermaid diagram. Summary of audit findings against the spec:

**DAG-01 — CRITICAL: Zero `AssetCheckSpec` registrations.**
No `@asset_check` decorator, no `AssetCheckSpec`, no `checks=` parameter exists anywhere in `src/dagster/georag_dagster/`. The spec (§6 Phase B B1) requires at least one blocking check per Silver/Gold/Index asset. Without checks, there is no gate preventing bad data from promoting to Silver or triggering a `data_version` bump. This is the highest-priority structural gap.

**DAG-02 — HIGH: `silver_raster` has no Bronze parent.**
`silver_raster` is registered in `definitions.py` with no `deps=` that reference a `bronze_raster` asset. A `bronze_raster` asset does not exist. The raster path reads directly from MinIO via config, bypassing Bronze asset lineage entirely. This breaks the medallion invariant: Silver must trace to Bronze.

**DAG-03 — HIGH: MinIOResource uses minio-py vendor SDK.**
`resources.py` lines 20–21 import `from minio import Minio` and `from minio.error import S3Error`. The entire `MinIOResource` class calls `fput_object`, `bucket_exists`, `make_bucket`, `list_objects`, `stat_object`, `put_object` — all minio-py-specific calls. Addendum §02a forbids vendor-specific API surfaces. Pre-approved for Phase B refactor per `ops/backlog/module-3-intake.md`.

**DAG-04 — MEDIUM: Sensor docstring names old bucket; live code is correct.**
`definitions.py` line 349 comment reads `"Polls the georag-bronze MinIO bucket"`. The live runtime code on line 371 uses `bucket = "bronze"` (correct post-Module-2 rename). Docstring-only drift; no functional impact. Flag for Module 10 doc sweep.

**DAG-05 — MEDIUM: `silver_surveys`, `silver_lithology`, `silver_samples`, `silver_well_logs`, `silver_spatial`, `silver_xlsx`, `silver_seismic`, `silver_xyz` are graph dead-ends.**
Eight Silver assets have no Gold or Index downstream. Their parsed data lands in PostgreSQL tables but is never embedded to Qdrant, never fed to Neo4j, and never surface to retrieval. Per the medallion architecture these must eventually flow to Index. This is expected for a work-in-progress pipeline but must be resolved before V1.

**DAG-06 — LOW: All three schedules are STOPPED.**
No schedules are running. `full_ingest_schedule` targets `AssetSelection.all()` which would also re-trigger Public Geoscience assets. This coupling needs review — the spec implies the main ingest schedule should target private-project assets only.

---

## A2 — Parser Coverage Audit

### CSV Collar (`parsers/csv_collar.py` — `CollarParseResult`)

**PARSE-01 — PASS (with notes)**
- Parser location: `src/dagster/georag_dagster/parsers/csv_collar.py`, class `CollarParseResult`
- Pydantic IR: not Pydantic — uses `@dataclass` (`CollarParseResult`). Fields: `records`, `total_rows`, `valid_rows`, `skipped_rows`, `unmapped_columns`, `column_map`, `skipped_details`, `warnings`, `detected_encoding`, `dip_convention`, `provenance`. Provenance dict includes `source_file_sha256`, `parser_name`, `parser_version`.
- Failure-row handling: GOOD — each failed row appends a structured dict to `skipped_details` with `row`, `code`, `reason`, `expected`, `actual`, `suggestion`. Count reported in `MaterializeResult` metadata.
- Unit tests: `test_csv_collar_parser.py` present, tests collar count, easting checksum, field completeness, unmapped-column count.
- Corpus fixture: `tests/fixtures/sample_collars.csv` — single file, expected-output values hardcoded in test constants. Sufficient for regression but sparse (one happy-path file). No malformed/edge-case files.
- Note: `@dataclass` not Pydantic — schema validation is manual range/type checks, not Pydantic validators. This is acceptable but differs from the spec's "Pydantic IR" language.

### CSV Survey, Lithology, Sample (`parsers/csv_survey.py`, `csv_lithology.py`, `csv_sample.py`)

**PARSE-02 — PASS (same pattern as collar)**
- Same `@dataclass` + row-skip pattern as CSV collar. Fixtures exist (`sample_surveys.csv`, `sample_lithology.csv`, `sample_samples.csv`). Tests cover basic counts. No malformed-input fixtures.

### LAS 2.0 (`parsers/las_parser.py` — `LasParseResult`)

**PARSE-03 — PASS**
- `LasParseResult` dataclass: `well_name`, `company`, `field_name`, `location`, `las_version`, `curves` (`list[LasCurve]`), `depth_curve_name`, `total_curves_in_file`, `skipped_curves`, `parse_quality_pct`, `skipped_details`, `provenance`.
- Failure-row handling: skipped curves appended to `skipped_details` with curve name and reason. Parse quality = parsed/total curves.
- Tests: `test_raster_parser.py` is raster-only; no dedicated LAS test file found. LAS tested indirectly via integration.
- Corpus fixture: `tests/fixtures/well_logs/PLS-22-08_gamma_resistivity.las` — single file. No expected-output fixture JSON.
- **CORP-01 gap: no expected-output fixture for LAS; no malformed LAS file.**

### Shapefile / GeoPackage / Other Vector (`parsers/spatial_parser.py` — `SpatialParseResult`)

**PARSE-04 — PASS (with scope note)**
- Supports: `.shp`, `.geojson`, `.json`, `.gpkg`, `.kml`, `.kmz`, `.gml`, `.gpx`, `.dxf`, `.dgn`, `.gdb`, `.fgb` — well beyond V1 spec scope.
- KML/KMZ: parser is implemented and can read KML. V1-roadmap spec says "do not implement" KML in Module 3. The parser exists but is not gated. Flag: KML parser present but no spec approval.
- GDB (FileGDB): parser dispatches to pyogrio's OpenFileGDB driver. This is read-only via GDAL — distinct from Geosoft GDB (a proprietary format). The deferred_capabilities list correctly signals `filegdb_domains/subtypes/relationship_classes`. No Geosoft GDB parser exists (correct — deferred).
- CRS handling: source CRS captured, reprojected to EPSG:4326 before WKT extraction. `_score_crs_confidence` helper compares geometry bbox against PyProj CRS area_of_use.
- Failure-row handling: empty/null geometries logged, counted in `empty_geom_skipped`, never silently dropped.
- Tests: `test_spatial_parser_sprint4.py`, `test_spatial_crs_confidence.py` present.
- Corpus fixtures: `pls_alteration_anomalies.geojson`, `pls_property_boundary.geojson`, `test_multilayer.gpkg`. No Shapefile fixture (the primary V1 format), no KML corpus file.
- **CORP-02 gap: no Shapefile fixture; no expected-output fixture JSON for any spatial format.**

### GeoTIFF (`parsers/raster_parser.py` — `RasterParseResult`)

**PARSE-05 — PASS (COG normalization absent)**
- `RasterParseResult` dataclass: comprehensive — driver, format, width, height, band_count, crs, crs_confidence, pixel_size_x/y, bounds (native), bounds_4326, bands (list of `RasterBandStats`), is_cog, has_alpha, compression, tags, warnings, provenance.
- CRS handling: reads from rasterio `src.crs`, converts to EPSG string where possible, reprojects bounds to 4326, scores confidence with PyProj area_of_use.
- COG detection: `is_cog = tiled AND has overviews` — detection only. **COG normalization (addendum §02b) — `rio cogeo create` conversion — is entirely absent.** The `silver_raster` asset does not call `rio cogeo create`, does not write to `bronze-raster`, does not emit `metadata.json`. The `bronze-raster` bucket exists (Module 2 created it empty) but the pipeline never writes to it.
- `rio-cogeo` is absent from `pyproject.toml` and `uv.lock`.
- `wellpathpy` is absent from `pyproject.toml` and `uv.lock`.
- Tests: `test_raster_parser.py`, `test_silver_raster_asset.py` present.
- Corpus fixtures: `test_small.tif`, `test_no_crs.asc` present.
- **CORP-03 gap: no multi-band GeoTIFF corpus; no COG corpus file; no expected-output fixture JSON.**

### PDF / NI 43-101 (`parsers/pdf_report.py`)

**PARSE-06 — PASS (RAGFlow integration absent)**
- Uses `unstructured` (fast strategy) with pdfplumber fallback — NOT RAGFlow. The spec (§6 scope) states NI 43-101 PDFs parse via RAGFlow; do not implement a second PDF parser. The current implementation IS a second PDF parser. RAGFlow is deferred to M2 but the note in the module spec says RAGFlow is the V1 parser for PDFs; the custom parser here is an alternative that predates that decision.
- `ReportParseResult` dataclass (not Pydantic): `sections`, `parse_quality_pct`, `metadata`, `warnings`, `provenance`.
- Failure-row handling: sections that cannot be detected are counted; parse quality = detected/expected sections.
- Tests: `test_pdf_resource_tables.py`, `test_pdf_warnings.py`, `test_pdf_page_languages.py`, `test_pdf_two_column_layout.py` present.
- Corpus fixture: `tests/fixtures/reports/PLS-2024-Technical-Report.pdf`. Single PDF, no expected-output fixture JSON.
- **CORP-04 gap: no expected-output fixture JSON; no scanned/low-quality PDF.**
- **PARSE-06 scope drift: custom PDF parser exists where spec says use RAGFlow. Needs Kyle decision: keep custom parser as fallback or remove in favour of RAGFlow-only.**

### DOCX (`parsers/docx_parser.py`)

**PARSE-07 — PASS**
- Handles `.docx` (python-docx) and `.doc` (LibreOffice subprocess). Imports and reuses helpers from `pdf_report.py`. Degrades gracefully if LibreOffice absent.
- `DocxParseResult` mirrors `ReportParseResult` for downstream compat.
- Tests: `test_docx_parser.py` present.
- Corpus: no `.docx` fixture file found in `tests/fixtures/`.
- **CORP-05 gap: no DOCX corpus fixture file.**

### XLSX / XLS (`parsers/xlsx_parser.py`)

**PARSE-08 — PASS**
- Handles both `.xlsx` and legacy `.xls`.
- Tests: `test_xls_parser.py` present.
- Corpus fixtures: `tests/fixtures/excel/PLS_collars.xlsx`, `tests/fixtures/excel/PLS_collars_legacy.xls` present.

### SEG-Y (`parsers/segy_parser.py`)

**V1-roadmap scope check:** `segyio` is in `pyproject.toml` as a pinned dependency (`segyio>=1.9`, resolved to a specific wheel in `uv.lock`). A `segy_parser.py` file and `bronze_seismic` / `silver_seismic` assets exist. A corpus fixture `tests/fixtures/seismic/test_2D_line.sgy` exists. The module spec states SEG-Y is deferred (V1 roadmap — do not implement). The implementation is present. **This needs Kyle's decision: is SEG-Y promoted to V1 scope, or should these assets be removed?** It is NOT a regression to have them — they are tested — but they are out of scope per the written spec.

---

## A3 — CRS Detection §04b Audit

### Step 1 — Metadata parsing from file headers

**CRS-01 — PARTIAL**
- CSV: no embedded CRS (correct — CSVs have no CRS header). Step 1 is documented as N/A for CSV in `silver.py`.
- LAS: no CRS parsing from the `~W` section (LAS files can carry `~C` or well location metadata but these are not parsed for CRS). Step 1 is implicitly skipped for LAS.
- Shapefile: pyogrio/GeoPandas reads the `.prj` sidecar — this IS step 1. Missing `.prj` emits `prj_missing` warning. GOOD.
- GeoPackage: EPSG read from OGR layer CRS. GOOD.
- GeoTIFF: `src.crs` read from rasterio. GOOD.
- KML: EPSG:4326 assumed if absent (OGC default). GOOD.

### Step 2 — Heuristic from coordinate ranges

**CRS-02 — WEAK**
- Implemented only for CSV collar in `silver.py` (`_detect_source_epsg`). The heuristic checks only the first record's easting/northing and applies a single UTM range test (`100_000 <= easting <= 999_999 AND 1_000_000 <= northing <= 10_000_000`). If matched, returns `PROJECT_EPSG` (32613). If not matched, also returns `PROJECT_EPSG` (fallback). The heuristic cannot distinguish between different UTM zones (e.g., Zone 12N vs Zone 13N) — it always returns Zone 13N regardless.
- Not implemented for LAS, XLSX, or SEG-Y. For those formats the heuristic is never invoked.
- Domain-reasonable for Athabasca Basin: the bbox check in `silver.py` constrains easting 400,000–650,000 and northing 6,100,000–6,400,000 which is geologically reasonable for the Basin in Zone 13N.

### Step 3 — Bbox validation

**CRS-03 — PARTIAL**
- Implemented for CSV collar in `silver.py`. `PROJECT_BBOX` is hardcoded (easting 400,000–650,000, northing 6,100,000–6,400,000). Bbox-rejected rows are logged with reason and counted in `bbox_rejected_rows` metadata. GOOD.
- For spatial formats: `_score_crs_confidence()` checks against PyProj `area_of_use.bounds` — this is a CRS-validity check, not a project-specific bbox validation. No `projects.bbox` column is read from the database. The spec (§A3) asks where the project bbox is sourced from — answer: it is hardcoded in Python, not read from `projects.bbox`.
- **CRS-03 gap: `projects.bbox` column does not exist in any migration** (confirmed: `create_projects_table.php` has no `bbox` column; `add_dashboard_fields_to_projects.php` adds only `status` and `slug`). The spec requires bbox to come from `projects.bbox`.

### Step 4 — Transform to project CRS + store original CRS

**CRS-04 — PARTIAL**
- CSV collar: geometry inserted as `ST_SetSRID(ST_MakePoint(easting, northing), PROJECT_EPSG)` with `source_crs` emitted as `MaterializeResult` metadata. The original CRS is NOT stored in a database column — it lives only in the Dagster run metadata. No `source_crs` column in `silver.collars` (confirmed from migration `create_collars_table.php`).
- Spatial formats: source CRS stored in `SpatialParseResult.source_crs` field, which is inserted into `silver.spatial_features.source_crs` (column present in the Silver asset SQL).
- **CRS-04 gap: `silver.collars` has no `source_crs` column.** The audit trail for original CRS is not complete for the primary data type.

### Default CRS

**CRS-05 — PASS**
- `PROJECT_EPSG = 32613` in `assets/silver.py`. Consistent with §04b. No other default CRS value found in the codebase.

---

## A4 — Desurvey Pipeline Audit

**DSV-01 — CRITICAL: `silver.drill_traces` does not exist.**
No migration creates `silver.drill_traces`. No Dagster asset named `silver_drill_traces` exists. The table, asset, and all associated SQL are absent.

**DSV-02 — HIGH: Desurvey math exists but is not wired to a pipeline asset.**
`parsers/_survey_interp.py` contains an in-house minimum curvature implementation (`minimum_curvature()` + `SurveyStation` + `XYZ` dataclasses). It is tested in `test_survey_interpolation.py` and covers: 0-survey collar (returns empty list), 1-survey collar (raises `ValueError` per tests — need to verify the spec says "vertical LINESTRING Z + warning", not raise), duplicate depths (implicit in monotonicity check), invalid azimuth/dip (raises `ValueError`), high dogleg (no explicit test found for the 15°/30m warning threshold).

**DSV-03 — HIGH: `wellpathpy` not adopted.**
The spec (§6 Phase B B4) mandates using `wellpathpy.minimum_curvature` (the reference implementation) and pinning it. The current codebase uses an in-house implementation instead. `wellpathpy` is absent from `pyproject.toml` and `uv.lock`. Flag for Phase B decision: keep in-house or migrate to `wellpathpy`.

**DSV-04 — MEDIUM: `projects.crs_epsg` column absent.**
The spec (§A4) asks whether `projects.crs_epsg` exists. The migration `create_projects_table.php` has `crs_datum VARCHAR(50) DEFAULT 'EPSG:32613'` — a string field, not a separate `crs_epsg INTEGER` column. This may be equivalent but the column name differs from the spec reference. The desurvey asset (when built) will need to know which column to read.

---

## A5 — Passage Store §10p-i Audit

**PASG-01 — CRITICAL: `document_passages` table does not exist.**
No migration in `database/migrations/` creates a `document_passages` table. Confirmed by exhaustive search across all 36 migration files — no match for `document_passages`, `passage_id`, or `text_hash`. The table specified in §10p-i does not exist.

**PASG-02 — CRITICAL: Embedding linkage is direct to `silver.reports`, not via `document_passages`.**
`index_reports.py` reads from `silver.reports`, splits text into chunks, embeds them, upserts to Qdrant, and writes `point_id_strings` back to `silver.reports.embedding_ids` (a `text[]` array column). There is no intermediate `document_passages` table and no stable `passage_id`. Re-ingesting the same document will generate new point IDs unless the Qdrant upsert (by deterministic UUID from content hash) re-uses the same ID. The upsert strategy needs inspection to confirm stability.

**PASG-03 — HIGH: No `text_hash` SHA-256 computed for passage stability.**
The spec requires each passage to have a `text_hash` (SHA-256) so that re-ingestion of unchanged text produces the same `passage_id`. The current `index_reports.py` chunks text by character position and generates UUIDs based on `hashlib.sha256(chunk_text.encode()).hexdigest()` for the Qdrant point ID — this is close to the right approach but is not stored in a `document_passages` table with a stable `passage_id` FK.

**PASG-04 — HIGH: No old-revision retention.**
Without `document_passages` and `document_revisions` tables, there is no mechanism for retaining old passage IDs for backreference when a document is re-ingested with changed text.

---

## A6 — Evidence-Model Pre-Migration Audit (Addendum §04j)

**EVID-01 — NOT STARTED: None of the three new tables exist.**
- `document_revisions` — does not exist in any migration.
- `evidence_items` — does not exist in any migration.
- `structured_record_lineage` — does not exist in any migration.

**EVID-02 — NOT STARTED: `answer_citation_items.evidence_id` column absent.**
No `answer_citation_items` table exists at all in the migration stack. The table is referenced in the architecture doc (§10p-i) but has never been created. This means:
- Zero existing `answer_citation_items` rows to backfill (B8.3 backfill is a no-op for the pre-existing rows).
- The FK dependency map is empty — no callers currently write to this table.

**EVID-03 — NOT STARTED: No callers in FastAPI or Laravel write to citation tables.**
Search of `src/fastapi/app/` finds no references to `answer_citation_items`, `document_passages`, or `evidence_items`. The orchestrator in `orchestrator.py` references `answer_runs` in a comment but does not write to it. The citation infrastructure is entirely absent at the application layer.

**EVID-04 — Phase B migration plan (draft):**

This is a clean-slate migration — no existing rows or FKs constrain the order. Proposed B8.1–B8.5 sequence given actual state:

| Step | Action | Pre-condition | Reversible until |
|---|---|---|---|
| B8.1a | Create `document_passages` (§10p-i) | None | B8.7 enable |
| B8.1b | Create `answer_citation_items` (§10p-i) | `document_passages` exists | B8.7 enable |
| B8.1c | Create `answer_runs` (§10p-i / §09b lifecycle) | None | B8.7 enable |
| B8.2a | Create `document_revisions` (§04j) | `document_passages` exists | B8.7 enable |
| B8.2b | Create `evidence_items` (§04j) | `document_passages` exists | B8.7 enable |
| B8.2c | Create `structured_record_lineage` (§04j) | `evidence_items` exists | B8.7 enable |
| B8.3 | Add `answer_citation_items.evidence_id` FK column (nullable) | `evidence_items` exists | Step B8.5 |
| B8.4 | Backfill `document_revisions` from existing `silver.reports` | Step B8.2a | DELETE FROM |
| B8.5 | Wire `index_reports` to write `document_passages` + Qdrant point linkage | Steps B8.1a, B8.4 | Disable flag |
| B8.6 | Ingestion pipeline emits `evidence_items` + `structured_record_lineage` for structured evidence types | Step B8.5 | Disable flag |
| B8.7 | Enable: `answer_citation_items.evidence_id` made NOT NULL for new rows (application-layer constraint, not DB) | Module 6 ready | Cannot revert without data loss |

**Deploy-order interlock with Module 6:** Module 3 owns B8.1–B8.6 (schema + data population). Module 6 owns the citation-attachment code that reads `evidence_items` and populates `answer_citation_items.evidence_id`. B8.7 enable requires Module 6 to be ready. Do NOT block Module 3 Phase B on Module 6 — complete through B8.6, stop, surface to Kyle before B8.7.

**Senior-reviewer gate applies to B8.2a–B8.2c DDL and to B8.7 enable.** Each step is its own PR.

---

## A7 — Data-Version Audit (Addendum §05d)

**DVER-01 — CRITICAL: `workspaces.data_version` and `projects.data_version` columns do not exist.**
No migration adds `data_version BIGINT` to `silver.projects` or any `workspaces` table. The column is absent from `create_projects_table.php` and all subsequent `projects`-table migrations. No `workspaces` table exists at all in the migration stack — GeoRAG does not yet have a workspace concept in the database schema.

**DVER-02 — CRITICAL: No increment logic exists.**
Confirmed: no code path in `src/dagster/` or `src/fastapi/` or `src/laravel/` references `data_version`. No trigger, no application-layer `UPDATE workspaces SET data_version = data_version + 1`. The contract defined in addendum §05d is entirely unimplemented.

**DVER-03 — MEDIUM: Retrieval cache key construction does not reference `data_version`.**
Module 4 owns retrieval cache keys; this is logged as a cross-module coordination note. The retrieval layer cannot honour the `data_version` freshness contract until Module 3 Phase B populates the columns.

**Martin tile function signature:** The Martin tile functions return `bytea` (current, per Module 2 audit). Addendum §05d / §02b requires extension to `(bytea, etag_hash)`. This is Module 8 work; noted here for coordination.

---

## A8 — Validation Corpora Audit

| Format | Fixture directory | Files present | Expected-output fixture | Coverage verdict |
|---|---|---|---|---|
| CSV (collar) | `tests/fixtures/` | `sample_collars.csv` | Hardcoded in test constants | PARTIAL — 1 file, no malformed variant |
| CSV (survey/litho/sample) | `tests/fixtures/` | 3 files | Hardcoded in test constants | PARTIAL |
| LAS 2.0 | `tests/fixtures/well_logs/` | 1 file | None | MISSING expected-output fixture |
| Shapefile | `tests/fixtures/spatial/` | None | None | MISSING — primary V1 vector format |
| GeoPackage | `tests/fixtures/spatial/` | `test_multilayer.gpkg` | None | MISSING expected-output fixture |
| GeoTIFF | `tests/fixtures/spatial/` | `test_small.tif`, `test_no_crs.asc` | None | MISSING COG variant; no expected-output |
| PDF (NI 43-101) | `tests/fixtures/reports/` | 1 PDF | None | MISSING expected-output fixture |
| DOCX | None found | None | None | MISSING entirely |
| XLSX | `tests/fixtures/excel/` | 2 files (xlsx + xls) | None | MISSING expected-output fixture |
| SEG-Y | `tests/fixtures/seismic/` | `test_2D_line.sgy` | None | MISSING expected-output fixture |
| XYZ | `tests/fixtures/xyz/` | `PLS_magnetics.xyz` | None | MISSING expected-output fixture |

**CORP-01 through CORP-08: Every V1 format is missing expected-output fixture JSON files.** The fixtures directory contains real-file inputs but no golden-output files (expected collar counts, expected coordinate checksums, expected parse quality ratios). Without these, the corpus cannot detect parser regressions.

**CORP-09 — Phase C blocker:** The module spec states that no quality percentage claim can be made without a named validation corpus per Global Invariant 3. All quality claims (parse_quality_pct fields, CRS detection accuracy) are currently unvalidated targets. Phase C corpus assembly is a blocker for any quality claim.

---

## A9 — Ingestion Idempotency Audit

**IDEMP-01 — PARTIAL: Bronze idempotency is size-based, not hash-based.**
`bronze_collars` checks `stat["size"] == file_size` to detect existing objects (line ~110 of `assets/bronze.py`). This is a pragmatic guard but would miss a file whose content changes with the same byte count. The SHA-256 is computed and emitted as metadata but is NOT stored in MinIO object user-metadata (no `x-amz-meta-sha256` header on `fput_object`). A re-run with a different-content same-size file would skip the re-upload. Since Bronze is supposed to be immutable, this matters for detecting accidental overwrites.

**IDEMP-02 — PASS: Silver is idempotent via `ON CONFLICT ... DO UPDATE`.**
All Silver insert statements use `ON CONFLICT (project_id, hole_id) DO UPDATE SET ...` (collars) or equivalent unique-key upsert patterns. Re-running the same Bronze file produces the same Silver output.

**IDEMP-03 — NOT VERIFIED: Parser-version replay.**
No mechanism exists to tag ingestion runs with a `parser_version` and replay only Silver-and-downstream from Bronze when the parser changes. The `bronze.provenance` table stores `parser_version` per-row, but there is no Dagster asset or job that filters by "Bronze rows ingested with parser_version < current, re-run Silver". This is Phase B work.

---

## A10 — Post-Ingest Tuning Invocation

**POSTING-01 — FINDING: `post-ingest-tune.sql` is NOT called from any Dagster asset.**

The script exists at `ops/postgis/post-ingest-tune.sql` (Module 2 deliverable, correct). However:

1. No Dagster asset references this file path.
2. No `subprocess.run("psql ... -f ops/postgis/post-ingest-tune.sql")` call exists in any asset.
3. Individual Silver assets implement their own inline GIST index creation + ANALYZE (e.g., `silver.py` runs `POSTLOAD_SQL` with `CREATE INDEX IF NOT EXISTS` + `ANALYZE silver.collars`). This is the per-asset local tuning pattern — it is NOT the same as the post-batch CLUSTER + MV refresh from `post-ingest-tune.sql`.
4. `silver_spatial.py` and `silver_xyz.py` do call `CLUSTER silver.spatial_features USING idx_spatial_features_geom` inline — this is the closest to the spec requirement but is per-asset, not a final pipeline step.

**Gap:** The pipeline does not call `post-ingest-tune.sql` as the final step after all asset checks pass (there are no asset checks to pass). The `CLUSTER` + `ANALYZE` + MV refresh invocation is fragmented across individual assets with inconsistent coverage. `silver.reports`, `silver.well_logs`, `silver.surveys`, `silver.lithology_logs`, `silver.samples` do NOT run CLUSTER at all.

---

## Module 10 Doc Sweep — New Drift Found

Additions to `ops/backlog/module-10-doc-sweep.md` (for Module 10 to close; NOT modified in this audit):

1. **`minio_upload_sensor` docstring** references `"georag-bronze MinIO bucket"` — stale after Module 2 bucket rename. Code is correct; docstring is wrong.
2. **`projects.crs_epsg` vs `projects.crs_datum`** — arch doc §04b and §A4 reference `projects.crs_epsg` (integer); live migration has `projects.crs_datum VARCHAR(50)`. Different name and type. Needs Kyle decision: rename column or update doc reference.
3. **`silver.collars` missing `source_crs` column** — arch §04b step 4 requires original CRS stored alongside transformed coordinates. No such column in the migration. Module 3 Phase B must add it (coordinate with Kyle per stop-and-ask rules — §04e adjacent).
4. **`workspaces` table absent** — addendum §05d references `workspaces.data_version`. No `workspaces` table exists in the database. The multi-tenant workspace concept is not yet in the schema. This is a significant gap that goes beyond Module 3 alone (Module 9 scope for RBAC, but Module 3 must write `workspace_id` on every new row).
5. **KML/KMZ parser is implemented** despite being in the V1 roadmap (deferred) list. Not a violation per se, but the spec's "do not implement" language creates ambiguity. Needs Kyle sign-off on whether KML is promoted to V1 or the parser removed.

---

## Surface to Kyle — Critical / High Findings & Proposed Phase B Sequence

### Critical findings (block Phase B start)

1. **DAG-01 / CRITICAL** — Zero asset checks. No blocking gates between stages. `data_version` bump cannot be safe without checks.
2. **DSV-01 / CRITICAL** — `silver.drill_traces` asset and table do not exist. Desurvey pipeline is absent entirely.
3. **PASG-01 / CRITICAL** — `document_passages` table does not exist. Passage store not implemented.
4. **DVER-01 / CRITICAL** — `workspaces.data_version` and `projects.data_version` columns do not exist. The `workspaces` table itself does not exist.
5. **EVID-01 / CRITICAL** — `document_revisions`, `evidence_items`, `structured_record_lineage` all absent. Evidence model not started.

### High findings (execute in Phase B)

6. **DAG-02 / HIGH** — `silver_raster` has no Bronze parent; Bronze raster lineage broken.
7. **DAG-03 / HIGH** — `MinIOResource` uses vendor minio-py SDK (pre-approved refactor).
8. **PARSE-05 / HIGH** — COG normalization (`rio cogeo create`) absent; `bronze-raster` bucket is empty; `rio-cogeo` not in dependencies.
9. **DSV-02 / HIGH** — Desurvey math exists but is not wired to any Dagster asset.
10. **DSV-03 / HIGH** — `wellpathpy` not adopted or pinned (in-house implementation used instead).

### Proposed Phase B sequence (accounting for dependencies)

```
B-Pre:  MinIOResource → boto3 refactor (pre-approved, low-risk, unblock B3)
B1:     AssetCheckSpec gates on existing Silver assets (collar_count_positive, parse_ratio)
B2:     Add silver.collars.source_crs column (Kyle approval — §04e adjacent)
        Add projects.crs_epsg column (Kyle approval) or rename crs_datum to crs_epsg
B3:     Add workspaces table + workspace_id to silver layer tables (Kyle approval — §04e + Module 9)
B4:     Add data_version to workspaces + projects tables
B5:     Wire data_version increment to ingestion committed-state transition
B6:     Create bronze_raster asset; wire silver_raster → bronze_raster parent
B7:     Add rio-cogeo to dependencies; implement COG normalization in silver_raster
B8.1a–c: Create document_passages, answer_citation_items, answer_runs (§10p-i) — senior-reviewer gate
B8.2a–c: Create document_revisions, evidence_items, structured_record_lineage (§04j) — senior-reviewer gate
B8.3:   Add answer_citation_items.evidence_id nullable FK
B8.4:   Backfill document_revisions from existing silver.reports rows
B8.5:   Wire index_reports to write document_passages + stable passage_id
B9:     Implement silver_drill_traces asset (decide: wellpathpy or pin in-house)
B10:    Asset check: trace count vs collar-with-surveys count
B11:    Corpus assembly — expected-output fixture JSON for every V1 format
B12:    End-of-pipeline post-ingest-tune.sql invocation asset
```

B8.5–B8.6 must coordinate with Module 6 (citation attachment uses evidence_items). Stop and surface to Kyle before B8.7 (enabling evidence_id as a required field).

---

## Evidence-Model Migration Plan Summary

**Current state:** Zero pre-existing rows in any citation table (tables do not exist). Migration is purely additive — no existing data to migrate for B8.1–B8.3.

**Tables to create (in order):**
1. `document_passages` — `passage_id UUID PK`, `document_id FK`, `workspace_id`, `text_hash CHAR(64)` (SHA-256), `chunk_text TEXT`, `embedding_id` (Qdrant point ID), `passage_number INT`, `created_at`, `updated_at`
2. `answer_runs` — per §09b lifecycle fields (pending Module 6 schema definition)
3. `answer_citation_items` — `citation_id UUID PK`, `answer_run_id FK`, `passage_id FK → document_passages`, `evidence_id FK → evidence_items` (nullable until B8.7), `created_at`
4. `document_revisions` — per §04j B8.1 field list
5. `evidence_items` — per §04j B8.1 field list with CHECK constraint (exactly one of passage_id, structured_ref, graph_edge_ref, map_feature_ref non-null)
6. `structured_record_lineage` — per §04j B8.1 field list

**Backfill strategy:** Since `answer_citation_items` table doesn't exist (0 rows), B8.3 backfill is trivial. B8.4 `document_revisions` backfill = one revision row per row in `silver.reports` (query `SELECT report_id, ... FROM silver.reports` and insert). Easily reversible via `DELETE FROM document_revisions WHERE superseded_by_revision_id IS NULL AND ingested_at < :cutoff`.

**Deploy-order interlock:** Module 3 owns through B8.6. Module 6 review required before B8.7. Module 9 must add workspace isolation enforcement concurrently with Module 3's `workspace_id` population.

---

## Data-Version State

- `workspaces.data_version`: **ABSENT** (no workspaces table exists)
- `projects.data_version`: **ABSENT** (column not in any migration)
- Current state: **0 / unimplemented** — no columns, no increment logic, no cache-key usage
- Both columns and the increment transaction must be created in Module 3 Phase B before any ingestion commit can be called "safe"

---

## Validation Corpora State

| Format | Input file(s) | Expected-output fixture |
|---|---|---|
| CSV collar | PRESENT (1 file) | PRESENT (hardcoded in test) |
| CSV survey/litho/sample | PRESENT (3 files) | PRESENT (hardcoded) |
| LAS | PRESENT (1 file) | MISSING |
| Shapefile | MISSING | MISSING |
| GeoPackage | PRESENT (1 file) | MISSING |
| GeoTIFF | PRESENT (2 files) | MISSING |
| PDF | PRESENT (1 file) | MISSING |
| DOCX | MISSING | MISSING |
| XLSX | PRESENT (2 files) | MISSING |
| SEG-Y | PRESENT (1 file) | MISSING |
| XYZ | PRESENT (1 file) | MISSING |

**Phase C blocker:** No format has a complete corpus (real input + expected-output fixture JSON with collar count, coordinate checksum, parse quality ratio). All quality claims remain targets until Phase C corpus is assembled.

---

## Confirmation of Hard Constraints

- No code, migration, Dockerfile, docker-compose, or running service was modified during this audit.
- No database schema modifications (CREATE / ALTER / DROP) were executed.
- No Dagster materializations were triggered.
- All findings derive from static file reads only.
- No files outside `ops/audit/` were written.

---

*Audit date: 2026-04-20. Produced by data-engineer agent (Claude Sonnet 4.6).*
*Architecture authority: georag-architecture.html §04, §04b, §04c, §04d, §04d-tile, §10, §10p-i + addendum v1.10 §04j, §05d.*

---

## Phase B1+B2 Close-Out (2026-04-20)

### Migration files created

| File | Purpose |
|---|---|
| `database/migrations/2026_04_20_100000_create_workspaces_and_data_version.php` | B1 DVER: workspaces table + data_version on workspaces + projects |
| `database/migrations/2026_04_20_110000_create_document_passages.php` | B2 PASG: silver.document_passages |
| `database/migrations/2026_04_20_120000_add_crs_epsg_to_projects.php` | Decision C: projects.crs_epsg + deprecation comment on crs_datum |

All three migrations executed successfully via `php artisan migrate --force`. Migration batch numbers: [11], [12], [13].

### workspaces table

- Created: `silver.workspaces` with `workspace_id UUID PK`, `name`, `slug UNIQUE`, `data_version BIGINT NOT NULL DEFAULT 0`, `created_at`, `updated_at`.
- Default workspace seeded: UUID `a0000000-0000-0000-0000-000000000001`, name "Default Workspace", slug "default". Row count: 1.
- Monotonic trigger `workspaces_data_version_monotonic` installed on `BEFORE UPDATE WHEN (data_version IS DISTINCT FROM OLD.data_version)`.

### projects table changes

- Added: `workspace_id UUID NULL FK → silver.workspaces(workspace_id) ON DELETE SET NULL`
- Added: `data_version BIGINT NOT NULL DEFAULT 0`
- Added: `crs_epsg INTEGER NULL` (Decision C)
- Column comment placed on `crs_datum` marking it deprecated 2026-04-20.
- Monotonic trigger `projects_data_version_monotonic` installed.
- Existing projects backfilled to default workspace: 2 rows updated (100% coverage, 0 remaining NULL).

### document_passages DDL

- Table: `silver.document_passages`
- Schema: passage_id UUID PK, document_id UUID NULL FK → silver.reports, workspace_id UUID NOT NULL FK → silver.workspaces, revision_number INTEGER, text TEXT, text_hash CHAR(64), ordinal INTEGER, embedding_id TEXT NULL, timestamps.
- Unique constraint: `(document_id, revision_number, text_hash)` — revision-stability rule.
- Check constraints: text_hash format `^[0-9a-f]{64}$`, revision_number >= 1, ordinal >= 0.
- Indices created: `idx_document_passages_text_hash`, `idx_document_passages_doc_revision (document_id, revision_number)`, `idx_document_passages_workspace_id`, `idx_document_passages_embedding_id (WHERE embedding_id IS NOT NULL)`.
- Zero rows — clean slate confirmed (Phase A finding PASG-01: no pre-existing rows to backfill).

### Monotonic trigger — negative test result

```
UPDATE silver.workspaces SET data_version = -1 WHERE workspace_id = 'a0000000-0000-0000-0000-000000000001';
ERROR: data_version is monotonic — cannot decrement from 0 to -1
CONTEXT: PL/pgSQL function enforce_data_version_monotonic() line 4 at RAISE
```

Trigger rejected the decrement as expected.

### KML parser

- Action: **partial removal** — `spatial_parser.py` handles many formats beyond KML; only KML-specific code was deleted.
- Removed from `spatial_parser.py`: `.kml`/`.kmz` entries in `_VECTOR_EXTENSIONS`, `"KML"` from `_MULTI_LAYER_DRIVERS`, `source_format = "kml"` branch in `parse_spatial_file`, KML CRS-assumption block (`if ext in (".kml", ".kmz") and gdf.crs is None`).
- Removed from `tests/test_spatial_parser_sprint4.py`: `TestKmlCrsHandling` class (3 tests), two `test_kml` / `test_kmz` entries in `TestDetectFormat`.
- No KML-specific libraries (fastkml/pykml) were in `pyproject.toml`; KML was handled via pyogrio (a general vector library). No dependency removals needed.
- `python -m py_compile` in container: OK.
- Module 10 doc-sweep entry added to `ops/backlog/module-10-doc-sweep.md`.

### PDF parser fallback marker

- Header docstring added to `src/dagster/georag_dagster/parsers/pdf_report.py` — marks it as RAGFlow fallback-only, references Kyle approval 2026-04-20 and the TODO for a runtime guard.
- TODO comment added at the call site in `src/dagster/georag_dagster/assets/silver_reports.py` line ~172.

### Sensor docstring

- Fixed: `definitions.py` sensor description at line 348–352. Changed `"Polls the georag-bronze MinIO bucket"` → `"Polls the bronze SeaweedFS bucket"`. Live code at line 371 (`bucket = "bronze"`) was already correct; this was docstring-only drift (DAG-04).

### Migration rollbacks

None. All three migrations applied cleanly on first attempt. No rollback was triggered.

### Service health post-migration

All 16 services remained healthy throughout: georag-postgresql, georag-laravel-octane, georag-laravel-horizon, georag-laravel-reverb, georag-fastapi, georag-dagster-daemon, georag-dagster-webserver, georag-martin, georag-minio, georag-neo4j, georag-pgbouncer, georag-qdrant, georag-redis, georag-ollama, georag-backup-agent, georag-ofelia.

### Structural surprise

The `silver.document_passages` FK to `silver.reports` resolves correctly as `reports(report_id)` because PostgreSQL uses the `silver` search_path set for the migration session. The constraint shows as `REFERENCES reports(report_id)` in `\d` output (unqualified) which is the expected behaviour — the FK is valid and resolves within the `silver` schema. No action needed.

---

*Phase B1+B2 executed: 2026-04-20. data-engineer agent (Claude Sonnet 4.6).*

---

## Phase B3 Applied (2026-04-20)

**Migration batch:** 14

**Migrations applied:**
| Migration | Duration | Result |
|---|---|---|
| `2026_04_20_130000_create_document_revisions` | 57.24ms | DONE |
| `2026_04_20_140000_create_evidence_items` | 28.28ms | DONE |
| `2026_04_20_150000_create_structured_record_lineage` | 24.73ms | DONE |
| `2026_04_20_160000_backfill_document_revisions` | 28.54ms | DONE |

**Row counts post-apply:**
- `silver.document_revisions`: 1 (backfilled legacy NI 43-101 row, source_sha256 all-zeros sentinel)
- `silver.evidence_items`: 0
- `silver.structured_record_lineage`: 0

**Schema verification:**
- All CHECK constraints present: `evidence_items_type_valid`, `evidence_items_exactly_one_ref`, `evidence_items_type_ref_consistent`, `document_revisions_sha256_format`, `document_revisions_revision_positive`, `structured_record_lineage_sha256_format`
- `evidence_items.passage_id` FK shows `ON DELETE RESTRICT` (RESTRICT blocker fix confirmed)
- All indices created per §04j spec

**Constraint smoke tests (all run in transactions, rolled back):**
- Test 1: INSERT `evidence_type='structured_record'` + `structured_ref='{"a":1}'` + all other refs NULL → SUCCEEDED (INSERT 0 1), then ROLLBACK
- Test 2: INSERT `structured_record` with both `structured_ref` AND `graph_edge_ref` populated → FAILED on `evidence_items_exactly_one_ref` CHECK (correct)
- Test 3: INSERT `evidence_type='graph_edge'` with `structured_ref` populated (type mismatch) → FAILED on `evidence_items_type_ref_consistent` CHECK (correct)
- Test 4: INSERT `structured_record_lineage` row with non-existent `evidence_id` → FAILED on `structured_record_lineage_evidence_id_fkey` FK (correct)

**Rollback pretend (--pretend, not executed):**
Confirmed correct reverse order: 160000 (DELETE sentinel rows with named bindings) → 150000 (DROP lineage) → 140000 (DROP evidence_items) → 130000 (DROP self-FK, DROP document_revisions). No anomalies.

**Tightenings applied (senior-reviewer conditions):**
- T1: Parameter binding in backfill SQL — `DB::statement($sql, [':ws' => ..., ':sha' => ...])` pattern applied to all three SQL calls in up() and down() of 160000 migration. No raw string interpolation of sentinel constants remains.
- T2: Deterministic timezone cast — `COALESCE(r.created_at AT TIME ZONE 'UTC', NOW())` replaces `::timestamptz` for session-TZ independence.
- T3: GIN index debt note added to plan doc Module 6 Coordination section.
- SME decisions (Q1 cardinality + Q2 is_current) documented in plan doc.

**Pre-apply parse error (resolved):**
Migration `140000` had an unescaped single quote in an inline SQL comment (`-- leave evidence_type='document_passage'`), terminating the PHP single-quoted string. Fixed by removing the quotes from the comment text (SQL comments do not require value quoting). No schema change.

**Architecture doc gaps logged to module-10-doc-sweep.md:**
- §04j does not specify FK cascade semantics for `evidence_items.passage_id` — drafter had to infer. RESTRICT is now the implemented and documented semantics. Module 10 should add one sentence mandating RESTRICT.

---

*Phase B3 applied: 2026-04-20. data-engineer agent (Claude Sonnet 4.6).*

---

## Chunk 1 Applied 2026-04-20

### Asset-check coverage table

| Asset | Check name | Severity | Blocking | Assertion |
|---|---|---|---|---|
| `silver_collars` | `collar_count_positive` | ERROR | Yes | `COUNT(*) > 0` on silver.collars |
| `silver_collars` | `schema_conformance_pass_rate` | ERROR/WARN | Yes | `parse_ok > 0` from bronze.provenance; WARN at partial; ERROR at 0% |
| `silver_collars` | `crs_round_trip_sane` | ERROR | Yes | Zero NULL geom + zero SRID=0 rows in silver.collars |
| `silver_surveys` | `parse_total_positive` | ERROR | Yes | `COUNT(*) > 0` on silver.surveys |
| `silver_lithology` | `parse_total_positive` | ERROR | Yes | `COUNT(*) > 0` on silver.lithology_logs |
| `silver_samples` | `parse_total_positive` | ERROR | Yes | `COUNT(*) > 0` on silver.assay_samples |
| `silver_well_logs` | `parse_total_positive` | ERROR | Yes | `COUNT(*) > 0` on silver.well_logs |
| `silver_spatial` | `geom_not_null` | ERROR | Yes | Zero NULL geom rows in silver.spatial_features |
| `silver_spatial` | `crs_srid_populated` | ERROR | Yes | Zero SRID=0 rows in silver.spatial_features |
| `silver_reports` | `parse_total_positive` | ERROR | Yes | `COUNT(*) > 0` on silver.reports |
| `silver_reports` | `schema_conformance_pass_rate` | ERROR/WARN | Yes | At least one report with non-empty sections_text |
| `silver_xlsx` | `parse_total_positive` | ERROR | Yes | Combined collar+sample count > 0 (XLSX proxy) |
| `silver_reports` | `no_duplicate_passage_ids` | ERROR | Yes | Zero duplicate passage_id groups in silver.document_passages |
| `silver_reports` | `text_hash_sha256_valid` | ERROR | Yes | All text_hash values match `^[0-9a-f]{64}$` |
| `silver_reports` | `document_revisions_document_id_not_null` | ERROR | Yes | Zero NULL document_id in silver.document_revisions |
| `silver_reports` | `document_revisions_sha256_format` | ERROR | Yes | All source_sha256 match `^[0-9a-f]{64}$` |
| `silver_reports` | `evidence_items_exactly_one_ref` | ERROR | Yes | All evidence_items rows have exactly one non-null ref field |
| `index_reports` | `embedding_id_present` | ERROR | Yes | All silver.reports rows have `cardinality(embedding_ids) > 0` |
| `index_reports` | `parser_error_floor` | ERROR/WARN | Yes | Blocking on 0% embedded; WARN otherwise |

Total: **19 checks**, **all blocking=True**. Zero warn-only-severity checks — partial failures surface as WARN but still gate execution via blocking=True.

Assets without checks (Chunk 2 scope — drill_traces, COG rasters): excluded per scope.

### `commit_ingestion_run` asset — architecture

```
bronze_* ──→ silver_* ──[blocking checks]──→ gold_* ──→ index_* ──[blocking checks]
                                                                          │
                                                               commit_ingestion_run
                                                                  (group: commit)
                                                            ┌──────────────────────┐
                                                            │ 1. Atomic DB txn:    │
                                                            │    UPDATE silver.     │
                                                            │    workspaces SET     │
                                                            │    data_version += 1  │
                                                            │    UPDATE silver.     │
                                                            │    projects SET       │
                                                            │    data_version += 1  │
                                                            │ 2. CLUSTER silver.*   │
                                                            │ 3. ANALYZE silver.*   │
                                                            │ 4. REFRESH MV (if any)│
                                                            │ 5. Emit metadata for  │
                                                            │    Module 7 Reverb    │
                                                            └──────────────────────┘
```

**Dep chain:** `silver_collars`, `silver_reports`, `silver_spatial`, `index_reports`, `index_neo4j`
**Group:** `commit` (separate from gold/index for UI clarity)
**Config:** `workspace_id` (default: `a0000000-0000-0000-0000-000000000001`), `project_ids` (comma-separated UUIDs)

### data_version bump — verified before/after

| Entity | Before | After | Bumped by |
|---|---|---|---|
| `silver.workspaces` (default workspace) | 0 | 1 | `_bump_data_version()` in `commit_ingestion_run` |
| `silver.projects` (019d74a1-...) | 0 | 1 | Same transaction |
| `silver.projects` (019d74a7-...) | 0 | 1 | Same transaction |

Monotonic trigger verified: `UPDATE ... SET data_version = -1` → `ERROR: data_version is monotonic — cannot decrement from 0 to -1` (rollback confirmed in Phase B1+B2; not re-tested destructively per spec).

### Post-ingest-tune invocation

Inline via `_run_tune_target()` called after data_version commit. Three targets (from `post-ingest-tune.sql` header inventory):

| Table | Index | Matview | CLUSTER | ANALYZE | REFRESH |
|---|---|---|---|---|---|
| `silver.collars` | `idx_collars_geom` | `silver.mv_collar_summary` | 0.14s | 0.02s | 0.05s |
| `silver.reports` | `idx_reports_geom` | none | skipped if idx missing | — | — |
| `silver.spatial_features` | `idx_spatial_features_geom` | none | skipped if idx missing | — | — |

**Wall time (collars full cycle):** 0.22s. Tune failures are non-blocking — a failed CLUSTER logs a WARNING but does not roll back the data_version commit (correct — data is already committed, tuning is best-effort).

Implementation: psycopg2 with `autocommit=True` for CLUSTER and REFRESH (both require being outside a transaction block for correctness). ANALYZE runs in a managed connection. Script at `ops/postgis/post-ingest-tune.sql` was NOT modified — it is Module 2's shipped artifact.

### Bronze cleanup

3 stale objects under `s3://bronze/georag-exports/` prefix deleted:

```
georag-exports/019d7909-3d16-7209-9644-ced238b1e5c8/georag_collars_69d95a6f85385.csv  (2285 bytes)
georag-exports/019d790a-1c13-70e4-a3d9-0af0ff9fe6a5/georag_csa_bundle_69d95aa898e8d.zip (1274 bytes)
georag-exports/019d7921-0542-7141-be72-b16e5ae01f0c/georag_collars_69d9608585ec8.csv  (2285 bytes)
```

Post-delete verification: `aws s3 ls s3://bronze/ --recursive | grep georag-exports` → "clean — no georag-exports prefix objects remain". Bronze bucket is now clean.

### Deferred / out-of-scope items

1. **`silver_seismic`, `silver_xyz` checks** — these Silver assets are graph dead-ends (DAG-05) and have no Gold/Index downstream. A `parse_total_positive` check on their tables could be added but would provide no commit-gate value until those assets have downstream consumers. Deferred to the sprint that wires them to Gold/Index.

2. **`gold_placeholder` and `index_placeholder` checks** — placeholder assets exist only as stubs and carry no real data. Adding `parse_total > 0` checks on them would always pass vacuously. Excluded per spec: "any asset that doesn't have a natural check: emit at minimum a `parse_total > 0` blocking check + document why a richer check wasn't added." Documentation: these assets are stubs — they execute no data transformation. A check would be a no-op gate.

3. **`commit_ingestion_run` own asset check** — the spec suggests an asset check that verifies the data_version UPDATEs actually landed. This was intentionally not added because `_bump_data_version()` raises on NULL result (workspace not found) and rolls back on any exception, making the asset itself fail rather than passing with bad state. Adding a check that re-reads the post-commit values would be redundant with the function's own guard. Flagged for Phase C review.

4. **`gold_public_geoscience_neo4j`, `index_public_geoscience_qdrant` checks** — public-geoscience assets run on a separate schedule and are not in the private-project commit chain. Checks should be added in the sprint that wires these to the public-project commit concept. Deferred.

5. **B4 desurvey (drill_traces), B5 COG normalization** — Chunk 2 scope. No placeholder checks added per spec.

---

*Chunk 1 applied: 2026-04-20. data-engineer agent (Claude Sonnet 4.6).*
*Items implemented: B1 (asset-check gates), B9 (data_version bump), B10 (post-ingest-tune invocation), Bronze cleanup.*
*Files touched: see list in return response.*

---

## Chunk 2 Applied 2026-04-20

### Deps pinned

| Package | Version | Range pinned |
|---|---|---|
| `wellpathpy` | 0.5.2 | `>=0.5.0,<1.0.0` |
| `rio-cogeo` | 5.4.2 | `>=5.0.0,<6.0.0` |

Both pinned in `src/dagster/pyproject.toml`. Dagster image rebuilt; imports verified in running container.

### Image rebuild

Wall time: 416.8s (PyPI deps resolve + layer export). Both services recreated and healthy post-rebuild.

### Migration

| Migration | Batch | Duration | Result |
|---|---|---|---|
| `2026_04_20_170000_create_silver_drill_traces` | 15 | 50.10ms | DONE |

`silver.drill_traces` verified with `\d silver.drill_traces`: correct LINESTRINGZ geometry column, all FKs present (collars → CASCADE, workspaces → CASCADE, projects → CASCADE), GIST index + 3 supporting BTree indices.

### silver_drill_traces asset

- Location: `src/dagster/georag_dagster/assets/silver_drill_traces.py`
- LOC: ~280 (asset + 8 helpers)
- Upstream deps: `silver_collars`, `silver_surveys`
- Algorithm: in-house `_survey_interp.minimum_curvature` (wellpathpy installed as reference/smoke-test)
- CRS: project EPSG (reads `projects.crs_epsg`, default 32613) → reprojected to EPSG:4326 via PyProj
- Idempotency: SHA-256 survey hash; same hash = skip
- 5 edge cases covered and unit tested (29 tests, 29 pass):
  - EC-1: 0-survey collar → skip, no row written
  - EC-2: 1-survey collar → vertical LINESTRINGZ, trace_quality='single_survey_vertical'
  - EC-3: Duplicate depths → keep first (most recent updated_at per SQL ORDER BY), count logged
  - EC-4: Invalid az/dip → rejected rows counted; if all invalid → treated as 0-survey
  - EC-5: High dogleg (>15°/30m) → computed anyway, trace_quality='high_dogleg_warning', dogleg_max_deg stored

### silver_cog_rasters asset

- Location: `src/dagster/georag_dagster/assets/silver_cog_rasters.py`
- LOC: ~360 (two assets + check + helpers + Pydantic model)
- `bronze_raster_uploads`: STUB (enumerates `bronze-raster/source/**/source.tif`; full upload wiring is a later sprint)
- `silver_cog_rasters`: downstream of stub; converts via `rio_cogeo.cogeo.cog_translate`, web-optimized deflate profile; writes to `bronze-raster/cog/`
- Sidecar `metadata.json` schema: `CogSidecarMetadata` Pydantic model — 10 fields including `bounds_wgs84`, `native_crs`, `pixel_resolution_m`, `band_count`, `nodata`, `data_type`, `cog_url`, `source_url`, `generated_at`, `rio_cogeo_version`, `source_etag`
- Idempotency: source ETag stored in sidecar; matching ETag → skip
- Source files never modified; all writes to `cog/` prefix only

### Asset-check count

| State | Count |
|---|---|
| Before Chunk 2 | 23 |
| After Chunk 2 | 26 |

New checks (all blocking=True):
- `desurvey_trace_count_matches_collar_count_with_surveys` (on `silver_drill_traces`)
- `bronze_raster_sources_discoverable_check` (on `bronze_raster_uploads`)
- `cog_readable_check` (on `silver_cog_rasters`)

### Live drill results

**Desurvey:** Ran minimum curvature against first dev-DB collar (8ab89d36-...) with 3 surveys. Produced 4-point LINESTRINGZ. Verified: `ST_SRID=4326`, `ST_NumPoints=4 >=2`, `ST_GeometryType=ST_LineString`. Test row cleaned up post-verification.

**COG:** `bronze-raster/source/` is empty (bucket exists, no source.tif objects yet). COG conversion not triggered; `cog_readable_check` correctly passes with 0 COGs (bucket empty is not an error).

### Commit gate

`commit_ingestion_run` wired to `silver_drill_traces` + `silver_cog_rasters` as upstream deps. `_TUNE_TARGETS` extended to include `silver.drill_traces` GIST index. Definitions load cleanly: 54 assets, 26 checks.

### Deferred items

1. `bronze_raster_uploads` full wiring — upload-to-Bronze flow is a later sprint. Stub sufficient to unblock COG development.
2. `silver_drill_traces` Dagster materialisation test via UI — requires non-empty Silver survey data and a materialization run. Logic validated via Python inline execution.
3. wellpathpy API parity check — math uses `_survey_interp.minimum_curvature`; wellpathpy is installed as reference. A Phase C corpus test should run both implementations on the same dataset and diff results.

### Surprises

- rio-cogeo latest stable is 7.0.2 (the spec said `>=5.0.0,<6.0.0`). Pinned to 5.x as specified; 5.4.2 installed without conflicts.
- `silver.drill_traces` note: the strict equality check in `desurvey_trace_count_matches_collar_count_with_surveys` will flag collars where ALL surveys have invalid az/dip (they are in `silver.surveys` but produce no trace). In practice this is correct — those collars should be inspected. Documented in check description.

---

*Chunk 2 applied: 2026-04-20. data-engineer agent (Claude Sonnet 4.6).*
*Items implemented: B5 (drill_traces pipeline), B6 (COG normalization), bronze_raster_uploads stub.*

---

## Chunk 3a — MinIOResource → boto3 Refactor (2026-04-20)

**Intake ref:** ops/backlog/module-3-intake.md "Dagster MinIOResource → boto3 refactor"

### Call sites touched

25 files total:

- `src/dagster/georag_dagster/resources.py` — class replaced
- `src/dagster/georag_dagster/definitions.py` — sensor rewritten, resource wiring updated
- `src/dagster/georag_dagster/assets/silver_cog_rasters.py` — own minio imports removed, all direct client calls rewritten
- `src/dagster/georag_dagster/assets/silver_public_geoscience.py` — `_find_latest_bronze` rewritten
- `src/dagster/georag_dagster/assets/index_reports.py` — figure extraction rewritten
- 20 additional asset files — import rename only (`MinIOResource` → `S3Resource`): silver.py, bronze_reports.py, silver_surveys.py, silver_samples.py, silver_raster.py, silver_xlsx.py, bronze_well_logs.py, silver_reports.py, silver_lithology.py, bronze_seismic.py, bronze.py, silver_spatial.py, silver_well_logs.py, bronze_public_geoscience.py, bronze_spatial.py, bronze_samples.py, bronze_xyz.py, bronze_xlsx.py, bronze_surveys.py, silver_xyz.py, bronze_lithology.py, silver_seismic.py

### Vendor methods removed

| minio-py (removed) | boto3 equivalent (added to S3Resource) |
|---|---|
| `fput_object()` | `upload_file()` (boto3 `upload_file`) |
| `put_object()` | `upload_bytes()` (boto3 `put_object`) |
| `get_object()` | `download_bytes()` (boto3 `get_object['Body'].read()`) |
| `bucket_exists()` | `bucket_exists()` (boto3 `head_bucket` + ClientError) |
| `make_bucket()` | `ensure_bucket()` (boto3 `create_bucket`) |
| `stat_object()` | `stat_object()` (boto3 `head_object`) |
| `list_objects(recursive=True)` | `list_keys()` (boto3 `get_paginator('list_objects_v2')`) |

### Resource class

- Old: `MinIOResource` (minio-py backed, `_client()` returned `Minio` object)
- New: `S3Resource` (boto3 backed, `get_client()` returns `boto3.client('s3')`)
- `MinIOResource = S3Resource` alias retained for any stray import
- Resource key in definitions.py: remains `"minio"` (Dagster matches by parameter name; no asset function rename required)

### Deps

- `minio>=7.2` removed from `pyproject.toml` (both `[project.dependencies]` and `[project.optional-dependencies].ingest`)
- `boto3>=1.35` and `botocore>=1.35` added
- Confirmed installed in container: boto3 1.42.92

### Docker compose

- Added `S3_ENDPOINT_URL: ${S3_ENDPOINT_URL:-http://minio:8333}` to dagster-daemon environment block

### Rebuild wall time

~247 seconds (layer cache warm except dep install layer)

### Smoke test

- `import minio` → ModuleNotFoundError (confirmed)
- `boto3.__version__` → 1.42.92 (confirmed)
- `S3Resource.bucket_exists('bronze')` → True (confirmed)
- `S3Resource.list_keys('bronze')[:3]` → ['collars/sample_collars.csv', 'excel/PLS_collars.xlsx', 'lithology/sample_lithology.csv'] (confirmed)
- `silver_cog_rasters` import → clean (confirmed)
- `silver_public_geoscience` import → clean (confirmed)
- `index_reports` import → clean (confirmed)
- `definitions` import → clean (confirmed)
- dagster-daemon: healthy; dagster-webserver: healthy

### Surprises

None. All 7 vendor methods mapped cleanly to boto3 equivalents. The iterator model change (minio object with `.object_name`/`.is_dir`/`.last_modified` → boto3 page dict with `Key`/`LastModified`) required rewriting the sensor loop, `_find_latest_bronze`, and the `index_reports` figure extraction — these were the three non-trivial call sites. No exotic minio-py features were in use.

*Chunk 3a applied: 2026-04-20. data-engineer agent (Claude Sonnet 4.6).*

---

## Phase D Complete 2026-04-20

### Runbooks written

| File | Purpose |
|---|---|
| `ops/runbooks/ingestion-pipeline.md` | Asset graph overview, trigger procedures, replay from Bronze, debug guide, commit gate, asset-check map |
| `ops/runbooks/evidence-model.md` | §04j three-table story, example rows per evidence_type, lineage trace walkthrough, FK cascade semantics, rollback procedure, sentinel values |
| `ops/runbooks/data-version.md` | §05d monotonicity contract, where it bumps, downstream consumers, monotonic trigger, debug checklists, post-restore procedure |
| `ops/runbooks/validation-corpora.md` | Corpus state by format, how to add test files, expected-output JSON shape, baseline location, Phase C runner spec |
| `docs/parsers/TEMPLATE.md` | Parser addition template: IR shape, failure handling, quality metrics, corpus requirements, asset-check requirements, commit-gate wiring, merge checklist |

### Module 3 final status

Phase A + B1 + B2 + B3 + Chunk 1 + Chunk 2 + Chunk 3a + D complete.

Phase C (parse-quality baselines) deferred to pair with Module 4 start. No quality percentage claims may be made until Phase C assembles corpus expected-output fixtures and populates `ops/baselines/`.

*Phase D written: 2026-04-20. data-engineer agent (Claude Sonnet 4.6).*
