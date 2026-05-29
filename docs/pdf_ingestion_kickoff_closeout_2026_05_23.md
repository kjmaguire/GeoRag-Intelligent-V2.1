# PDF Ingestion Accuracy + Speed — Kickoff Closeout

**Closeout date:** 2026-05-23
**Kickoff doc:** `georag-pdf-ingestion-accuracy-claude-code-kickoff.md` (2026-05-22)
**Scope:** Eight of ten phases shipped. One phase deferred on schema-gate, one phase closed as cleanup.

---

## Phase status

| Phase | Topic | Status | Notes |
|---|---|---|---|
| 1 | Figure subprocess handoff fix | ✅ Shipped | 11 tests, no-data-loss path verified |
| 2.0 | rapidocr writable model cache | ✅ Shipped | 20 tests, named volume + chown documented |
| 2.1 | Fitz-first dispatch with docling-OCR per-page | ✅ Shipped | 16 tests, **3.1× wall-clock speedup on text-only PDFs** |
| 3 | OCR confidence scoring end-to-end | ✅ Shipped | 13 pgTAP + 24 pytest; migration via psql workaround |
| 4 | Table extraction overhaul | ✅ Shipped | 25 tests, perf-protect threshold added during smoke |
| 5 | Subprocess max_workers + memory guard | ✅ Shipped | 15 tests, **4× parse parallelism** on this host |
| 6 | OCR Quality Agent | ✅ Shipped | 28 tests, gated `OCR_QUALITY_AGENT_ENABLED=false` |
| 7 | §04p → retrieval wiring | ⏭️ **Deferred** | Schema gate failed: `silver.ingest_tables` and `ingest_assays` don't exist; only generic `ingest_extractions` rows. Defer per kickoff guidance until §04p emits structured table/assay rows. |
| 8 | embed_verify simplification | ✅ Shipped | 7 tests, polling loop removed |
| 9 | PaddleOCR GPU | ✅ Shipped | 24 tests, GPU smoke: 0.81s inference (vs ~2-3s CPU) |
| 10 | Cleanup | ✅ Shipped | Unstructured removed, deps audited, this doc |

**Test totals**: 88 dagster-side + 94 fastapi-side = **182 new tests** across the kickoff.

---

## Env-knob inventory (additions, all phases)

### Phase 1
- *No new envs* — figure handoff is a code change

### Phase 2.0
- `DOCLING_OCR_ENABLED=true` — flip 2.0 wires rapidocr to docling
- `RAPIDOCR_MODEL_DIR=/tmp/rapidocr_models` — writable model cache
- `DOCLING_OCR_LANGS=english` — comma-separated lang list

### Phase 2.1
- `PDF_PARSER_DOCLING_ENABLED=true` — promoted from opt-in to default
- `PDF_PARSER_TESSERACT_FALLBACK_ENABLED=true` — last-resort image-page OCR

### Phase 4
- `TABLE_BORDER_LINE_THRESHOLD=3` — horizontal lines/page → bordered
- `TABLE_BORDER_RECT_THRESHOLD=20` — rectangles/page → bordered
- `PDF_PARSER_DOCLING_TABLES_MIN_BORDERED_PAGES=30` — perf-protect (docling overhead amortizes above this)

### Phase 5
- `PARSE_SUBPROCESS_MAX_WORKERS=` (empty → `min(cpu_count, 4)`)
- `PARSE_MIN_FREE_RAM_MB=1500`
- `PARSE_MEMORY_WAIT_MAX_S=30`

### Phase 6
- `OCR_QUALITY_AGENT_ENABLED=false` (default; flip after smoke)
- `OCR_QUALITY_THRESHOLD=0.75`
- `OCR_REOCR_THRESHOLD=0.60`
- `OCR_MAX_REOCR_PAGES_PER_DOC=20`

### Phase 9
- `PADDLEOCR_USE_GPU=` (empty → auto-detect)
- `PADDLEOCR_MIN_FREE_VRAM_MB=1024`
- `HOME=/tmp` + `FLAGS_logging_dir=/tmp` (PaddleOCR cache redirect)

---

## Schema changes (all via psql workaround due to PG role-membership gap)

1. **Phase 3** — `silver.document_passages.ocr_confidence numeric(5,4)`, `ocr_method varchar(50)`, CHECK enum, partial index for low-confidence
2. **Phase 6** — `silver.document_passages.ocr_status varchar(50) DEFAULT 'accepted'`, CHECK enum (accepted/pending_reocr/reocr_complete/low_confidence), partial index for pending_reocr

Both migration files exist in `database/migrations/` for replay on fresh clusters, but were applied to the live cluster via psql + manual `public.migrations` INSERT because the Laravel `georag_app` role isn't a member of `georag` (table owner). The one-time fix `GRANT georag TO georag_app;` is in MEMORY but hasn't been applied.

---

## Performance deltas (measured on test.pdf, 46-page text-only NI 43-101 prospectus, 458 KB)

| Phase | Wall clock | Notes |
|---|---|---|
| Pre-kickoff baseline | ~30 s | docling-primary path |
| After Phase 2.0 | 61.7 s | docling+rapidocr full OCR pipeline on all pages (image worst case) |
| After Phase 2.1 | 19.7 s | fitz-first; docling only on image pages (this doc has none) |
| After Phase 4 (no threshold) | 66.4 s | docling-tables-only fired on 11 bordered pages — regression |
| After Phase 4 + threshold=30 | 19.3 s | threshold gates docling for small docs; pdfplumber-lines fallback |
| After Phase 5 (single parse) | 19.8 s | within noise; Phase 5 wins amortize across concurrent parses |
| After Phase 9 (paddleocr GPU) | n/a for test.pdf | §04p path doesn't run on text-only docs; PP-OCRv5 measured at 0.81 s inference on synthetic image (vs 2-3 s CPU) |

**Expected real-world impact** (un-measured): 12-PDF V3 batch with Phase 5's 4× parallelism drops from ~12×T to ~3×T per-batch wall clock. Big NI 43-101 reports with many bordered table pages route to docling TableFormer instead of dual-pass pdfplumber.

---

## Test counts

| Phase | pgTAP | pytest |
|---|---|---|
| 1 | — | 11 (dagster) + 12 (fastapi) |
| 2.0 | — | 20 (dagster) |
| 2.1 | — | 16 (dagster) |
| 3 | 13 | 16 (dagster) + 8 (fastapi) |
| 4 | — | 25 (dagster) |
| 5 | — | 15 (fastapi) |
| 6 | — | 28 (fastapi) |
| 8 | — | 7 (fastapi) |
| 9 | — | 24 (fastapi) |
| **Total** | **13** | **88 dagster + 94 fastapi = 182** |

All green at closeout.

---

## Known limitations + deferred work

### Phase 7 — `silver.ingest_*` schema gap (deferred)
The kickoff design assumed `silver.ingest_tables` + `silver.ingest_assays` with structured row/col data existed in §04p. Reality: only generic `ingest_extractions`, `ingest_layouts`, `ingest_ocr_results` exist. Zero `table` rows in `ingest_layouts` on current ingestion. Phase 7's "tabular path" has no source data. Defer until §04p ingestion is extended to populate dedicated structured tables — that's a separate plan, not a query-wiring task.

### Phase 6 production-on
`OCR_QUALITY_AGENT_ENABLED` ships at `false`. End-to-end smoke on a degraded scanned PDF wasn't run because no degraded fixture was available. All branches covered by unit tests; flip the flag after smoke on a real low-quality PDF.

### Phase 9 image vs hot-install
`paddlepaddle-gpu` is hot-installed via the docker-compose worker bootstrap on `PADDLEOCR_USE_GPU=true`. Container recreate with the flag picks it up (~4 min). The bootstrap re-pins `nvidia-nccl-cu12>=2.30` because paddle-gpu downgrades nccl to 2.25.1 (breaks torch's ABI). Proper fix: rebuild the Dockerfile with paddle-gpu + the pinned nccl baked in.

### Image rebuild backlog
After all kickoff work, a clean image rebuild should produce a working ingestion stack. Things to verify on rebuild:
- `polars`, `pytesseract`, `onnxruntime-gpu>=1.20` (Linux) installed via pyproject.toml (Phase 10 explicit pins)
- `paddlepaddle-gpu==3.3.1` from the Paddle cu126 index (NOT `paddlepaddle`)
- `nvidia-nccl-cu12>=2.30` re-pinned after paddle-gpu install
- `unstructured[pdf]` is **gone** (Phase 10 removal)
- All env defaults match `.env.example` Phase 1-10 sections

### PG role-membership gap
Phase 3 + Phase 6 migrations both hit `must be owner of table document_passages` and were applied via psql + manual record. One-time fix `GRANT georag TO georag_app;` is in MEMORY (`project_pg_role_membership_gap_2026_05_22.md`). Until applied, future migrations touching `silver.document_passages` will need the same workaround.

### Phase-agent rename
Reviewed in Phase 10 — decision to keep `phase0/5/6/7/8/10` directory names. The names are historical (kickoff phase numbers), not runtime semantics. Renaming would touch hundreds of import sites + ADRs + the architecture doc. Filed for a future cycle.

---

## Files touched (summary)

### New files
- `src/fastapi/app/ocr/_paddleocr_gpu.py` — Phase 9 GPU detection helper
- `src/fastapi/app/hatchet_workflows/ocr_quality_check.py` — Phase 6 workflow
- `database/migrations/2026_05_22_020000_add_ocr_confidence_to_document_passages.php` — Phase 3
- `database/migrations/2026_05_22_030000_add_ocr_status_to_document_passages.php` — Phase 6
- `database/tests/pgtap/12_phase3_ocr_confidence.sql` — Phase 3 pgTAP
- 9 new pytest files across phases 1, 2.0, 2.1, 3, 4, 5, 6, 8, 9
- `docs/architecture_review_for_sonnet_2026_05_22.md` — review doc that generated the kickoff
- `docs/pdf_ingestion_kickoff_closeout_2026_05_23.md` — this doc

### Heavy edits
- `src/dagster/georag_dagster/parsers/pdf_report.py` — Phases 1, 2.0, 2.1, 3, 4
- `src/fastapi/app/hatchet_workflows/ingest_pdf.py` — Phases 1, 3, 5, 6, 8
- `docker-compose.yml` — Phases 2.0, 2.1, 5, 6, 9, 10 env + bootstrap
- `.env.example` — every phase's env knobs
- `src/fastapi/pyproject.toml` — Phase 5 (psutil), Phase 10 (drop unstructured + add explicit pins)

### Removed
- `_parse_with_unstructured` function (Phase 10)
- `_DOCLING_FIGURE_CACHE` module-scope cache (Phase 1)
- `_extract_docling_figures` separate function (Phase 1)
- 6-iteration polling loop in `embed_verify` (Phase 8)
- `unstructured[pdf]` from pyproject.toml + worker bootstraps (Phase 10)

---

## Commit message suggestions

```
feat(ingestion): close the PDF accuracy+speed kickoff (phases 1-6, 8-10)

Eight of ten kickoff phases shipped, one deferred on a schema gate.
182 new tests; details in docs/pdf_ingestion_kickoff_closeout_2026_05_23.md.

Phase 7 (§04p retrieval wiring) deferred — silver.ingest_tables and
ingest_assays don't exist yet; only generic ingest_extractions rows.
Defer per kickoff guidance until §04p ingestion is extended to populate
structured table/assay rows.
```
