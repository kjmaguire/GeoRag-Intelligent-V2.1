# Appendix E — Ingestion Format Matrix

Status: **Draft.** Per-format end-to-end contract. One row per supported
file format with the full path from upload to gold/index.

> **Cardinal decision:** all *user-triggered* ingest goes through Hatchet
> per Hard Rule #7. Dagster owns *scheduled bulk* and *reranker label*
> assets only. References to Dagster below in user-flow rows are the
> downstream Dagster materialisation that re-uses the same bronze rows
> populated by the Hatchet ingest workflow.

## 1. Matrix

| # | Format | Upload path | Parser entry | Orchestrator | Bronze target | Silver target | Gold / index | CRS handling | Unit handling | Duplicate detection | QA gates | Review routing | Commit | Reprocess | Tests |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | PDF (NI 43-101, internal report) | `POST /api/v1/projects/{p}/uploads kind=pdf` | [src/dagster/georag_dagster/parsers/pdf_report.py](../../../src/dagster/georag_dagster/parsers/pdf_report.py) → `parse_pdf_report()` | Hatchet `ingest_pdf` | `bronze/<ws>/<proj>/<sha>/<file>.pdf` + `bronze.ingest_manifest` | `silver.reports`, `silver.report_pages`, `silver.report_figures`, `silver.report_tables`, `silver.document_passages`, `silver.evidence_items`, `silver.parser_run_artifacts`, `silver.ocr_page_quality`, `silver.table_extraction_quality`, `silver.document_ingestion_quality` | Qdrant `reports` collection, Neo4j `:Report` + `:Document` + `:Citation` | n/a (text) | n/a | sha256 | qpdf structural check; per-page char count ≥ MIN; OCR confidence; table cell count | low confidence pages → `silver.low_confidence_page_reviews` | `commit_ingestion_run` only when persist_node succeeds + embed_verify passes | Re-running same sha256 upserts on `(workspace_id, parser_name, source_file_sha256, source_row)` — silver no-dup, bronze gets a new run row | `src/fastapi/tests/test_ingest_pdf_e2e.py`, pgTAP report-pages shape |
| 2 | TIFF (multi-page) | upload as `kind=tiff` | [src/fastapi/app/services/tiff_to_pdf.py](../../../src/fastapi/app/services/tiff_to_pdf.py) → normalise to PDF, then Path #1 | Hatchet `tiff_normalize` → `ingest_pdf` | same as PDF | same as PDF | same as PDF | n/a | n/a | sha256 | TIFF tag conformance | identical to PDF | identical | identical | TIFF normalise smoke |
| 3 | GeoTIFF (raster) | upload as `kind=geotiff` | [src/dagster/georag_dagster/parsers/raster_parser.py](../../../src/dagster/georag_dagster/parsers/raster_parser.py) | Hatchet `ingest_raster` → Dagster `silver_raster`, `silver_cog_rasters` | `bronze-raster/<ws>/...` + `bronze.spatial_features` | `silver.raster_layers`, `silver.cog_rasters` | Martin raster path TODO; tiles served from COG | Detect SRID; reproject to EPSG:4326 for `geom` columns | metric/feet auto-detect from GeoKeys | sha256 | GDAL `gdalinfo` success; SRID known | unknown CRS → review | written when COG generated | re-runs replace COG; old retained in bronze-raster | TBD |
| 4 | CSV — collar | upload `kind=csv_collar` | [parsers/csv_collar.py](../../../src/dagster/georag_dagster/parsers/csv_collar.py) | Hatchet `ingest_csv_drillhole` → Dagster `silver_collars_canonicalize_backfill` | `bronze.raw_collar_entries` + `bronze.source_files` | `silver.collars` | `gold.h3_density_mineral` refresh; `silver.drill_traces` derived | EPSG explicit in header OR project default (EPSG:32613 UTM Z13N) | depths in m; coords in projection units | `(project_id, hole_id)` unique constraint | header signature classifier; `to_depth > from_depth` checks; required cols present | low confidence row → `silver.review_queue.lifecycle='pending'` | only when `silver.review_queue.lifecycle='committed'` | re-upload by sha256 = no-op; corrected row update via review flow | parser unit tests + RLS coverage |
| 5 | CSV — assay | `kind=csv_assay` | [parsers/csv_sample.py](../../../src/dagster/georag_dagster/parsers/csv_sample.py) (multi-element) | Hatchet `ingest_csv_drillhole` → Dagster | `bronze.raw_assay_submissions` | `silver.assays_v2` (+ legacy `silver.assays`) | `gold.assay_composites`, `gold.significant_intersections` (in-flight) | n/a | element units inferred from header (ppm/ppb/%/g/t); normalised to `value_ppm` | sha256 + natural key `(collar_id, from_depth, to_depth, element)` | over/under detection limit flags; QAQC dup checks | low confidence batch → review | post-review | re-upload same sha = no-op | csv audit tests |
| 6 | CSV — lithology log | `kind=csv_lithology` | [parsers/csv_lithology.py](../../../src/dagster/georag_dagster/parsers/csv_lithology.py) | Hatchet → Dagster `silver_lithology` | `bronze.raw_lithology_logs` | `silver.lithology` (+ legacy `silver.lithology_logs`) | downhole strip log gold view | n/a | depths in m | sha256 + `(collar_id, from_depth)` | rock-code dictionary lookup; description GIN tsvector | rock-code conflict → derive_intervals.py v2 LLM disambiguation | post-derive | rerun re-derives | parser + derive tests |
| 7 | CSV — downhole survey | `kind=csv_survey` | [parsers/csv_survey.py](../../../src/dagster/georag_dagster/parsers/csv_survey.py) | Hatchet → Dagster `silver_drill_traces` | `bronze.raw_surveys` | `silver.surveys`, `silver.drill_traces` (LineString) | Martin `silver.pg_drill_traces_by_project` | EPSG:4326 LineString output | depths/azimuth/dip in m/° | sha256 + `(collar_id, depth)` | survey continuity + monotonic depth | gaps → derived from curves fallback | post-derive | re-runs replace LineString | parser tests |
| 8 | CSV — sample (soil / rock chip) | `kind=csv_sample` | [parsers/csv_sample.py](../../../src/dagster/georag_dagster/parsers/csv_sample.py) | Hatchet → Dagster | `bronze.raw_sample_submissions` (planned) | `silver.samples` + `silver.geochemistry` | `gold.h3_density_mineral` | EPSG from header or project default | element-specific units | sha256 + natural key | over/under detection | low confidence → review | post-review | re-run no-dup | parser tests |
| 9 | XLSX (multi-sheet workbook) | `kind=xlsx` | [parsers/xlsx_parser.py](../../../src/dagster/georag_dagster/parsers/xlsx_parser.py) + [_sheet_classifier.py](../../../src/dagster/georag_dagster/parsers/_sheet_classifier.py) | Hatchet `ingest_xlsx` → branches into CSV pipeline per sheet type | `bronze.ingest_manifest` per sheet | branches by sheet type (collar / assay / litho / sample) | same as the routed type | inherited | inherited | sha256 + per-sheet sha | sheet-type classifier confidence | sheet route uncertain → review | per-sheet commit | rerun re-routes | xlsx audit tests |
| 10 | LAS (well logs) | `kind=las` | [parsers/las_parser.py](../../../src/dagster/georag_dagster/parsers/las_parser.py) | Hatchet `ingest_las` → Dagster `silver_well_logs` (planned silver target) | `bronze.well_log_curves_raw` | `silver.well_log_curves` | derived gold views in flight | n/a (depths only) | curve-specific units carried | sha256 | LAS version + required curves | missing depth curve → review | post-derive | rerun replaces curves | parser tests |
| 11 | SEG-Y (seismic) | `kind=segy` | [parsers/segy_parser.py](../../../src/dagster/georag_dagster/parsers/segy_parser.py) | Hatchet `ingest_segy` → Dagster `bronze_seismic` | `bronze.seismic_traces` | `silver.seismic_surveys` (Polygon bbox) | Martin `silver.pg_seismic_by_project` | EPSG from EBCDIC header → reproject 4326 | distance ft/m carried | sha256 | trace count + bin geometry sanity | unknown EPSG → review | post-process | rerun re-extracts header | TBD |
| 12 | GPKG (GeoPackage) | `kind=gpkg` | [parsers/spatial_parser.py](../../../src/dagster/georag_dagster/parsers/spatial_parser.py) | Hatchet `ingest_gpkg` → Dagster `bronze_spatial` | `bronze.spatial_layers` (per-table) | `silver.spatial_features` + (if matched) `silver.project_boundaries`, `silver.geological_formations`, `silver.historic_workings` | Martin `silver.pg_boundaries_*`, `pg_formations_*` (stubs) | EPSG from GPKG metadata → reproject 4326 | n/a | sha256 | OGR `ogrinfo` success; required layers present | non-spatial sidecar layers filtered out (per QField fix) | post-import | rerun replaces features | spatial parser tests |
| 13 | GeoJSON | `kind=geojson` | spatial_parser.py | Hatchet `ingest_geojson` | `bronze.spatial_layers` | `silver.spatial_features` | inherited | EPSG:4326 only | n/a | sha256 | well-formed JSON; valid geometries | invalid geom → review | post-import | rerun replaces features | parser tests |
| 14 | Shapefile (ZIP of .shp+.dbf+.prj) | `kind=shapefile` | spatial_parser.py | Hatchet `ingest_shapefile` | `bronze.spatial_layers` | `silver.spatial_features` | inherited | EPSG from .prj → reproject 4326 | n/a | sha256 (on zip) | required sidecar files present | missing .prj → review | post-import | rerun replaces | parser tests |
| 15 | Point cloud / XYZ | `kind=xyz` | [parsers/xyz_parser.py](../../../src/dagster/georag_dagster/parsers/xyz_parser.py) | Hatchet `ingest_xyz` → Dagster `bronze_xyz` | `bronze.point_clouds` | `silver.spatial_features` (point class) | downstream raster derivation (planned) | EPSG from sidecar; fallback project default | xyz unit per file | sha256 | header sanity | malformed columns → review | post-import | rerun replaces | parser tests |
| 16 | Drillhole ZIP bundle | `kind=drillhole_zip` | bundle handler unpacks → routes per file | Hatchet `ingest_drillhole_bundle` (orchestrates 4-15 above) | per-file bronze | per-file silver | per-file gold | inherited | inherited | per-file sha256 | per-file QA | per-file review | per-file commit | per-file rerun | bundle e2e test |
| 17 | Geophysics survey CSV | `kind=geophysics` | [parsers/csv_sample.py](../../../src/dagster/georag_dagster/parsers/csv_sample.py) variant | Hatchet `ingest_geophysics` → Dagster `silver_geophysics` | `bronze.raw_geophysical_runs` | `silver.geophysics_surveys` | (none yet) | EPSG explicit | survey-specific units | sha256 + survey_type+date | per-survey-type schema | unknowable type → review | post-classify | rerun re-classifies | TBD |
| 18 | DOCX (rare) | `kind=docx` | [parsers/docx_parser.py](../../../src/dagster/georag_dagster/parsers/docx_parser.py) | Hatchet `ingest_docx` → routes through reduced PDF path | `bronze/<ws>/.../file.docx` | `silver.reports` (as text-only report) | (none) | n/a | n/a | sha256 | docx parse success | structure missing → review | post-import | rerun replaces | TBD |
| 19 | Provincial open-data feed | Kestra `public_geoscience_pull` scheduled flow | downloaders under [src/fastapi/app/services/publicgeo/](../../../src/fastapi/app/services/) | Kestra → FastAPI → Hatchet `public_geoscience_pull` → Dagster `bronze_public_geoscience` → `silver_public_geoscience` | `bronze.public_geoscience_*` | `public_geo.pg_*` | `public_geo.v_pg_*_mvt` views → Martin | per-source EPSG → reproject 4326 | per-source | jurisdiction+sha | source schema validation | none (machine-trusted) | scheduled cron | re-pulls upsert by `(source_id, source_feature_id)` | flow tests |

## 2. Routing layer in Laravel

`POST /api/v1/projects/{project}/uploads`:
1. Validates `kind` against the matrix above.
2. Streams body to SeaweedFS with key
   `bronze/<workspace_id>/<project_id>/<sha256>/<original-filename>`.
3. Writes `bronze.upload_files` (planned) + `bronze.ingest_runs(status='running')`.
4. Calls FastAPI `POST /internal/v1/shadow/<workflow>/trigger` with the
   workflow name resolved from `kind`.
5. Returns `{ingest_run_id, upload_id, sha256}` to the browser.
6. Reverb `ingestion-progress.{ws}::IngestProgress(status=queued)` event.

If `sha256` is already known (`bronze.ingest_manifest WHERE sha256=?
AND project_id=?`), the route is idempotent — returns the original
`ingest_run_id`.

## 3. CRS handling — canonical rules

- Internal storage: every silver `geom` column is EPSG:4326 (geographic)
  EXCEPT `silver.collars.geom_utm` which carries the project's primary
  UTM zone (default EPSG:32613).
- Every silver row writes both an in-source CRS field
  (`source_crs_epsg INTEGER`) and the normalised geometry.
- Reprojection happens once at the bronze→silver boundary using PostGIS
  `ST_Transform`.
- Unknown CRS → row routed to `silver.review_queue` until the SME tags
  the CRS.

## 4. Unit handling — canonical rules

- Depths: metres.
- Lengths / distances: metres.
- Elevations: metres ASL.
- Grades: `value_ppm` is the canonical, plus per-row `original_unit` and
  `original_value` for replay.
- Geophysics field strengths: SI (T for magnetic, mGal for gravity,
  S/m for conductivity).
- Vendor unit aliases live in
  [parsers/_vendor_aliases.py](../../../src/dagster/georag_dagster/parsers/_vendor_aliases.py).

## 5. Duplicate detection — canonical rules

- File-level: `sha256` on bytes. Same sha → same row in
  `bronze.ingest_manifest`; subsequent runs link to the prior run via
  `superseded_by_run_id`.
- Row-level (silver): natural key per table — examples in §1.

## 6. Review routing — canonical thresholds

Routing decision is computed in
[src/fastapi/app/services/review_router.py](../../../src/fastapi/app/services/) (canonical home):

| Signal | Threshold | Action |
|---|---|---|
| Parser confidence < 0.7 | row | `review_required` |
| Sheet-type classifier ambiguity | sheet | `review_required` |
| CRS unknown | row | `review_required` |
| Required column missing | row | `auto_reject` |
| QAQC dup detected | row | `review_required` |
| Otherwise | row | `auto_pass` |

`silver.review_queue.routing_decision` records the decision; the
lifecycle enum gates the silver commit (see [Appendix A §7](A-medallion-contract.md#7-human-review-gates)).

## 7. Tests required per format

Each format must ship:
1. **Unit test** for the parser entry function (golden input → expected bronze row).
2. **Integration test** for the Hatchet workflow (bronze + silver assertions).
3. **CRS round-trip test** where geometry is involved.
4. **Unit round-trip test** where units are involved.
5. **RLS test** — same file uploaded to two workspaces → rows fenced.
6. **Replay test** — re-running on the same sha256 produces idempotent rows.

Convention: tests live under
`tests/Feature/Ingestion/<Format>Test.php` and
`src/fastapi/tests/ingestion/test_<format>_*.py`.
