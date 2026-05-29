# ADR 0005: Normalize TIFF scans to PDF and route through the §04p PDF stack

- **Date**: 2026-05-23
- **Status**: Accepted (implementation in flight under the same date's autonomous run)
- **Deciders**: Kyle Maguire (SME)
- **Supersedes**: the standalone TIFF OCR path at `src/fastapi/app/services/ingest/tiff_ocr_ingester.py` (Phase E.1, doc-phase 182) — retained for one deprecation cycle, then removed.
- **Related**: [[adr-0002-04p-stack-replaces-ragflow]], the 5/22 PDF overhaul (commit `3463195`), [[bronze-minio-unification]] (commit `91fa9a2`), `src/fastapi/app/hatchet_workflows/ingest_pdf.py`, `src/dagster/georag_dagster/parsers/pdf_report.py`

## Context

The 2026-05-22 overnight push delivered five major improvements to the PDF
ingest pipeline — `psm=3` multi-column OCR, all-page table extraction,
the `§04p` dual-write into five quality tables, docling as opt-in
primary parser, and figure→caption linking with MinIO upload. The §04p
PDF stack now writes `ocr_confidence`, `ocr_status`, structured table
sections, per-page bbox / page_first / page_last on every passage, has
atomic persist with heartbeat resilience, an inline embed dispatch into
Qdrant, and a confidence-driven retry agent (`ocr_quality_check`).

A separate TIFF ingest path exists at
`src/fastapi/app/services/ingest/tiff_ocr_ingester.py`, built for the
Cameco WSGS uranium archive in doc-phase 182. It is **materially below
PDF parity** (audit 2026-05-23):

- `--psm 6` instead of `--psm 3` — single-column assumption fragments multi-column text
- no image preprocessing (no deskew, no upscale, no adaptive threshold, no sharpen)
- no `ocr_confidence` or `ocr_status` capture
- silent 50-page cap (`_MAX_PAGES`); long TIFFs lose everything past page 50
- no table extraction (scanned tables flatten to garbled prose)
- no figure extraction
- no docling / PaddleOCR / `§04p` dual-write
- no `ocr_quality_check` retry agent (`retries=0` on the workflow task)
- not embedded inline into Qdrant — relies on the daily embed cron
- no UploadController route, no MinIO sensor prefix — manual Hatchet trigger only

The user-facing impact: a scanned NI 43-101 ingested as a TIFF stack
returns lower-quality chat answers, missing tables, missing figures, and
no provenance for the OCR confidence layer than the same content
ingested as a PDF.

Two paths exist forward (others considered + rejected in the "Options
considered" table below). The architectural shape under discussion:

| Option | Shape |
|---|---|
| Status quo + targeted fixes | Keep `tiff_ocr_ingester.py`; backport the 5/22 PDF improvements piece by piece (psm=3, preprocessing, confidence, docling, etc.) into the TIFF path. |
| **Normalize TIFF → PDF, route through `ingest_pdf`** | A small Hatchet task wraps `img2pdf` (multi-page TIFF → lossless multi-page PDF) and emits the canonical PDF into `bronze/reports/`. The existing `ingest_pdf` workflow does the rest. The original TIFF is preserved in `bronze/tiff/`. |

## Options considered

| Option | Cost | Decision |
|---|---|---|
| A. Parallel `ingest_tiff` workflow that mirrors `ingest_pdf` step-by-step | Medium-high (~2 weeks). Every future PDF fix has to be backported. Two OCR codepaths to maintain. | Rejected — exact drift bug class (cf. the bronze-MinIO-unification carryover, the WSL clone reversal). |
| **B. Normalize TIFF → PDF at the bronze edge via `img2pdf`, route through `ingest_pdf`.** | Small (~1 day). 100 % of the 5/22 stack applies for free. Storage overhead ~10–15 % for the derived PDF. Reversible — original TIFF preserved. | **Chosen.** |
| C. Generalize `ingest_pdf` into `ingest_document` that accepts PDF or TIFF natively | Large refactor of a 3,800-line hot path. High regression risk on PDF traffic. The §04p stack and docling assume a `path: pdf` contract in places. | Rejected as v1; reconsider as v2 once we have data on TIFF-specific tuning needs that the normalize path can't address. |
| D. Extend docling-as-primary to accept image input directly inside `_parse_with_docling` | Mid-ground. Docling's `InputFormat.IMAGE` mode is less mature than its PDF mode; skips the multi-page + section-split + figure-link machinery that's PDF-specific in the parser. | Rejected — produces an asymmetric pipeline that handles single-image TIFFs well and multi-page badly. |

## Decision

**Normalize multi-page TIFF to PDF at the bronze edge, then trigger the
existing `ingest_pdf` workflow against the derived PDF.** Implementation
shape:

1. **New Hatchet workflow** `tiff_normalize` at
   `src/fastapi/app/hatchet_workflows/tiff_normalize.py`.
   Input: `IngestPdfInput`-shaped (workspace_id + project_id + minio_key
   + actor_id + correlation_token). The `minio_key` points at the TIFF
   under `tiff/{project_id}/...`.
2. **Single task `normalize`**:
   - Stream the TIFF from MinIO to a `NamedTemporaryFile`
   - Compute SHA-256 over the source bytes
   - Idempotency check: if `bronze/reports/{derived_key}` already exists
     with the same `x-georag-derived-from-tiff-sha256` metadata, **skip
     normalize, trigger ingest_pdf directly**.
   - Otherwise, run `img2pdf.convert()` — lossless wrapping of the
     per-frame image into a single multi-page PDF. JPEG-in-TIFF embeds
     without re-encode; LZW/uncompressed RGB encodes once as Flate.
   - Upload derived PDF to `bronze/reports/{project_id}/{timestamp}_{stem}.pdf`
     with `x-georag-derived-from-tiff-sha256` metadata stamped.
   - Trigger `ingest_pdf.aio_run_no_wait(IngestPdfInput(minio_key=<new>, ...))`.
   - Return the workflow_run_id of the downstream PDF ingest.
3. **UploadController** accepts `'reports'` category with extension
   `tif|tiff|TIF|TIFF` in addition to `pdf`. The category dispatch checks
   the extension and routes to `tiff_normalize` instead of `ingest_pdf`
   when the upload is a TIFF.
4. **MinIO sensor** (`src/dagster/georag_dagster/definitions.py`) adds a
   `tiff/` → `bronze_tiff_normalize` prefix entry. Bronze asset is a thin
   wrapper that triggers `tiff_normalize` via the Hatchet API.
5. **Audit trail**: `silver.reports.parser_used` is stamped as
   `tiff-normalize→<downstream-parser>` (e.g. `tiff-normalize→docling`)
   so the lineage is traceable from chat citation back to TIFF source.
6. **Original TIFF preserved** in `bronze/tiff/` (not deleted post-derive),
   so a reprocessing pass can re-derive at any future stack version.

## Consequences

### What stays the same
- All Hard Rules (4 + 5 — citations + six-layer hallucination prevention)
  apply unchanged because the downstream PDF stack is unchanged.
- The existing `ingest_pdf` contract, `IngestPdfInput`, and the Laravel
  `/internal/v1/shadow/ingest_pdf/trigger` endpoint don't change.
- The `ocr_quality_check` confidence-driven retry agent operates on the
  derived PDF same as any other PDF.

### What this ADR enables
- TIFF traffic gets every 5/22 PDF improvement automatically:
  psm=3 OCR, docling layout + tables, PaddleOCR opt-in, figure→caption
  linking, ocr_confidence per passage, atomic persist, inline embed
  dispatch, §04p dual-write into the 5 quality tables.
- Future PDF improvements continue to apply for free.
- Chat citations on TIFF-sourced content link back to the same
  `silver.document_passages` rows as PDF content — single retrieval path.
- The `silver.reports.figures` JSONB + the new Figures tab in
  `Foundry/ReportView` ([[figures-presign-2026-05-23]]) light up for
  TIFFs.

### What this ADR closes off
- Backporting the 5/22 fixes into `tiff_ocr_ingester.py` (option A).
- Maintaining two OCR codepaths.

### Storage + cost
- Derived PDF lives alongside the original TIFF. img2pdf is lossless;
  derived PDF size ≈ TIFF size × 1.05–1.15. Acceptable per the master
  plan's storage budget.
- One additional Hatchet task per TIFF upload (the normalize step).
  Wall-clock overhead: <2 s for a typical multi-page TIFF; img2pdf is
  in-memory wrap, not re-encode.

### Risks + mitigations
- **Risk**: img2pdf doesn't handle every TIFF variant (e.g. 16-bit per
  channel, exotic CMYK, OJPEG). **Mitigation**: try/except around the
  convert call; on failure, log a structured `tiff_normalize_failed`
  event with the variant details, emit a non-200 to the Hatchet workflow
  and surface the original TIFF in the IngestQuality admin page for
  manual triage. Original TIFF is preserved either way.
- **Risk**: A pathologically large TIFF (>2 GB single file, multi-page
  satellite mosaic) blows the in-memory wrap. **Mitigation**: cap input
  size at 2 GB (matches the existing Laravel upload cap from
  [[upload-size-stack-2026-05-21]]). Larger files route to the
  `silver_raster` GeoTIFF path (which is a separate concern — those
  aren't documents, they're rasters).
- **Risk**: docling on a derived PDF that's effectively just image pages
  takes longer than tesseract-only would have. **Mitigation**: this is
  the right behaviour — docling's table + figure extraction is the
  feature, not a bug; if a TIFF turns out to be just text scans with no
  tables / figures, the ocr_quality_check agent will detect low
  table/figure yield and short-circuit accordingly.

### Open questions to revisit
- Should the `tiff/` bucket prefix be `documents/tiff/` to distinguish
  from `geophysics/tiff/` (raster TIFFs)? **Decision: `tiff/` for v1.**
  Geophysics raster TIFFs already land at `geophysics/` (handled by the
  silver_geophysics asset, not the document OCR path).
- Should the original TIFF be moved to a cold-storage tier after
  successful derive? **Deferred.** Current SeaweedFS deployment doesn't
  have tiered storage wired (cf. [[adr-0001-seaweedfs-replaces-minio]]).
- Should we also normalize PNG / JPG document scans? **Yes, eventually,
  via the same workflow** — the wrapper is format-agnostic. Out of scope
  for this ADR; tracked as a follow-up.

## Deprecation path for `tiff_ocr_ingester.py`

1. **2026-05-23 (this ADR)**: ship `tiff_normalize`. Both paths exist.
   New uploads route to the normalize path.
2. **+1 week**: re-process the Cameco WSGS 028N079W36 cluster through
   the normalize path. Diff the resulting `silver.document_passages`
   against the existing rows; quality should be at-or-better on every
   metric. Update [[bsg-buildout-2026-05-22]] memory with the diff
   results.
3. **+2 weeks**: if the diff is clean, delete the rows the old path
   wrote (or keep with `parser_used='tesseract-tiff-deprecated-v1'` for
   provenance), and remove `src/fastapi/app/services/ingest/tiff_ocr_ingester.py`
   + `src/fastapi/app/hatchet_workflows/tiff_ocr_cluster.py`. Update the
   schema-search section of `georag-architecture.html` §04d to reflect
   the single OCR path.

## References

- [src/fastapi/app/hatchet_workflows/ingest_pdf.py](../../src/fastapi/app/hatchet_workflows/ingest_pdf.py) — downstream workflow
- [src/dagster/georag_dagster/parsers/pdf_report.py](../../src/dagster/georag_dagster/parsers/pdf_report.py) — the §04p parser
- [src/fastapi/app/services/ingest/tiff_ocr_ingester.py](../../src/fastapi/app/services/ingest/tiff_ocr_ingester.py) — the path this ADR retires
- [docs/adr/0002-04p-stack-replaces-ragflow.md](0002-04p-stack-replaces-ragflow.md) — the §04p decision this builds on
- `img2pdf` library: lossless TIFF→PDF wrapping, BSD license
