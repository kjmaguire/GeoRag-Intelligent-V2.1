# Phase 51 Handoff — Master-plan §3 Step 3 (PDF profiler + native parser)

**Document version:** 1.0
**Status:** Doc-phase 51 complete. Doc-phase 52 inheriting.
**Predecessors:** `docs/phase50_handoff.md`, `docs/phase3_master_plan_kickoff.md`,
`docs/adr/0002-04p-stack-replaces-ragflow.md`.

First behavioural step in master-plan §3. Three of the eight
`app.ocr.*` modules graduate from Step 1 skeletons to working
implementations: preflight, profile, parse_native.

---

## 1. What doc-phase 51 delivered

| Module | What it does | Latency baseline (PLS-2024 fixture) |
|---|---|---|
| `app.ocr.preflight` | qpdf/pikepdf preflight: encryption, magic bytes, page count, sha256 | < 5 ms (18 KB native PDF) |
| `app.ocr.profile` | Per-page + document profile classification via pdfplumber text density + table count | ~30 ms (1-page native PDF) |
| `app.ocr.parse_native` | pdfminer.six text extraction with bboxes + pdfplumber tables | ~60 ms/page from earlier smoke-bench |

All three:
- Pure async functions: `asyncio.to_thread` wrapper over a sync implementation
- No database writes — Hatchet `ingest_pdf` step (Step 7) is responsible for persistence
- Typed return dict with locked schema (each module docstring is the contract)

### Heuristic thresholds in `profile.py` (initial; tune in Step 9)

```python
NATIVE_TEXT_DENSITY_MIN = 0.005      # chars / sq-pt
SCANNED_TEXT_DENSITY_MAX = 0.0005
TABLE_HEAVY_TABLES_PER_PAGE_MIN = 3
DOC_TABLE_HEAVY_PAGE_FRACTION = 0.5
DOC_SCANNED_PAGE_FRACTION = 0.8
```

All exposed as module constants so Step 9 corpus tuning can adjust
without rewriting the classifier logic.

---

## 2. Files of record

### New
- `src/fastapi/tests/fixtures/ocr/PLS-2024-Technical-Report.pdf` (committed; 18 KB)
- `src/fastapi/tests/test_ocr_native_path.py` (8 behaviour tests)
- `scripts/phase3_master_plan_step3_verify.sh`

### Modified (skeleton → implementation)
- `src/fastapi/app/ocr/preflight.py`
- `src/fastapi/app/ocr/profile.py`
- `src/fastapi/app/ocr/parse_native.py`
- `src/fastapi/tests/test_ocr_module_imports.py` — added `SKELETON_MODULES` set
  to track which modules are still placeholders; the
  `test_ocr_skeletons_raise_notimplemented` parametrized test now skips
  graduated modules

---

## 3. Verifier status

```
[check1] PASS — 8/8 native path behaviour tests green
[check2] PASS — preflight/profile/parse_native removed from SKELETON_MODULES
[check3] PASS — Step 1 verifier still 3/3 green
[check4] PASS — Step 2 verifier still 6/6 green

=== Phase 3 master-plan Step 3 verifier summary ===
  4/4 checks passed
```

Pytest sample (native path + module imports): **22 passed, 3 skipped**
(the 3 skips are intentional — they cover the 5 remaining skeleton
modules; preflight/profile/parse_native are now in graduated state
and skip the skeleton check).

---

## 4. Decisions made in this phase

### 4.1 Parsers are pure functions; persistence is the Hatchet step's job

`parse_native` returns a dict. It does NOT open an asyncpg connection,
set `app.workspace_id`, or write to silver tables. That layering means
the parser is testable in isolation (which it is, in
`test_ocr_native_path.py`), and the Hatchet `ingest_pdf.parse` step
(Step 7) becomes the orchestrator that:

1. Calls `preflight` → records row in `silver.parser_run_artifacts`
   with `parser_used='preflight'`
2. Calls `profile` → records row in `silver.parser_run_artifacts`
   with `parser_used='profiler'`
3. Dispatches to the right `parse_*` per profile
4. Persists passages to `silver.ingest_extractions` (per-region) +
   `silver.document_passages` (existing)
5. Persists per-page quality to `silver.ocr_page_quality`
6. Persists per-document summary to `silver.document_ingestion_quality`

Step 7 is where the workspace_id GUC handling + asyncpg pool wiring
lives. Keeping it OUT of the parser layer prevents test contamination
and makes the parsers reusable from CLI smoke-benches without DB.

### 4.2 `bbox` returned as `[x0, y0, x1, y1]` lists, not tuples

Lists are JSON-serializable; tuples aren't (json.dumps converts but
then re-loads as lists). Single canonical representation across the
pipeline avoids a coerce-at-write-time step in the Hatchet
persistence layer.

### 4.3 Header detection: cheap heuristic now, Docling later

`parse_native._heuristic_header_detected()` is a 5-line cell-content
heuristic: first row has no None cells, ≥70% are short non-numeric
strings. Good enough to populate the `header_detected` column for
native NI 43-101 tables. Step 5's Docling-backed table-heavy parser
will replace it with TableFormer's structured header detection — the
column already exists from doc-phase 50.

### 4.4 SKELETON_MODULES set tracks implementation state in tests

Rather than maintaining a separate test file per skeleton vs
implementation, `test_ocr_module_imports.py` has a `SKELETON_MODULES`
set listing which modules still raise `NotImplementedError`. The
`test_ocr_skeletons_raise_notimplemented` parametrized test skips
modules outside that set. As doc-phases 52-54 graduate the remaining
5 modules, each commit removes one entry. Clean signal of progress
through Phase 3.

---

## 5. Findings carried over to doc-phase 52+

### 5.1 Profile classifier needs tuning against the 50-PDF corpus (Step 9)

Current thresholds are based on the single PLS-2024 fixture (a clean
modern NI 43-101). Real-world PDFs span a wider distribution. Step 9
will retune all five threshold constants in `profile.py` against the
labeled corpus. The constants are intentionally module-level so
retuning is a one-line edit per threshold.

### 5.2 PDF profiler doesn't yet detect map_heavy

The current `_classify_page` doesn't emit `map_heavy` — that profile
requires image-area-fraction analysis which I deferred (pypdfium2's
object-traversal API is finicky and the smoke-bench doesn't exercise
it). Map-heavy detection lands when Step 5's mixed parser needs to
know about embedded image regions anyway. For now, map-heavy PDFs
will classify as `mixed` or `scanned` and route through Docling;
that's a softer wrong than under-routing them.

### 5.3 pdfplumber's `find_tables()` can throw on edge cases

Wrapped in `try/except Exception → []` per the smoke-bench's
observation that some PDFs trip pdfplumber's internals. This is a
known pdfplumber issue with malformed table structures, not a bug
in our code. The Silver Review UI (Step 8) will surface pages with
zero detected tables but high `table_confidence` heuristic for
human re-check.

---

## 6. Pre-existing carry-overs (unchanged this phase)

From prior handoffs (49, 50), unchanged:
- PaddleOCR fitz workaround locked in `parse_scanned.py` skeleton
- Docling is the slowest path
- WSL2 exposes 6/32 CPUs
- Migration apply path workaround (use psql + manual INSERT migrations)
- Windows ↔ WSL dual-tree sync

From doc-phase 48:
- `phase4_step7` sweep-only flake
- `phase9_step1` docker network name mismatch
- Phase-4 dead-code findings

---

## 7. What doc-phase 52 will do

**Master-plan §3 Step 4 — Scanned parser (PaddleOCR PP-OCRv5 CPU
image-input) + render_page module.**

Two modules graduate:
- `app.ocr.render` — pypdfium2 page-to-image bytes (PNG)
- `app.ocr.parse_scanned` — PaddleOCR PP-OCRv5 on pre-rendered numpy
  arrays (avoids the fitz dependency, per the smoke-bench reference
  pattern documented in the skeleton docstring)

Deliverables:
- Deskew preprocessing (Hough-transform-based, threshold configurable)
- Per-page `ocr_confidence` extracted from PaddleOCR's char-level
  confidences
- Retry policy: max 2 re-OCR retries with escalating engine settings
  (binarization threshold, language hint)
- New fixture: a synthetic scanned PDF (rasterized PLS-2024 first 5
  pages, same shape the smoke-bench uses)
- Behaviour tests in `tests/test_ocr_scanned_path.py`

Verifier: `scripts/phase3_master_plan_step4_verify.sh`. Should
benchmark a known-scanned page through `parse_scanned` and assert:
- per-page `ocr_confidence` in [0, 1]
- `text_lines` count > 0 for a non-blank rasterized page
- Step 1-3 verifiers still green

---

## 8. Master-plan §3 progress

| Step | Status | Doc-phase tick |
|---|---|---|
| 1. `app/ocr/` scaffolding + smoke-bench | ✅ DONE (3/3) | 49 |
| 2. §9.3 + §9.6 silver migrations | ✅ DONE (6/6) | 50 |
| 3. PDF profiler + native parser | ✅ DONE (4/4) | 51 |
| 4. Scanned parser + render | next | 52 |
| 5. Mixed + table-heavy parsers (Docling) | pending | 53 |
| 6. LangGraph OCR Quality Graph | pending | 54 |
| 7. Hatchet `ingest_pdf` cutover + shadow + persistence | pending | 55 |
| 8. Silver Review UI extension | pending | 56 |
| 9. 50-PDF acceptance corpus + sign-off | pending — needs Kyle labeling | 57 |
| 10. RAGFlow retirement + cleanup | pending | 58 |

**3 of 10 steps complete.** Three more skeletons to graduate (render,
parse_scanned, parse_mixed, parse_table_heavy, quality_graph) before
the Step 7 persistence layer can be wired.

---

End of doc-phase 51 handoff. Native path behaves. Doc-phase 52 adds OCR.
