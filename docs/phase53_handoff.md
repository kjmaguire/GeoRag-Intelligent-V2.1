# Phase 53 Handoff — Master-plan §3 Step 5 (mixed + table-heavy parsers)

**Document version:** 1.0
**Status:** Doc-phase 53 complete. Doc-phase 54 inheriting.
**Predecessors:** `docs/phase52_handoff.md`, `docs/phase3_master_plan_kickoff.md`,
`docs/adr/0002-04p-stack-replaces-ragflow.md`.

Two skeleton modules graduate: `parse_mixed` (Docling layout-first)
and `parse_table_heavy` (pdfplumber → Docling fallback). Only one
skeleton remains: `quality_graph`.

---

## 1. What doc-phase 53 delivered

| Module | What it does | Latency baseline (PLS-2024 7-page fixture) |
|---|---|---|
| `app.ocr.parse_mixed` | Docling with `do_ocr=False`; per-region passages + layouts + tables | ~12 sec/page (full Docling pipeline) |
| `app.ocr.parse_table_heavy` | pdfplumber first pass + Docling fallback for pages with no pdfplumber tables | pdfplumber: ~50 ms/page; Docling fallback: ~12 sec/page only when needed |

Plus:
- `app.ocr._docling_common` — shared helpers: `normalize_label` (Docling
  label → silver enum), `_bbox_from_prov`, `_page_from_prov`,
  `run_docling_no_ocr`, `extract_table_cells`
- `tests/test_ocr_mixed_path.py` — 9 behaviour tests
- `scripts/phase3_master_plan_step5_verify.sh` — 6 checks

### Why do_ocr=False

Docling's default OCR backend is RapidOCR, which on import tries to
download model ONNX files to `/usr/local/lib/python3.13/site-packages/rapidocr/models/`
— not writable for the FastAPI container's `www-data` user (same
class of issue as PaddleOCR before doc-phase 52 fixed it).

Rather than wire a second cache-dir workaround for RapidOCR, the
cleaner design fits ADR-0002's separation-of-concerns: **Docling does
layout, parse_scanned does OCR.** Mixed PDFs whose pages have no text
layer come back from parse_mixed with empty text content and the
page numbers in `pages_needing_ocr` — the Hatchet orchestrator (Step 7)
then calls parse_scanned on those pages.

Concrete benefit: parse_mixed completes a 7-page native PDF in ~5 sec
real-time on this Threadripper (Docling cold-load + ~7 fast pages
with no OCR pass), versus the ~84 sec it would take if Docling did
its own OCR per scanned page.

---

## 2. Files of record

### New
- `src/fastapi/app/ocr/_docling_common.py`
- `src/fastapi/tests/test_ocr_mixed_path.py` (9 tests)
- `scripts/phase3_master_plan_step5_verify.sh`

### Modified (skeleton → implementation)
- `src/fastapi/app/ocr/parse_mixed.py`
- `src/fastapi/app/ocr/parse_table_heavy.py`
- `src/fastapi/tests/test_ocr_module_imports.py` — removed both from
  `SKELETON_MODULES` (only `quality_graph` remains)

---

## 3. Verifier status

```
[check1] PASS — 9/9 mixed + table-heavy path tests green
[check2] PASS — parse_mixed + parse_table_heavy removed from SKELETON_MODULES
[check3] PASS — Step 1 verifier still green
[check4] PASS — Step 2 verifier still green
[check5] PASS — Step 3 verifier still green
[check6] PASS — Step 4 verifier still green

=== Phase 3 master-plan Step 5 verifier summary ===
  6/6 checks passed
```

Pytest scoreboard (all OCR tests):
- 9 mixed tests pass (test_ocr_mixed_path.py)
- 7 scanned tests pass (test_ocr_scanned_path.py)
- 8 native tests pass (test_ocr_native_path.py)
- 17 module-import + skeleton tests pass (test_ocr_module_imports.py),
  7 of 8 modules now graduated; 1 remains skeleton

---

## 4. Decisions made in this phase

### 4.1 Docling runs with do_ocr=False; OCR dispatched externally

See § 1 above. Separation-of-concerns: Docling does layout +
TableFormer; parse_scanned does OCR. Mixed parser returns
`pages_needing_ocr` list so the Hatchet orchestrator can dispatch.

### 4.2 Docling label → silver enum mapping in one place

`_docling_common.normalize_label` maps Docling's layout labels
(text, title, section_header, picture, table, etc.) to the
silver.ingest_layouts.layout_label CHECK-constrained enum.
Unknown labels collapse to "other" rather than crashing — the
silver schema allows "other" explicitly so this is safe.

Two known Docling labels with aliases:
- `picture` → `figure` (silver schema uses "figure")
- Case-insensitive matching (Docling occasionally emits Title-case)

### 4.3 parse_table_heavy: pdfplumber first, Docling second pass

Tables in native NI 43-101 PDFs (resource estimate sections, drillhole
assay summaries) extract reliably with pdfplumber's grid heuristic.
Docling's TableFormer is slower and slightly less precise on
deterministic native tables. Running pdfplumber first is the fast
path; Docling fallback catches the cases pdfplumber misses
(multi-page tables, irregular merges, image-rendered tables).

Each pdfplumber table records `parser_used = "pdfplumber"`; Docling
fallback tables record `parser_used = "docling_tableformer"`. The
silver.table_extraction_quality CHECK constraint accepts both.

### 4.4 Bounding box coord_origin preserved from Docling (BOTTOMLEFT)

pdfminer.six (used by parse_native) and Docling both emit BOTTOMLEFT
PDF-coordinate bboxes. parse_scanned's OCR output uses image
coordinates (TOPLEFT, rendered scale) — a translation will be needed
in the Hatchet persistence layer (Step 7) when writing scanned OCR
rows to silver.ingest_ocr_results next to native rows in
silver.ingest_extractions. Document this in Step 7's design.

### 4.5 `pages_needing_ocr` is the only cross-parser signal

parse_mixed records which pages came back without text from Docling.
That list is the contract with the Step 7 orchestrator: "call
parse_scanned on these page numbers, then merge the OCR output back
into the silver rows." No other cross-parser state passes — each
parser is otherwise independent.

---

## 5. Findings carried over to doc-phase 54+

### 5.1 Docling TableFormer image-extraction deprecation warning

Docling emits a DeprecationWarning about `generate_table_images`. Not
affecting current use (we use the structured cells, not the
images), but watch for breakage on a future Docling bump. The
warning is benign for now.

### 5.2 Table review thresholds need tuning in Step 9

Two module constants in parse_table_heavy:
```
TABLE_REVIEW_STRUCTURE_THRESHOLD = 0.70
TABLE_REVIEW_CELL_THRESHOLD = 0.85
```
Both currently set conservatively. The 50-PDF acceptance corpus
(Step 9) will reveal what threshold actually catches real low-
confidence tables without over-flagging.

### 5.3 Docling's bbox coord_origin convention varies by stage

Step 5 implementation uses Docling's `prov[0].bbox` which reports
PDF-coordinate BOTTOMLEFT for native pages. If a future Docling
version returns TOPLEFT (image coord) for layout regions, the
bbox extraction in `_bbox_from_prov` will be wrong. Step 9's
acceptance corpus across many PDFs will catch any cases where
this happens — when it does, `_bbox_from_prov` needs to check
`prov[0].bbox.coord_origin` and translate.

### 5.4 Docling cold-load is real (~5-7 sec)

PaddleOCR cold-load: ~3 sec. Docling cold-load: ~5-7 sec
(layout model + TableFormer model). Both are paid once per
worker process and amortize over the worker's lifetime.

---

## 6. Pre-existing carry-overs (unchanged this phase)

From prior handoffs:
- Profile classifier thresholds need 50-PDF corpus tuning (Step 9)
- Migration apply path workaround (use psql + manual INSERT migrations)
- Windows ↔ WSL dual-tree sync
- WSL2 exposes 6/32 CPUs
- PaddleOCR cache → /tmp by default
- No Hough-transform deskew yet

From doc-phase 48:
- `phase4_step7` sweep-only flake
- `phase9_step1` docker network name mismatch
- Phase-4 dead-code findings

---

## 7. What doc-phase 54 will do

**Master-plan §3 Step 6 — LangGraph OCR Quality Graph.**

The last skeleton graduates. The quality graph is the routing logic
that decides per-page what to do based on parse results:
- accept (high confidence → no further action)
- re_ocr (max 2 retries with escalating settings)
- silver_review (write low_confidence_page_reviews row)
- reject (writes document_ingestion_quality.recommended_action = "reject")

Deliverables:
- `app.ocr.quality_graph.route_page` implementation as a LangGraph
  state machine (or a pure-function equivalent — LangGraph adds
  complexity for what might be a simple decision tree; will evaluate
  during implementation)
- Reason codes match the silver.low_confidence_page_reviews.reason
  CHECK enum from doc-phase 50
- Behaviour tests: per-route conditions with synthetic parse results
  (don't need a real PDF for this — the graph operates on parse
  output dicts)
- New verifier: scripts/phase3_master_plan_step6_verify.sh

Test wall-time should be sub-second (no PDF I/O, no model inference).

---

## 8. Master-plan §3 progress

| Step | Status | Doc-phase tick |
|---|---|---|
| 1. `app/ocr/` scaffolding + smoke-bench | ✅ DONE | 49 |
| 2. §9.3 + §9.6 silver migrations | ✅ DONE | 50 |
| 3. PDF profiler + native parser | ✅ DONE | 51 |
| 4. Scanned parser + render | ✅ DONE | 52 |
| 5. Mixed + table-heavy parsers (Docling) | ✅ DONE | 53 |
| 6. LangGraph OCR Quality Graph | next | 54 |
| 7. Hatchet `ingest_pdf` cutover + shadow + persistence | pending | 55 |
| 8. Silver Review UI extension | pending | 56 |
| 9. 50-PDF acceptance corpus + sign-off | pending — needs Kyle labeling | 57 |
| 10. RAGFlow retirement + cleanup | pending | 58 |

**5 of 10 steps complete.** Last skeleton (quality_graph) graduates
next; Step 7 (Hatchet cutover + persistence) is the integration
phase that wires everything to the silver tables.

---

End of doc-phase 53 handoff. All four parser paths implemented.
Quality routing next.
