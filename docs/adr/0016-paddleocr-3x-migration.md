# ADR 0016: PaddleOCR 2.10 → 3.7 + PaddleOCR-VL Phase 2 plan

- **Date**: 2026-06-23
- **Status**: Accepted (Phase 1) · Proposed (Phase 2)
- **Deciders**: Kyle Maguire (SME)
- **Supersedes**: §04p Stage 5 OCR engine wiring (PP-OCRv5 via paddleocr 2.10)

## Context

§04p Stage 5 (region-targeted OCR on rendered PDF crops) and the
scanned-PDF parser (`app/ocr/parse_scanned.py`) both use PaddleOCR. The 2026-06 stack audit flagged:

> **PaddleOCR 2.10 → 3.4 + PaddleOCR-VL-1.6 (May 28 2026) — 96.3% OmniDocBench v1.6 accuracy**, SOTA on tables/formulas/Chinese rare chars — directly improves §04p ingest.

Verified state pre-migration:

- `pyproject.toml` declared `paddleocr>=2.10` (no upper cap)
- Resolved version in the runtime image was **2.10.0** — uv had not picked anything newer because the call-site API (`use_angle_cls`, `use_gpu`, `show_log`, `ocr.ocr()`) is 2.x-flavoured and the 3.x cut broke those names
- Two production call sites: `services/pdf_ocr.py` (Stage 5 worker) + `ocr/parse_scanned.py` (scanned-page parser)
- One smoke test: `ops/validation/ocr_cpu_smoke.py`

The audit recommendation has two distinct pieces — a **library upgrade** (2.x → 3.x with PP-OCRv5/v6 as default) and a **new model** (PaddleOCR-VL-1.6, an end-to-end VLM-style document parser). They have different cost/benefit profiles and warrant different rollout paths.

## Decision

**Phase 1 (this ADR, Accepted):** migrate the existing `PaddleOCR(...)` + `.ocr()` call sites to the 3.x API (`use_textline_orientation`, `device=...`, `.predict()`, attribute-based result access). Pin `paddleocr>=3.7,<4.0` in pyproject. This is an in-place engine upgrade — same shape input/output, better model defaults (PP-OCRv5 is now the implicit baseline), forward-compatible with 3.x.

**Phase 2 (this ADR, Proposed):** introduce **PaddleOCR-VL-1.6** as a *parallel* §04p Stage-6-style capability for full-page parsing of scanned NI 43-101s. Flag-gated, additive — does **not** replace the PP-OCRv5 regional-crop worker, which remains the right tool for per-bbox OCR.

## What changes (Phase 1)

| Surface | 2.x | 3.x |
|---|---|---|
| Constructor kwarg | `use_angle_cls=True` | `use_textline_orientation=True` |
| Constructor kwarg | `use_gpu=True/False` | `device="gpu:0"` / `"cpu"` |
| Constructor kwarg | `show_log=False` | removed; use `logging.getLogger("paddleocr").setLevel(...)` |
| Constructor kwarg | `det_model_dir=...` | `text_detection_model_dir=...` |
| Constructor kwarg | `rec_model_dir=...` | `text_recognition_model_dir=...` |
| Constructor kwarg | `cls_model_dir=...` | `textline_orientation_model_dir=...` |
| Inference call | `ocr.ocr(arr, cls=True)` | `ocr.predict(arr)` (cls toggle is now constructor-only) |
| Return shape | `[[ [bbox, (text, conf)], … ]]` (nested list of tuples) | `[OCRResult]` with `.rec_texts: list[str]`, `.rec_scores: list[float]`, `.rec_boxes: np.ndarray (Nx4 axis-aligned)`, `.rec_polys: list[np.ndarray]` |
| Default OCR model | PP-OCRv4 / earlier | **PP-OCRv5** (free upgrade) |

### Files touched (Phase 1)

- `src/fastapi/pyproject.toml` — pin bumped `>=2.10` → `>=3.7,<4.0`
- `src/fastapi/app/services/pdf_ocr.py` — `_get_ocr_instance()` constructor + `_ocr_worker()` consumer
- `src/fastapi/app/ocr/parse_scanned.py` — main constructor + consumer + `_flatten_paddleocr_result()` deprecation note
- `ops/validation/ocr_cpu_smoke.py` — bench updated

### What stays the same (Phase 1)

- The GPU detection helper (`app/ocr/_paddleocr_gpu.py::paddleocr_use_gpu`) — output is now translated to `device="gpu:0" | "cpu"` at call time instead of `use_gpu=True | False`, but the helper's logic is unchanged.
- The `silver.pdf_ocr_results` schema and the `source_method="paddleocr_pp_ocrv5"` provenance tag — model name didn't change, just the engine wrapping it.
- The `settings_used["use_angle_cls"]` key in `parse_scanned.py` persisted results — kept stable for downstream consumers.
- The OCR worker process-pool pattern, the worker-singleton, the lazy-import strategy — all unchanged.

### Risks (Phase 1) + mitigations

| Risk | Mitigation |
|---|---|
| **Confidence-score distribution drift.** PP-OCRv5 (3.x default) may produce subtly different confidence values vs the v4 model 2.10 was serving. Downstream thresholds (e.g. retrieval-quality gates that filter on OCR confidence) could shift. | Run `ops/validation/ocr_cpu_smoke.py` against a golden NI 43-101 crop; compare per-page confidence distributions before promote. If thresholds need tuning, the affected configs are `services/eval/promotion_gate.py` + retrieval profile bm25_weights — not load-bearing schema changes. |
| **`.predict()` returns different number of lines than `.ocr()` on edge cases** (e.g. very small text, rotated regions). | Same smoke-test gate. The legacy `_flatten_paddleocr_result` helper is retained (with a 3.x guard) for the smoke test's cross-version diff. |
| **GPU/CPU routing inversion.** `device="gpu:0"` requires that exact device exist — failing differently than `use_gpu=True` did on a CPU-only host. | The `paddleocr_use_gpu()` helper already checks CUDA availability + VRAM threshold before returning True; the call-site translation lands `device="cpu"` whenever the helper says no, preserving the prior fall-through behaviour. |

## What changes (Phase 2 — Proposed)

Add **PaddleOCR-VL-1.6** as a parallel parser path for full-page scanned-PDF parsing, sitting alongside (not replacing) the existing PP-OCRv5 regional worker.

### Why parallel, not replacement

| Capability | PP-OCRv5 (3.x default) | PaddleOCR-VL-1.6 |
|---|---|---|
| Regional crop OCR (Stage 5 use case) | ✅ purpose-built; fast | ⚠️ overkill; runs full-page layout analysis first |
| Full-page scan parsing (Stage 6-style) | OK — needs separate layout step | ✅ end-to-end VLM, 96.3% OmniDocBench v1.6, tables + formulas + multi-column |
| Per-page latency on text-only | <0.5s | 2–5s (BF16 1B inference) |
| VRAM footprint | ~500 MB for the model | ~3–4 GB BF16 (≤2 GB on GGUF quants) |
| Output | `rec_texts/rec_scores/rec_boxes` per region | Layout-aware Markdown for the whole page |

The two are complementary — PP-OCRv5 for per-crop precision work, PaddleOCR-VL for whole-page document understanding (the slot that docling currently occupies but with a fundamentally better quality ceiling).

### Phase 2 rollout plan (Proposed)

1. Add the `[doc-parser]` extra: `paddleocr[doc-parser]>=3.7,<4.0`. Brings in `PaddleOCRVL` + a special safetensors build. Weights download from HF on first use.
2. Introduce a feature flag `PDF_DOCPARSER_BACKEND` with values `docling` (current default) | `paddleocr-vl`. Default stays `docling`.
3. Wire a new parser class `PaddleOCRVLParser` in `app/ocr/` that mirrors the docling parser interface — same input (PDF path or bytes), same output schema (Markdown + figure regions + table regions).
4. **Shadow run on a golden 20-PDF corpus.** Run both parsers on the same input; compare:
   - Markdown structural fidelity (heading hierarchy, table row count).
   - Figure detection recall.
   - Per-page latency.
   - VRAM peak.
5. **Promote per-document-class.** Possible cutover heuristic: `paddleocr-vl` for `scanned=true ∨ tables_detected>N`, `docling` otherwise. Or full cutover if the eval shows VL strictly dominates.

### Phase 2 risks

| Risk | Mitigation |
|---|---|
| **VRAM contention with vLLM.** PaddleOCR-VL adds ~3–4 GB on the same A4500 already running Qwen3-14B-AWQ + the embedding/reranker. May force `VLLM_GPU_MEM_UTIL` ≤ 0.70. | Profile during shadow run. If contention is real, run PaddleOCR-VL on the ingestion worker's GPU exclusive of vLLM, or move to a dedicated VL serving box at production scale. |
| **Latency regression on text-only pages.** PP-OCRv5 is 10–50× faster on simple pages. | Per-document-class routing in the cutover plan above — don't pay the VL cost on pages that don't need it. |
| **HuggingFace dependency.** First-run weight download hits HF; air-gapped deploys need pre-staged weights. | Document the `huggingface-cli download PaddlePaddle/PaddleOCR-VL-1.6` pre-stage in the airgap runbook before Phase 2 promotes. |

## Consequences

### Positive

- **PP-OCRv5 model upgrade is free** with the library bump (Phase 1) — measurable accuracy gain on the scanned-page tail.
- The 3.x API is more honest about return shape (typed attributes vs nested tuples). Future maintenance is easier.
- Phase 2 path is open without committing to it — flag-gated rollout means we can A/B without disrupting the production parser stack.
- `paddleocr<4.0` cap protects against a future 4.x cut sneaking in unannounced.

### Negative

- Phase 1 touched four files including the production worker. Confidence distributions may shift slightly — golden-set validation required before declaring the migration done.
- The `_flatten_paddleocr_result` helper is now legacy-only (smoke test + defensive). It's documented as such but adds module surface to maintain.
- Phase 2 adds another OCR model to the inventory — operational footprint grows.

### Neutral

- License unchanged (Apache 2.0 for paddleocr; permissive for PaddleOCR-VL weights).
- Persistence schema unchanged.
- Worker process-pool pattern unchanged.

## References

- `src/fastapi/app/services/pdf_ocr.py` — Stage 5 worker, post-migration.
- `src/fastapi/app/ocr/parse_scanned.py` — scanned-PDF parser, post-migration.
- `ops/validation/ocr_cpu_smoke.py` — smoke bench, post-migration.
- ADR-0002 — §04p PDF stack replaces RAGFlow (defines the broader PDF-ingest architecture this OCR sits inside).
- ADR-0015 — Qwen3-VL-8B migration (the LLM-VL parallel to PaddleOCR-VL on the figure-summary side).
- [PaddleOCR 3.x Upgrade Notes](http://www.paddleocr.ai/main/en/update/upgrade_notes.html)
- [PaddleOCR-VL Usage Tutorial](https://www.paddleocr.ai/latest/en/version3.x/pipeline_usage/PaddleOCR-VL.html)
- [HF: PaddlePaddle/PaddleOCR-VL-1.6](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6)
- 2026-06 audit punch-list item 10 (the one this ADR resolves).
