# Chapter 05 — PDF Stack §04p

In-process replacement for the deleted RAGFlow service ([ADR-0002](../../adr/)).
Everything runs inside the `hatchet-worker-ingestion` (and occasionally the
`fastapi`) container — no separate parsing process to deploy or scale.

> All file paths in this chapter are relative to the repo root.
>
> **⚠️ OCR/VL engines upgraded 2026-06.** The Stage-4 OCR and Stage-6 VL
> tiers changed: PaddleOCR 2.10 → **3.7** (new `.predict()` API),
> Tesseract → **5.5.2 from source**, VL → **Qwen3-VL-8B** (gated; default
> still Qwen2.5-VL-7B). The pipeline *shape* below is unchanged; for the
> engine versions + call-site API see
> [Ch 18 §3](18-model-stack-evolution.md) (ADRs 0015/0016/0017).

## 1. Entry point

`ingest_pdf.parse()` ([src/fastapi/app/hatchet_workflows/ingest_pdf.py](../../../src/fastapi/app/hatchet_workflows/ingest_pdf.py))
calls `_run_parser_subprocess()` which delegates to:

[src/dagster/georag_dagster/parsers/pdf_report.py](../../../src/dagster/georag_dagster/parsers/pdf_report.py) → `parse_pdf_report(body_bytes, sha256)`

Why a subprocess pool: pdfminer/PaddleOCR/docling each carry global state.
A crash in one PDF must not poison the worker. The pool also bounds memory
per parse — see `_wait_for_memory_headroom()` in ingest_pdf.

## 2. The seven-stage pipeline

```
body_bytes
   │
   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Stage 1 — PREFLIGHT      pdf_preflight.py                               │
│   qpdf --check          (structure)                                     │
│   pypdfium2 page count                                                  │
│   sha256 + size cap                                                     │
│   magic-byte sniff                                                      │
└───────────────────────┬─────────────────────────────────────────────────┘
                        ▼
┌─────────────────────────────────────────────────────────────────────────┐
│ Stage 2 — FAST-PATH NATIVE TEXT     pdf_extract.py → PyMuPDF (fitz)     │
│   per-page get_text("blocks")                                           │
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
│ Stage 4 — LAYOUT + OCR    pdf_layout.py + pdf_ocr.py                    │
│   image-only pages, OR low-confidence fitz pages                        │
│   PDF_PARSER_DOCLING_ENABLED=true → docling + rapidocr (GPU)            │
│   else → tesseract (psm=3) (CPU fallback)                               │
│   silver.ocr_page_quality + silver.low_confidence_page_reviews          │
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
│ Stage 6 — VISUAL-LANGUAGE PASS (opt-in)    pdf_vl.py                    │
│   Qwen2.5-VL-7B-Instruct on vLLM (separate VLLM_MODEL config)           │
│   describes figures + extracts numeric values from charts               │
│   gated on DOCLING_VL_ENABLED + figure_extractor.py confidence          │
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
| Preflight | [src/fastapi/app/services/pdf_preflight.py](../../../src/fastapi/app/services/pdf_preflight.py) | `preflight()`, `_qpdf_check()`, `_page_count()` |
| Native text | [src/fastapi/app/services/pdf_extract.py](../../../src/fastapi/app/services/pdf_extract.py) | `extract_native_text_pages()` (PyMuPDF primary; pdfminer fallback) |
| Tables | [src/fastapi/app/services/pdf_extract.py](../../../src/fastapi/app/services/pdf_extract.py) + [pdf_layout.py](../../../src/fastapi/app/services/pdf_layout.py) | `extract_tables_diverse()` — pdfplumber + camelot strategies |
| OCR | [src/fastapi/app/services/pdf_ocr.py](../../../src/fastapi/app/services/pdf_ocr.py) | `ocr_page_docling()`, `ocr_page_tesseract()`; docling primary, tesseract fallback |
| Coordinates | [src/fastapi/app/services/pdf_coordinates.py](../../../src/fastapi/app/services/pdf_coordinates.py) | Maps OCR text → page-relative bboxes for citation span resolver |
| Rendering | [src/fastapi/app/services/pdf_render.py](../../../src/fastapi/app/services/pdf_render.py) | `render_page_png()` → uploads to SeaweedFS `bronze-raster` bucket |
| VL pass | [src/fastapi/app/services/pdf_vl.py](../../../src/fastapi/app/services/pdf_vl.py) | `describe_figure_vl()` — calls Qwen2.5-VL on the vllm endpoint |
| Figure linking | [src/fastapi/app/agent/figure_extractor.py](../../../src/fastapi/app/agent/figure_extractor.py) | Figure → caption nearest-text linking v1 |
| Dagster glue | [src/dagster/georag_dagster/parsers/pdf_report.py](../../../src/dagster/georag_dagster/parsers/pdf_report.py) | `parse_pdf_report()` orchestrator |

## 4. Performance fixes that landed in 2026-05

[project_parse_perf_2026_05_22](../notes/INDEX.md#project_parse_perf_2026_05_22):

1. PyMuPDF promoted to primary native-text parser (was pdfminer).
2. Parallel pdfplumber for tables.
3. Diverse table-probe (multiple pdfplumber strategies).
4. OCR-skip probe — skips OCR for pages already covered by native text.
5. PDF body cached in memory per parse — no re-reads from disk.
6. Single-pass tables — one walk over pages instead of per-table calls.
7. Slot tuning — pool size from `min(os.cpu_count(), 4)`.
8. Docling moved to GPU.

Result: 3-5× speedup on text-heavy NI 43-101 PDFs.

## 5. Performance & quality fixes that landed in 2026-05 (Phase 2)

[project_overnight_run_2026_05_22](../notes/INDEX.md#project_overnight_run_2026_05_22):

- Tesseract bumped from default psm to `psm=3` (auto page seg with OSD).
- Tables now read all pages, not just the first hit page.
- `§04p` re-enabled after the Phase 1 freeze.
- Docling made the opt-in primary parser; rapidocr handles OCR.
- Figure→caption linking v1 with MinIO uploads.

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
| `PDF_PARSER_DOCLING_ENABLED` | true | Docling becomes primary OCR engine |
| `DOCLING_OCR_ENABLED` | true | rapidocr backend on (GPU) |
| `RAPIDOCR_MODEL_DIR` | `/tmp/rapidocr_models` | Shared model cache (named volume) |
| `PDF_PARSER_TESSERACT_FALLBACK_ENABLED` | true | Fall back to tesseract on docling failure |
| `PDF_PARSE_PAGE_WORKERS` | 4 | Page-level parallelism within a parse |
| `PARSE_SUBPROCESS_MAX_WORKERS` | (auto) | Parallel parses per worker; empty → `min(cpu_count(), 4)` |
| `BRONZE_LOCAL_DIR` | `/tmp/georag/bronze` | Body-bytes cache |
| `P04P_DUAL_WRITE_ENABLED` | false | Run legacy parser in parallel for A/B |

## 11. Non-PDF parsers (in the same `parsers/` directory)

[src/dagster/georag_dagster/parsers/](../../../src/dagster/georag_dagster/parsers/):

| Parser | Format | Used by |
|---|---|---|
| `csv_collar.py`, `csv_lithology.py`, `csv_sample.py`, `csv_survey.py`, `csv_geochronology.py` | CSV | Dagster bronze→silver |
| `xlsx_parser.py` | XLSX (multi-sheet, classifier-routed) | Dagster |
| `las_parser.py` | LAS well logs | Dagster + `services/las_ingester.py` |
| `segy_parser.py` | SEG-Y seismic | Dagster |
| `spatial_parser.py` | GPKG/GeoJSON/shapefile | Dagster |
| `raster_parser.py` | GeoTIFF | Dagster |
| `xyz_parser.py` | XYZ point cloud | Dagster |
| `docx_parser.py` | Word documents | FastAPI/Dagster |
| `_csv_io.py`, `_encoding.py`, `_hole_id.py`, `_sheet_classifier.py`, `_unit_ambiguity.py`, `_vendor_aliases.py`, `_dip_convention.py`, `_survey_interp.py` | Helpers | All the above |

The CSV audit and XLSX audit memory notes
([project_csv_audit_2026_05_23](../notes/INDEX.md#project_csv_audit_2026_05_23),
[project_xlsx_audit_2026_05_23](../notes/INDEX.md#project_xlsx_audit_2026_05_23))
document recent fixes: delimiter auto-detect, decimal-comma transform,
multi-sheet workbook sheet_type='' auto-dispatch, and the shared
`csv_silver_ingest` Dagster concurrency pool.
