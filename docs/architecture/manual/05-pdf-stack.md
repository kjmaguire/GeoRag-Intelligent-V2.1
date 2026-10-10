# Chapter 05 — PDF Stack §04p

> **Reconciled 2026-09-07** against `src/fastapi/app/services/ingest/pdf_report.py`,
> `services/ingest/`, `src/fastapi/pyproject.toml` and `docker-compose.yml`.
> The pipeline description was current through ADR-0019; what was stale was
> the library stack (PyMuPDF was removed on licence grounds), the vision
> stage (vLLM), the container name and the parser table, which pointed into
> the deleted `src/dagster/` tree.
>
> **Corrected 2026-10-10.** This chapter cited `services/pdf_extract.py` and
> `services/pdf_coordinates.py` for native text, tables and citation
> coordinates. Neither exists: both were deleted 2026-10-06 with the other
> §04p Stage-3 services that read cache tables no migration creates
> (`silver.pdf_text_blocks`, `silver.pdf_coordinates`). Native text and tables
> are extracted inside `services/ingest/pdf_report.py`, and there is no
> bbox/coordinate stage today: a passage records `page_first` / `page_last`,
> not a bounding box.

In-process replacement for the deleted RAGFlow service ([ADR-0002](../../adr/)).
Everything runs inside the single `hatchet-worker` container (and
occasionally `fastapi`) — no separate parsing process to deploy or scale.
There is no `hatchet-worker-ingestion`; the pools were merged
([Ch 07 §2.1](07-orchestration.md)).

> All file paths in this chapter are relative to the repo root.
>
> **OCR stack updated 2026-09-02 (ADR-0019), rehosted 2026-09-08
> ([ADR-0022](../../adr/0022-aws-replaces-azure-as-the-production-cloud.md)).**
> Cohere Parse is the primary scanned-page OCR engine — on Amazon Bedrock
> now, on Azure AI Foundry before — with Tesseract retained as the
> last-resort fallback. The MODEL did not change, only the host, and its
> wire contract has **never** been empirically verified on either.
> Azure Document Intelligence (2026-07-29 →
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
│ Stage 2 — NATIVE TEXT    pdf_report.py::_parse_with_fitz (pypdfium2)    │
│   per-page text in reading order; PER_PAGE_MIN_CHARS (80) gate and a    │
│   native-text quality screen: a short or garbled page is queued for OCR │
│   pdfplumber (pdfminer.six) runs only if pdfium fails on the whole file:│
│   pdf_report.py::_parse_with_pdfplumber                                 │
└───────────────────────┬─────────────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Stage 3 — TABLES        pdf_report.py → pdfplumber                      │
│   _extract_resource_tables (resource / reserve pages) and               │
│   _extract_all_tables_as_sections (every page: bordered tables by the   │
│   lines strategy, borderless by the text strategy); each surviving table│
│   becomes a section, so it is chunked and embedded like prose           │
└───────────────────────┬─────────────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Stage 4 — OCR    services/ingest/pdf_report.py                          │
│   image-only pages, OR low-content fitz pages                           │
│   Cohere Parse (Bedrock) primary; Tesseract last-resort fallback       │
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
│   services/ingest/page_vision_client.py → a Bedrock vision model        │
│   the description BECOMES the passage text, so reranker, citations and  │
│   §04i numeric grounding all work on it like any other passage          │
│   inert unless IMAGE_VERBALIZATION_ENABLED; runs on an hourly cron      │
│   (verbalize_page_images), not inline during ingest                     │
└───────────────────────┬─────────────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Stage 7 — PERSIST     ingest_pdf.persist                                │
│   silver.reports (lineage on the row: source_object_key,                │
│   source_file_sha256, parser_used), silver.document_passages,           │
│   silver.shadow_runs, audit.audit_ledger,                               │
│   silver.review_queue (OCR pages the router sends to review)            │
│   No bronze.provenance row: source_row / source_col_map mean nothing    │
│   for a PDF (tests/test_provenance_coverage.py)                         │
└─────────────────────────────────────────────────────────────────────────┘
```

## 3. Per-stage code map

| Stage | File | Key functions |
|---|---|---|
| Preflight | [src/fastapi/app/hatchet_workflows/ingest_pdf.py](../../../src/fastapi/app/hatchet_workflows/ingest_pdf.py) | `preflight()` — sha256, magic bytes, pikepdf open, page count, password-protected rejection |
| Native text | [`services/ingest/pdf_report.py`](../../../src/fastapi/app/services/ingest/pdf_report.py) | `_parse_with_fitz()` — per-page text from **pypdfium2** (PDFium; the `fitz` name is a stable wire label, PyMuPDF is gone), the `PER_PAGE_MIN_CHARS` gate and native-text screen that route a page to OCR; `_parse_with_pdfplumber()` (**pdfplumber** / pdfminer.six) only when pdfium fails on the whole file. Runs inside the parse subprocess pool |
| Tables | [`services/ingest/pdf_report.py`](../../../src/fastapi/app/services/ingest/pdf_report.py) | `_extract_resource_tables()` and `_extract_all_tables_as_sections()` — **pdfplumber** `find_tables()` with the lines / text strategies; each table becomes a section. There is no `pdf_layout.py` and camelot is not a dependency |
| OCR | [src/fastapi/app/services/ingest/pdf_report.py](../../../src/fastapi/app/services/ingest/pdf_report.py) + [cohere_parse_client.py](../../../src/fastapi/app/services/ingest/cohere_parse_client.py) + [html_table.py](../../../src/fastapi/app/services/ingest/html_table.py) | Cohere Parse v5 primary (one page image per request, HTML tables → grids), Tesseract fallback |
| Page attribution | [`services/ingest/pdf_report.py`](../../../src/fastapi/app/services/ingest/pdf_report.py) | `_build_page_index()` maps each chunk's character span to `page_first` / `page_last`; `text_pages` / `text_page_coverage_pct` on the result say which pages produced text. `pdf_coordinates.py` (page-relative bboxes for a citation span resolver) was deleted 2026-10-06 — the `bbox_*` columns on `silver.document_passages` are not filled |
| Rendering | [`services/pdf_render.py`](../../../src/fastapi/app/services/pdf_render.py) | renders page PNGs into the bronze raster prefix — SeaweedFS in dev, S3 in production, behind the one `STORAGE_BACKEND` switch ([Ch 02 §4](02-data-stores.md)) |
| Page verbalization | [`services/ingest/page_verbalizer.py`](../../../src/fastapi/app/services/ingest/page_verbalizer.py) + [`page_vision_client.py`](../../../src/fastapi/app/services/ingest/page_vision_client.py) | a vision model describes the page; the description becomes the passage text. **`BEDROCK_VISION_MODEL_ID` has no default and the feature refuses to run without one** — Bedrock has no equivalent of the retired `gpt-5-mini`, and guessing a substitute would silently change what every image passage says (ADR-0022). `services/pdf_vl.py` still exists and is constructed in the FastAPI lifespan, but its docstring describes an Ollama/vLLM backend that is gone — the live path is the verbalizer |
| Figure linking | [src/fastapi/app/agent/figure_extractor.py](../../../src/fastapi/app/agent/figure_extractor.py) | Figure → caption nearest-text linking v1 |
| Hatchet workflow | [src/fastapi/app/hatchet_workflows/ingest_pdf.py](../../../src/fastapi/app/hatchet_workflows/ingest_pdf.py) | `parse_pdf_report()` invocation and atomic Silver persistence |

## 4. Performance fixes that landed in 2026-05

[project_parse_perf_2026_05_22](../notes/INDEX.md#project_parse_perf_2026_05_22):

1. ~~PyMuPDF promoted to primary native-text parser~~ — **reversed.** PyMuPDF was removed in the 2026-05-27 licence audit (AGPL-3.0); **pypdfium2** (Apache-2.0) is the native-text parser now, with pdfplumber / pdfminer.six as the whole-file fallback and the table extractor.
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

These tables exist (migrations `2026_05_12_1800xx`) but **no code under
`src/fastapi/app` writes them today** (checked 2026-10-10); they were the
target of the deleted Stage-3 services:
- [silver.parser_run_artifacts](03-schemas.md) — per-stage artifact list with sizes/durations.
- `silver.ocr_page_quality` — per-page confidence + char count.
- `silver.table_extraction_quality` — per-table cells / strategy / score.
- `silver.document_ingestion_quality` — overall summary row.
- `silver.low_confidence_page_reviews` — pages routed to human review.

What a parse records instead: the `ocr_quality_assessment` warning per OCR'd
page on the parse result, `silver.review_queue` rows for pages the router
sends to review, and `text_page_coverage_pct` on `silver.reports` (over the
document's real page count).

## 10. Env knobs

From [docker-compose.yml:2039-2065](../../../docker-compose.yml):

| Env var | Default | Effect |
|---|---|---|
| `OCR_ENGINE` | `cohere_parse` (since 2026-10-04; unset = hosted, like `EMBEDDING_BACKEND`) | Selects the remote OCR engine. A worker with no `COHERE_API_KEY` logs ONE CRITICAL per process and runs Tesseract; `tesseract` stays selectable (the Helm air-gapped profile sets it). Retired values (`azure_document_intelligence`) log CRITICAL and run Tesseract — the engine never silently selects something else. |
| `COHERE_API_KEY` | unset | The Cohere API key, shared with `LLM_BACKEND=cohere`. **Unset means every page runs Tesseract** after one CRITICAL line — no table structure, no error |
| `COHERE_PARSE_MODEL` | `parse-v5.0` | Cohere's own model name. A plain name, not an endpoint ARN — there is no endpoint indirection on this host (ADR-0023) |
| `COHERE_PARSE_MAX_PIXELS` | 20000000 | Pixel cap for the rendered page image; oversized sheets are downscaled (no tiling). Binds only above ~tabloid, where the 200 DPI ceiling stops applying: A0 renders at ~112 DPI. Raised from 4000000 on 2026-09-24; a 20 MP page was accepted by the live probe the same day |
| `COHERE_PARSE_TIMEOUT_S` / `_OUTPUT_FORMAT` / `_INCLUDE_IMAGE_DESCRIPTIONS` | 120 / blocks / 0 | Per-request timeout, response shape, and whether Parse's figure descriptions enter the retrievable text |
| `OCR_PAGES_PER_BATCH` | 8 | Pages rendered together and posted concurrently as one group (in-flight requests capped by `PDF_OCR_PAGE_CONCURRENCY`) |
| `OCR_MAX_PAGES_PER_DOC` | 300 | Per-document cap on pages sent to the remote engine; the rest go to Tesseract and the parse carries an `ocr_page_budget_exhausted` warning |
| `PDF_PARSE_MODE` | `all` (code, compose and production since 2026-10-04; `.env.example` keeps `ocr_only` on purpose) | Which pages the remote engine reads. `ocr_only` = image-only pages, as before; also what a worker degrades to when the engine is not usable (`OCR_ENGINE=tesseract` or no key). `tables` = text-layer pages with a detected table / resource-reserve hint / "Table N" caption are also read by Parse and its tables replace pdfplumber's for that page (prose stays native). `all` = every page goes through Parse, native text only as the fallback when the engine fails, is over `OCR_MAX_PAGES_PER_DOC`, or returns empty/short output (over-budget text-layer pages keep native text; they are never sent to Tesseract). Needs `OCR_ENGINE=cohere_parse` and `COHERE_API_KEY`, else it behaves as `ocr_only` (quiet for the built-in default, a warning/CRITICAL when explicitly set); an unknown value logs CRITICAL and behaves as `ocr_only`. Production (Terraform, next to `OCR_ENGINE`) runs `all` since 2026-09-29 (SME decision after the `Parse vs native comparison` workflow, `scripts/ops/parse_vs_native.py`, matched native text at 99.5% on a synthetic 7-page report; table pages not yet measured). In `all`, an engine reading shorter than 60% of the page's own text layer (`PARSE_MIN_NATIVE_RATIO`) is rejected: the native text is kept and a `page_parse_under_read` warning recorded. Parse tables are indexed once (a `[Table k, page N]` placeholder in the narrative, the grid as a `Table (OCR, ...)` section). `pdf_report.py`'s module docstring is the reference. |
| `PDF_PARSER_TESSERACT_FALLBACK_ENABLED` | true | Controls the Tesseract floor of the per-page OCR loop ONLY. `false` with Parse usable keeps Parse (and `tables`/`all`) running and removes the floor; it used to switch the whole loop off. The whole-document scanned-PDF OCR path is not gated by it |
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
