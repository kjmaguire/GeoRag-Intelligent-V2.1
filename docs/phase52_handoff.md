# Phase 52 Handoff — Master-plan §3 Step 4 (scanned parser + render)

**Document version:** 1.0
**Status:** Doc-phase 52 complete. Doc-phase 53 inheriting.
**Predecessors:** `docs/phase51_handoff.md`, `docs/phase3_master_plan_kickoff.md`,
`docs/adr/0002-04p-stack-replaces-ragflow.md`.

Two more `app.ocr.*` modules graduate from skeleton to implementation:
`render` and `parse_scanned`. The PaddleOCR pipeline now works
end-to-end on synthetic scanned PDFs.

---

## 1. What doc-phase 52 delivered

| Module | What it does | Latency baseline |
|---|---|---|
| `app.ocr.render` | pypdfium2 single-page → PNG bytes (configurable scale) | ~50–80 ms/page at scale=2.0 |
| `app.ocr.parse_scanned` | PaddleOCR PP-OCRv5 CPU on pre-rendered numpy arrays (no fitz) | ~6 sec/page warm, ~8.5 sec/page cold (first call per worker) |

Both:
- Pure async functions; no DB writes
- `asyncio.to_thread` wrapping sync impl
- Locked return schemas via docstring

### Key parse_scanned design

Settings overrideable via the optional `settings` kwarg:
```python
DEFAULT_SCANNED_SETTINGS = {
    "use_angle_cls": True,   # auto-rotation
    "lang": "en",
    "render_scale": 2.0,      # ~144 DPI
}
```

Step 6's `quality_graph` will override these for retry passes with
escalating engine settings (binarization threshold, language hint).
The function itself does ONE OCR pass per call — retry orchestration
lives in the quality graph.

### PaddleOCR cache location workaround

PaddleOCR's default model cache is `$HOME/.paddleocr` which is
`/var/www/.paddleocr` for the FastAPI container's `www-data` user
(not writable). Bench worked because it ran the hatchet worker as
`--user 0`. Fixed in `parse_scanned.py`:

- `_PADDLEOCR_HOME` reads from `PADDLEOCR_HOME` env or defaults to
  `/tmp/.paddleocr` (always writable)
- Explicit `det_model_dir`, `rec_model_dir`, `cls_model_dir` passed
  to `PaddleOCR()` so it never falls back to the home-dir path

Operators wanting a persistent cache (not /tmp) can set
`PADDLEOCR_HOME=/some/persistent/path` at container startup.

---

## 2. Files of record

### New
- `src/fastapi/tests/test_ocr_scanned_path.py` (7 behaviour tests)
- `scripts/phase3_master_plan_step4_verify.sh`

### Modified (skeleton → implementation)
- `src/fastapi/app/ocr/render.py`
- `src/fastapi/app/ocr/parse_scanned.py`
- `src/fastapi/tests/test_ocr_module_imports.py` — removed `render`
  and `parse_scanned` from `SKELETON_MODULES`

---

## 3. Verifier status

```
[check1] PASS — 7/7 scanned path behaviour tests green
[check2] PASS — render + parse_scanned removed from SKELETON_MODULES
[check3] PASS — Step 1 verifier still 3/3 green
[check4] PASS — Step 2 verifier still 6/6 green
[check5] PASS — Step 3 verifier still 4/4 green

=== Phase 3 master-plan Step 4 verifier summary ===
  5/5 checks passed
```

Pytest scoreboard for all OCR tests:
- 7 scanned tests pass (test_ocr_scanned_path.py)
- 8 native tests pass (test_ocr_native_path.py)
- 17 module-import + skeleton tests pass (test_ocr_module_imports.py),
  5 graduated modules now skip the skeleton NotImplementedError check

---

## 4. Decisions made in this phase

### 4.1 PaddleOCR cache → /tmp by default, overridable via env

The simplest writable-everywhere default with an env-var escape hatch
for production deploys that want a persistent cache. Step 7 (Hatchet
cutover) and Step 8 (Silver Review UI) should probably set
`PADDLEOCR_HOME=/data/paddleocr` to avoid re-downloading models on
container restart.

### 4.2 PaddleOCR result-shape normalizer

The `_flatten_paddleocr_result` helper handles two known result
shapes (versioned across PaddleOCR releases). Defensive: if PaddleOCR
changes its return shape in a future minor, the helper returns `[]`
instead of crashing — caller sees a low-confidence empty page, which
the quality_graph (Step 6) routes to Silver Review. Better than a
hard exception.

### 4.3 Synthetic scanned fixture (not committed)

The scanned path's test fixture is built in-test by rasterizing the
first 2 pages of the committed PLS-2024 native PDF via pypdfium2 —
same pattern as the smoke-bench's `make_synthetic_scanned_pdf`. No
additional binary fixture committed. The module-scope pytest fixture
caches one build for all 3 scanned tests so we only pay one PaddleOCR
cold-load per test session.

### 4.4 Single-pass parse, retry lives in quality_graph

`parse_scanned` does exactly one OCR pass per call. The kickoff Step 4
language "max 2 re-OCR retries with different engine settings" is
fulfilled by Step 6's `quality_graph` calling `parse_scanned` repeatedly
with escalating `settings`. Keeps the parser stateless + testable.

### 4.5 PaddleOCR errors caught per-page, not per-document

If PaddleOCR raises on a single page (degenerate input, decode error),
the parser records that page as `ocr_confidence=0.0, text_lines=0` and
continues to the next. Caller sees the low confidence and routes that
page through the retry path. Killing the whole document on one bad
page would be brittle for real-world scanned NI 43-101s with
inconsistent page quality.

---

## 5. Findings carried over to doc-phase 53+

### 5.1 PaddleOCR cold-load ~3 sec per worker process

Within a single worker process, subsequent OCR calls reuse the
in-memory model. Across worker restarts, the cold-load is paid again.
For a long-running Hatchet ingestion worker pool this is fine — the
cold cost is amortized over thousands of pages. Worth knowing for
deployment topology decisions in Step 7.

### 5.2 Render scale 2.0 is the default; tune in Step 9

PaddleOCR docs recommend 300+ DPI for best recognition; scale=2.0 is
~144 DPI. The smoke-bench at this scale produced reasonable OCR
text on the synthetic PLS-2024 fixture. The 50-PDF acceptance corpus
will reveal whether scale=4.0 (~288 DPI) gives meaningfully better
recognition at the cost of ~4× memory + ~3-5× wall-time per page.
Tunable via the `render_scale` setting.

### 5.3 No deskew preprocessing yet (use_angle_cls handles 90° rotations)

PaddleOCR's `use_angle_cls=True` detects + corrects 0/90/180/270°
rotations. It does NOT handle small skew angles (e.g. 5° off-axis
from a poorly-scanned page). Step 5 or Step 6 may add a Hough-transform
deskew preprocessor; the kickoff Step 4 language mentioned this but
the smoke-bench passed without it, so it's deferred to a tick that
actually surfaces the need.

---

## 6. Pre-existing carry-overs (unchanged this phase)

From prior handoffs (49, 50, 51):
- Docling is the slowest path (~12 sec/page); Step 5 implements it
- Profile classifier thresholds need tuning against 50-PDF corpus (Step 9)
- Migration apply path workaround
- Windows ↔ WSL dual-tree sync
- WSL2 exposes 6/32 CPUs
- Map-heavy detection deferred to Step 5

From doc-phase 48:
- `phase4_step7` sweep-only flake
- `phase9_step1` docker network name mismatch

---

## 7. What doc-phase 53 will do

**Master-plan §3 Step 5 — Mixed + table-heavy parsers (Docling).**

Two skeleton modules graduate:
- `app.ocr.parse_mixed` — Docling layout-first per-region dispatch
- `app.ocr.parse_table_heavy` — pdfplumber + Docling table-region focus

Deliverables:
- Docling pipeline for layout + table extraction (~12 sec/page baseline)
- Dispatch logic: text regions → pdfminer.six, table regions →
  pdfplumber/Docling tableformer, image regions → parse_scanned
- Table-heavy: multi-page table row-continuation detection
- Behaviour tests using a synthetic mixed PDF (alternating native +
  rasterized pages, same pattern as the smoke-bench)
- New verifier: `scripts/phase3_master_plan_step5_verify.sh`

Step 5 tests will be ~30-60 sec wall (Docling cold-load is ~7 sec;
mixed parse ~12 sec/page × 4 pages ≈ 50 sec).

---

## 8. Master-plan §3 progress

| Step | Status | Doc-phase tick |
|---|---|---|
| 1. `app/ocr/` scaffolding + smoke-bench | ✅ DONE (3/3) | 49 |
| 2. §9.3 + §9.6 silver migrations | ✅ DONE (6/6) | 50 |
| 3. PDF profiler + native parser | ✅ DONE (4/4) | 51 |
| 4. Scanned parser + render | ✅ DONE (5/5) | 52 |
| 5. Mixed + table-heavy parsers (Docling) | next | 53 |
| 6. LangGraph OCR Quality Graph | pending | 54 |
| 7. Hatchet `ingest_pdf` cutover + shadow + persistence | pending | 55 |
| 8. Silver Review UI extension | pending | 56 |
| 9. 50-PDF acceptance corpus + sign-off | pending — needs Kyle labeling | 57 |
| 10. RAGFlow retirement + cleanup | pending | 58 |

**4 of 10 steps complete.** Three skeletons remain (parse_mixed,
parse_table_heavy, quality_graph).

---

End of doc-phase 52 handoff. OCR works. Doc-phase 53 adds Docling.
