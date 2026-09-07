# Chapter 05 — PDF Stack §04p

> **Reconciled 2026-09-07** against `src/fastapi/app/services/pdf_extract.py`,
> `services/ingest/`, `src/fastapi/pyproject.toml` and `docker-compose.yml`.
> The pipeline description was current through ADR-0019; what was stale was
> the library stack (PyMuPDF was removed on licence grounds), the vision
> stage (vLLM), the container name and the parser table, which pointed into
> the deleted `src/dagster/` tree.

In-process replacement for the deleted RAGFlow service ([ADR-0002](../../adr/)).
Everything runs inside the single `hatchet-worker` container (and
occasionally `fastapi`) — no separate parsing process to deploy or scale.
There is no `hatchet-worker-ingestion`; the pools were merged
([Ch 07 §2.1](07-orchestration.md)).

> All file paths in this chapter are relative to the repo root.
>
> **OCR stack updated 2026-09-02 (ADR-0019).** Cohere Parse v5 on Azure AI
> Foundry is the primary scanned-page OCR engine, with Tesseract retained as
> the last-resort fallback. Azure Document Intelligence (2026-07-29 →
> 2026-09-02) is gone, and with it the lossless tiling / polygon
> reconstruction of oversized pages — Parse returns no word polygons, so
> oversized pages are downscaled to a pixel cap instead. Parse also returns
> no per-word confidence: its pages persist `ocr_confidence = NULL` and the
> multi-signal quality router scores them on content signals only.

## 1. Entry point

`ingest_pdf.parse()` ([src/fastapi/app/hatchet_workflows/ingest_pdf.py](../../../src/fastapi/app/hatchet_workflows/ingest_pdf.py))
calls `_run_parser_subprocess()` which delegates to:

[src/fastapi/app/services/ingest/pdf_report.py](../../../src/fastapi/app/services/ingest/pdf_report.py) → `parse_pdf_report(path)`

Why a subprocess pool: PDF parsing and raster OCR are CPU- and memory-heavy.
A crash in one PDF must not poison the worker. The pool also bounds memory
per parse — see `_wait_for_memory_headroom()` in `ingest_pdf.py`.

## 2. The seven-stage pipeline

```
body_bytes
   │
   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Stage 1 — PREFLIGHT      hatchet_workflows/ingest_pdf.py::preflight     │
│   sha256 + %PDF- magic bytes + pikepdf open + page count.               │
│   Rejects only genuinely password-protected PDFs — a permission flag    │
│   ("no copy") is NOT a rejection; that bare /Encrypt substring test     │
│   used to fail NI 43-101 reports that extracted fine.                   │
│                                                                         │
│   The standalone services/pdf_preflight.py module was deleted           │
│   2026-08-28, having never had a caller. It is the only place structural│
│   repair, linearization and the >500-page split plan were implemented — │
│   the shipped pipeline does none of those, and writes no PreflightReport│
│   to Bronze.                                                            │
└───────────────────────┬─────────────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Stage 2 — FAST-PATH NATIVE TEXT   pdf_extract.py → pdfminer.six         │
│   per-page text blocks with bboxes + font metadata                      │
│   reading-order recovery via blocks                                     │
│   PER_PAGE_MIN_CHARS gate: < N chars → mark page "image-only"           │
└───────────────────────┬─────────────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Stage 3 — TABLES        pdf_extract.py → pdfplumber (parallel)          │
│   diverse table-probe over pdfplumber + camelot strategies              │
│   one-pass page traversal — caches PDF body in memory                   │
│   each table goes through OCR-skip probe                                │
│   silver.table_extraction_quality row written                           │
└───────────────────────┬─────────────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Stage 4 — OCR    services/ingest/pdf_report.py                          │
│   image-only pages, OR low-content fitz pages                           │
│   Cohere Parse v5 (Foundry) primary; Tesseract last-resort fallback     │
│   pages rendered under COHERE_PARSE_MAX_PIXELS (downscaled, not tiled)  │
│   multi-signal quality routing → silver.review_queue                    │
└───────────────────────┬─────────────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Stage 5 — FIGURE EXTRACTION   pdf_render.py + agent/figure_extractor.py │
│   render figure pages → bronze-raster/<sha>/page-<NNNN>.png             │
│   detect figure bounding boxes                                          │
│   caption link via nearest-text heuristics                              │
└───────────────────────┬─────────────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Stage 6 — PAGE VERBALIZATION (opt-in, off the critical path)            │
│   services/ingest/page_vision_client.py → a Foundry vision model        │
│   the description BECOMES the passage text, so reranker, citations and  │
│   §04i numeric grounding all work on it like any other passage          │
│   inert unless IMAGE_VERBALIZATION_ENABLED; runs on an hourly cron      │
│   (verbalize_page_images), not inline during ingest                     │
└───────────────────────┬─────────────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Stage 7 — PERSIST     ingest_pdf.persist                                │
│   silver.reports, silver.report_pages, silver.report_figures            │
│   silver.report_tables, silver.parser_run_artifacts                     │
│   bronze.provenance rows (trigger auto-fills workspace_id)              │
│   outbox.pending_propagations for Qdrant fan-out                        │
│   workspace.data_version bump                                           │
└─────────────────────────────────────────────────────────────────────────┘
```

## 3. Per-stage code map

| Stage | File | Key functions |
|---|---|---|
| Preflight | [src/fastapi/app/hatchet_workflows/ingest_pdf.py](../../../src/fastapi/app/hatchet_workflows/ingest_pdf.py) | `preflight()` — sha256, magic bytes, pikepdf open, page count, password-protected rejection |
| Native text | [`services/pdf_extract.py`](../../../src/fastapi/app/services/pdf_extract.py) | text blocks with bboxes and font metadata via **pdfminer.six**, in a `ProcessPoolExecutor` |
| Tables | [`services/pdf_extract.py`](../../../src/fastapi/app/services/pdf_extract.py) | cell-level bboxes via **pdfplumber** `find_tables()`. There is no `pdf_layout.py` and camelot is not a dependency |
| OCR | [src/fastapi/app/services/ingest/pdf_report.py](../../../src/fastapi/app/services/ingest/pdf_report.py) + [cohere_parse_client.py](../../../src/fastapi/app/services/ingest/cohere_parse_client.py) + [html_table.py](../../../src/fastapi/app/services/ingest/html_table.py) | Cohere Parse v5 primary (one page image per request, HTML tables → grids), Tesseract fallback |
| Coordinates | [src/fastapi/app/services/pdf_coordinates.py](../../../src/fastapi/app/services/pdf_coordinates.py) | Maps OCR text → page-relative bboxes for citation span resolver |
| Rendering | [`services/pdf_render.py`](../../../src/fastapi/app/services/pdf_render.py) | renders page PNGs into the bronze raster prefix — SeaweedFS in dev, Azure Blob in production, behind the one `STORAGE_BACKEND` switch ([Ch 02 §4](02-data-stores.md)) |
| Page verbalization | [`services/ingest/page_verbalizer.py`](../../../src/fastapi/app/services/ingest/page_verbalizer.py) + [`page_vision_client.py`](../../../src/fastapi/app/services/ingest/page_vision_client.py) | a Foundry vision model describes the page; the description becomes the passage text. `services/pdf_vl.py` still exists and is constructed in the FastAPI lifespan, but its docstring describes an Ollama/vLLM backend that is gone — the live path is the verbalizer |
| Figure linking | [src/fastapi/app/agent/figure_extractor.py](../../../src/fastapi/app/agent/figure_extractor.py) | Figure → caption nearest-text linking v1 |
| Hatchet workflow | [src/fastapi/app/hatchet_workflows/ingest_pdf.py](../../../src/fastapi/app/hatchet_workflows/ingest_pdf.py) | `parse_pdf_report()` invocation and atomic Silver persistence |

## 4. Performance fixes that landed in 2026-05

[project_parse_perf_2026_05_22](../notes/INDEX.md#project_parse_perf_2026_05_22):

1. ~~PyMuPDF promoted to primary native-text parser~~ — **reversed.** PyMuPDF was removed in the 2026-05-27 licence audit (AGPL-3.0); pdfminer.six is the native-text parser.
2. Parallel pdfplumber for tables.
3. Diverse table-probe (multiple pdfplumber strategies).
4. OCR-skip probe — skips OCR for pages already covered by native text.
5. PDF body cached in memory per parse — no re-reads from disk.
6. Single-pass tables — one walk over pages instead of per-table calls.
7. Slot tuning — pool size from `min(os.cpu_count(), 4)`.
8. OCR execution was isolated from the event loop in parse subprocesses.

Result: 3-5× speedup on text-heavy NI 43-101 PDFs.

## 5. Performance & quality fixes that landed in 2026-05 (Phase 2)

[project_overnight_run_2026_05_22](../notes/INDEX.md#project_overnight_run_2026_05_22):

- Tesseract bumped from default psm to `psm=3` (auto page seg with OSD).
- Tables now read all pages, not just the first hit page.
- `§04p` re-enabled after the Phase 1 freeze.
- Azure Document Intelligence was the primary scanned-page OCR service (replaced by Cohere Parse v5 on 2026-09-02, ADR-0019).
- Figure→caption linking v1, writing page renders to object storage (the compose service is still named `minio` but runs SeaweedFS, per ADR-0001).

## 6. PDF coverage overhaul (six gaps closed 2026-05-22)

[project_pdf_coverage_overhaul_2026_05_22](../notes/INDEX.md#project_pdf_coverage_overhaul_2026_05_22):

- Per-page OCR (was only first-page).
- `page_first`/`page_last` tracking on every silver row that came from a PDF.
- Atomic persist — failure mid-write rolls back the entire silver page row.
- Inline embed trigger so embedding starts on persist, not on next sweep.
- `www-data` cache env vars (HF/numba/mpl/xdg) for unstructured.partition.pdf.
- Docker-commit CMD gotcha — explicit `command:` in compose so a stray
  `docker commit` can’t swap uvicorn for the worker entrypoint.

## 7. The pdfminer / Hatchet block

[project_pdfminer_loglevel_hatchet_block_2026_05_21](../notes/INDEX.md#project_pdfminer_loglevel_hatchet_block_2026_05_21):
`LOG_LEVEL=debug` + pdfminer logging flooded the Hatchet asyncio loop →
ingest_pdf steps cancelled → `silver.reports` stayed empty. Fixed by
forcing pdfminer’s logger to INFO regardless of global `LOG_LEVEL`.

## 8. TIFF normalisation (ADR-0005)

[project_tiff_smoke_2026_05_23](../notes/INDEX.md#project_tiff_smoke_2026_05_23):
`tiff_normalize` (replaced `tiff_ocr_cluster`) runs before the PDF stack
to convert multi-page TIFF stacks into normalised PDFs. End-to-end smoke
took 3.1 s per file. Exposed the pre-existing §04p parse subprocess-pool
instability on image-only PDFs — flagged separately.

## 9. Quality tables

Every parse writes quality telemetry:
- [silver.parser_run_artifacts](03-schemas.md) — per-stage artifact list with sizes/durations.
- `silver.ocr_page_quality` — per-page confidence + char count.
- `silver.table_extraction_quality` — per-table cells / strategy / score.
- `silver.document_ingestion_quality` — overall summary row.
- `silver.low_confidence_page_reviews` — pages routed to human review.

## 10. Env knobs

From [docker-compose.yml:2039-2065](../../../docker-compose.yml):

| Env var | Default | Effect |
|---|---|---|
| `OCR_ENGINE` | tesseract (compose: `cohere_parse`) | Selects the remote OCR engine. The retired `azure_document_intelligence` value logs CRITICAL and runs Tesseract. |
| `AZURE_FOUNDRY_PARSE_DEPLOYMENT` | unset | Foundry deployment name for Cohere Parse v5 (`Cohere-parse-v5`); endpoint and key are the shared `AZURE_FOUNDRY_ENDPOINT` / `AZURE_FOUNDRY_API_KEY` |
| `COHERE_PARSE_MAX_PIXELS` | 4000000 | Pixel cap for the rendered page image; oversized sheets are downscaled (no tiling) |
| `COHERE_PARSE_TIMEOUT_S` / `_OUTPUT_FORMAT` / `_INCLUDE_IMAGE_DESCRIPTIONS` | 120 / blocks / 0 | Per-request timeout, response shape, and whether Parse's figure descriptions enter the retrievable text |
| `OCR_PAGES_PER_BATCH` | 8 | Pages rendered together and posted concurrently as one group (in-flight requests capped by `PDF_OCR_PAGE_CONCURRENCY`) |
| `OCR_MAX_PAGES_PER_DOC` | 300 | Per-document cap on pages sent to the remote engine; the rest go to Tesseract and the parse carries an `ocr_page_budget_exhausted` warning |
| `PDF_PARSER_TESSERACT_FALLBACK_ENABLED` | true | Fall back to Tesseract when Parse is unavailable or empty |
| `OCR_ROUTING_THRESHOLDS_JSON` | unset | Tier thresholds; unset routes uncertain OCR to review. The shipped values are hand-picked, **not** calibrated — the assessment reports `thresholds_calibrated` only when the JSON carries a `calibrated_from` key naming an artefact. Supports per-engine bands via `by_ocr_method`; Cohere Parse reports no confidence, so its block uses `"floor_tier": "spot_check"` to stay out of auto-accept until calibrated. |
| `PDF_PARSE_PAGE_WORKERS` | 4 | Page-level parallelism within a parse |
| `PARSE_SUBPROCESS_MAX_WORKERS` | (auto) | Parallel parses per worker; empty → `min(cpu_count(), 4)` |
| `BRONZE_LOCAL_DIR` | `/tmp/georag/bronze` | Body-bytes cache |
| `P04P_DUAL_WRITE_ENABLED` | false | Run legacy parser in parallel for A/B |

## 11. Non-PDF parsers

The parser package outlived Dagster. It now lives at
[`src/georag_geoparsers/georag_geoparsers/`](../../../src/georag_geoparsers/georag_geoparsers/)
and is imported by the Hatchet ingest workflows.

| Parser | Format | Used by |
|---|---|---|
| `csv_collar.py`, `csv_lithology.py`, `csv_sample.py`, `csv_survey.py`, `csv_geochronology.py` | CSV | `ingest_tabular` |
| `xlsx_parser.py` | XLSX (multi-sheet, classifier-routed) | `ingest_tabular` |
| `las_parser.py` | LAS well logs | `ingest_well_logs` + `services/ingest/las_ingester.py` |
| `spatial_parser.py`, `qgis_parser.py` | GPKG / GeoJSON / shapefile / QGIS projects | `ingest_spatial` |
| `raster_parser.py`, `erdas_rrd.py` | GeoTIFF and ERDAS rasters | `ingest_spatial`, `tiff_normalize` |
| `xyz_parser.py` | XYZ point cloud | `ingest_spatial` |
| `access_mdb.py`, `dbase_reader.py` | Access MDB, dBASE | `ingest_tabular` (mdbtools) |
| `surpac_parser.py`, `dcip2d_parser.py`, `dcip2d_survey.py` | Surpac strings, DC/IP 2-D geophysics | `ingest_spatial` |
| `_csv_io.py`, `_encoding.py`, `_hole_id.py`, `_sheet_classifier.py`, `_unit_ambiguity.py`, `_vendor_aliases.py`, `_dip_convention.py`, `_survey_interp.py`, `_drill_schema.py`, `_header_match.py` | Helpers | all of the above |

**Gone with the Dagster tree:** `segy_parser.py` (SEG-Y) and
`docx_parser.py` (Word). Neither has a replacement — `segyio` and `obspy`
are not dependencies, so seismic ingest is not a path this system has
today, and Word documents are not an accepted upload type.

The CSV and XLSX audit notes
([project_csv_audit_2026_05_23](../notes/INDEX.md#project_csv_audit_2026_05_23),
[project_xlsx_audit_2026_05_23](../notes/INDEX.md#project_xlsx_audit_2026_05_23))
document delimiter auto-detect, the decimal-comma transform and
multi-sheet `sheet_type=''` auto-dispatch. The concurrency limit they call
the `csv_silver_ingest` pool is now the `ingest_tabular` workflow's Hatchet
concurrency key.
