# ADR 0015: Migrate VL model from Qwen2.5-VL-7B to Qwen3-VL-8B

- **Date**: 2026-06-23
- **Status**: Proposed (deploy gated on shadow-eval pass)
- **Deciders**: Kyle Maguire (SME)
- **Supersedes**: Stage-6 model selection in §04p (`georag-architecture.html`) which named Qwen2.5-VL-7B.

## Context

§04p Stage-6 (figure / caption VL reasoning) currently uses `Qwen/Qwen2.5-VL-7B-Instruct` (the default in `src/fastapi/app/services/pdf_vl.py`, env-overridable via `PDF_VL_MODEL_ID`). It's the engine behind:

- `/pdf/summarize_section` — turns rendered figure regions into structured `PdfVlSummary` JSON.
- The figure→caption linking pass (`figure_extractor.py`) — feeds `silver.report_figures`.
- The chart-grounded retrieval surface — cited answers that reference specific figures in NI 43-101 reports.

The Qwen ecosystem has already moved on around it: the embedding stack swapped to `Qwen/Qwen3-Embedding-0.6B` and the reranker to `Qwen/Qwen3-Reranker-0.6B` (per `project_qwen_ecosystem_swap_2026_06_03`). The VL slot is the last 2.x holdout.

Qwen3-VL (4B / 8B / 32B dense + 30B-A3B MoE) was released by Alibaba's Qwen team in late 2025 / early 2026 with the same Apache-2.0 license and the same HuggingFace transformers integration surface as 2.5-VL.

## Options considered

| Option | Pros | Cons | Outcome |
|---|---|---|---|
| **A. Migrate to Qwen3-VL-8B** | 256K native context (vs ~32K) — multi-page figure bundles in one pass. Better OCR (stylised fonts, scanned legacy reports). Better table extraction. Materially better chart-grounded reasoning. Consistent with embedding + reranker already on Qwen3 line. | Different model — structured-output prior may drift. Slightly larger (8B vs 7B). Needs eval against the figure-grounded golden set before promote. | **Chosen** (subject to shadow gate). |
| B. Migrate to Qwen3-VL-4B | Same family wins as 8B at lower VRAM. | Lower benchmark scores per the Qwen3-VL tech report. The whole motivation is quality on dense geological figures. | Rejected. |
| C. Migrate to Qwen3-VL-32B / 30B-A3B MoE | Best benchmark numbers in the family. | 32B dense doesn't fit alongside vLLM's main Qwen3-14B-AWQ + the embedding/reranker tenancy on the dev A4500 (20 GB). 30B-A3B has ~22 GB AWQ footprint — same problem. | Defer until production GPU sizing decision (`HANDOVER_INDEX.md` §5.7). |
| D. Stay on Qwen2.5-VL-7B | Zero migration effort. | Falls progressively behind the Qwen3 family the rest of the stack is on. Known weakness on dense multi-panel geological figures (cross-sections, drill plan overlays). | Rejected — accruing tech debt. |

## Decision

Adopt **`Qwen/Qwen3-VL-8B-Instruct`** as the §04p Stage-6 model, gated on a shadow-mode evaluation pass against the figure-grounded subset of `eval.golden_questions`.

> **⚠ Correction 2026-06-24 — the original `Qwen/Qwen3-VL-8B-Instruct-AWQ` does not exist.**
> Qwen never published an official AWQ of Qwen3-VL-8B (verified on HF: 404). The
> canonical model is the BF16 `Qwen/Qwen3-VL-8B-Instruct` (~8.8 B params → ~17.5 GB),
> which does **not** fit the dev A4500 (20 GB) alongside the main Qwen3-14B-AWQ vLLM
> + the embedding/reranker tenancy. Serving options for the shadow run, in order of
> preference:
> 1. **Own GPU / production sizing** — serve the BF16 model on dedicated VRAM (clean).
> 2. **Community W4A16/AWQ quant (~5-6 GB)** — e.g. `cyankiwi/Qwen3-VL-8B-Instruct-AWQ-4bit`
>    or `MLliu6/Qwen3-VL-8B-Instruct-AWQ-W4A16`, served as a 2nd vLLM with the main
>    vLLM's `gpu_memory_utilization` dropped 0.92 → ~0.62 (cuts the live LLM's KV
>    cache / concurrency). Unofficial — **vet quant quality before promoting**.
> 3. **Smaller `Qwen3-VL-4B-Instruct` (or its community AWQ, ~3 GB)** — fits with
>    minimal LLM impact; weaker than 8B.
> `pdf_vl._DEFAULT_MODEL_ID_V3` now points at the real BF16 id; set
> `PDF_VL_MODEL_ID_V3` to a quant for constrained VRAM. The shadow gate + dual-write
> machinery (`pdf_vl_shadow.py`, `pdf_vl.shadow_observe_section`) are already built —
> only a servable endpoint is missing.

## Rollout plan (gated, reversible)

1. **Add the feature flag.** Introduce `PDF_VL_MODEL_VERSION` env (default `2` = current 2.5-VL behaviour). When set to `3`, `pdf_vl.py` reads from `PDF_VL_MODEL_ID_V3` (default `Qwen/Qwen3-VL-8B-Instruct-AWQ`); otherwise reads from the existing `PDF_VL_MODEL_ID`.
2. **Stand up serving.** Either:
   - Add a second vLLM instance dedicated to the VL model (cleanest — VL inference doesn't compete with the main LLM's KV cache); or
   - Override `PDF_VL_BACKEND_URL` on the AI worker to point at a Qwen3-VL serving endpoint provisioned out of band.
3. **Shadow run for a week.** Dual-write 2.5-VL + 3-VL outputs on the same input pages; compare via `Admin/ShadowRuns`. Track:
   - Schema-valid output rate (must be ≥ 95% — same bar as the existing typed-output validators).
   - Figure→caption link rate vs the 2.5-VL baseline.
   - Per-page latency p95.

   **Gate machinery landed (2026-06-23):** `services/eval/pdf_vl_shadow.py`
   computes all three metrics and the promote/block decision from a list of
   per-section `VlShadowObservation` rows (`assess_vl_shadow(...)`). Thresholds:
   schema-valid ≥ 0.95, figure-link-rate regression ≤ 2.0 pp below the V2
   baseline, ≥ 20 observations (latency p95 is reported, not gated). The
   **dual-write runtime** also landed: `PdfVlService.shadow_observe_section`
   renders a section once, scores it with both model versions
   (`_call_vl_backend` is now model-parametrized), and returns a
   `VlShadowObservation` for the gate — read-only, never persisted, and it
   records an errored/invalid version as schema-invalid rather than raising.
   Still pending: V3 serving (step 2) and wiring `shadow_observe_section` into
   the shadow-trigger path to run it across the golden corpus.
4. **Promote.** Once the three metrics meet the gate (`assess_vl_shadow().allow`), flip the default in `pdf_vl.py` from version `2` to `3` and document the cutover date in the project memory.
5. **Rollback.** Set `PDF_VL_MODEL_VERSION=2` in the affected workspace's env to revert without a deploy.

## Consequences

### Positive

- Materially better extraction quality on the figure-heavy NI 43-101 corpus we ingest.
- 256K context unlocks multi-page figure-with-caption bundles in one pass — directly improves the figure→caption linking work from `project_pdf_coverage_overhaul_2026_05_22`.
- VL stack catches up with the rest of the Qwen3 ecosystem (embedding + reranker already swapped).

### Negative

- Operational footprint grows by one vLLM instance (or one model-swap on shared vLLM). Worth-it for the use case but not free.
- Pydantic output shapes (`PdfVlSummary`, etc.) need verification — `guided_json` schema enforcement (already wired into `pdf_vl` per `project_vllm_polish_2026_06_03`) should absorb most drift but spot-check is required.
- Operator must update env scaffolding before promote.

### Neutral

- License unchanged (Apache 2.0).
- HuggingFace transformers + vLLM serving integration unchanged.
- No client-code changes in `pdf_vl.py` beyond reading the new env vars.

## Not in scope

- 32B / 30B-A3B variants (see option C — defer until GPU sizing).
- Promoting Qwen3-VL to a tier in `MODEL_ROUTING_ENABLED` for chat-side use (this ADR is strictly the §04p Stage-6 swap).

## References

- `src/fastapi/app/services/pdf_vl.py` — current model wiring.
- ADR-0008 — embedding model evaluation (the procedural template).
- ADR-0010 — canonical chunked-corpus contract (downstream of figure extraction).
- Memory: `project_qwen_ecosystem_swap_2026_06_03`, `project_pdf_coverage_overhaul_2026_05_22`, `project_vllm_polish_2026_06_03`.
- 2026-06 audit punch-list item 11 (the one this ADR resolves).
