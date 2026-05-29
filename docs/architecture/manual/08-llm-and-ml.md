# Chapter 08 — LLM and ML Models

Every model the system runs, classified by **kind** and **where it executes**.

## 1. vLLM — the LLM tier

[docker-compose.yml:1476](../../../docker-compose.yml).

| Field | Value |
|---|---|
| Image | `vllm/vllm-openai:v0.21.0` |
| Default model | `Qwen/Qwen3-14B-AWQ` (INT4 quantised, ~17 GB on disk) |
| Quantisation | `awq_marlin` |
| Max model len | 16384 |
| KV cache dtype | FP8 (storage; FP16 compute on Ampere) |
| Max num seqs | 12 |
| GPU mem util | 0.93 (capped at 0.80 when sharing with hatchet-worker-ai per [project_gpu_acceleration_2026_05_22](../notes/INDEX.md#project_gpu_acceleration_2026_05_22)) |
| Tensor parallel | 1 (single A4500) |
| Speculative decoding | n-gram, `num_speculative_tokens=2` |
| Prefix caching | enabled |
| Chunked prefill | enabled |
| Cuda-graph capture sizes | `[1,2,4,8,12]` (trimmed from default to free ~1-3 GiB) |
| OpenAI-compatible endpoint | `http://vllm:8000/v1` |

Why this exact configuration is in
[docker-compose.yml:1518-1577](../../../docker-compose.yml) with multi-paragraph
inline comments justifying every flag.

### How FastAPI calls it

`LLM_BACKEND=vllm` (default). The OpenAI-compatible client points at
`LLM_PRIMARY_URL=http://vllm:8000/v1` with `LLM_PRIMARY_MODEL=Qwen/Qwen3-14B-AWQ`.
Cross-backend failover (`LLM_BACKEND_FALLBACK=downshift`) re-routes to
the configured fallback when the primary 429s.

LLM client implementation:
[src/fastapi/app/agent/llm_calls.py](../../../src/fastapi/app/agent/llm_calls.py).
Streams tokens via SSE; FastAPI forwards chunks to Laravel which broadcasts
on the Reverb `query.streaming.{run_id}` channel.

### Warmup

The `vllm-warmup` sidecar ([docker-compose.yml:1629](../../../docker-compose.yml))
fires 5 throwaway 16-token completions once vLLM is healthy — burns the
FlashInfer JIT compilation tax (~28 → 154 tok/s, A4500 + Qwen3-14B-AWQ).

### Anthropic fallback

`LLM_BACKEND=anthropic` + `LLM_BACKEND_FALLBACK=vllm` flips the order:
Anthropic primary, vLLM as backup on 429.
Env vars [docker-compose.yml:975-985](../../../docker-compose.yml):
- `ANTHROPIC_API_KEY`
- `ANTHROPIC_MODEL=claude-opus-4-7`
- `ANTHROPIC_MAX_OUTPUT_TOKENS=4096`
- `ANTHROPIC_ENABLE_PROMPT_CACHING=true`
- `ANTHROPIC_USE_PRIORITY_TIER=false`
- `MODEL_TIER_FAST=claude-haiku-4-5`
- `MODEL_TIER_STANDARD=claude-sonnet-4-5`
- `MODEL_TIER_DEEP=claude-opus-4-7`

### Qwen2.5-VL (figures)

`VLLM_MODEL=Qwen/Qwen2.5-VL-7B-Instruct` is supported as a §04p Stage-6
config. The VL pass over figure pages is gated on
`DOCLING_VL_ENABLED` and goes through `pdf_vl.py::describe_figure_vl()`.

---

> **ADR-0008** ([docs/adr/0008-embedding-model-evaluation.md](../../adr/0008-embedding-model-evaluation.md))
> — Accepted Option D: domain-fine-tune `bge-small` in place (keep
> 384-dim). Bigger embedders (e.g., bge-large, jina-v3) were evaluated
> and rejected because the dim cost on the Qdrant payload + reranker
> latency on CPU outweighed recall gains.
>
> **ADR-0003** ([docs/adr/0003-defer-v2m3-gpu-reranker.md](../../adr/0003-defer-v2m3-gpu-reranker.md))
> — Proposed (Deferred): bumping to `bge-reranker-v2-m3` requires a GPU
> reranker host. Trigger conditions for the bump live in the ADR.

## 2. bge-small-en — dense embedder

**Classification:** ML model (single forward pass per chunk).

- Path: `BAAI/bge-small-en-v1.5` (or local equivalent), loaded via
  `sentence-transformers`.
- Vector dim: 384.
- Where: runs **on the GPU** in `hatchet-worker-ai`
  ([docker-compose.yml:2286-2292](../../../docker-compose.yml)).
- Throughput: 144 chunks/s on A4500 vs 3-4 chunks/s CPU
  ([project_gpu_acceleration_2026_05_22](../notes/INDEX.md#project_gpu_acceleration_2026_05_22)).
- Cache: `HF_HOME=/tmp/hf_cache`, `SENTENCE_TRANSFORMERS_HOME=/tmp/hf_cache` (mounted as `fastapi_hf_cache` named volume).
- Invocation:
  [src/fastapi/app/hatchet_workflows/embed_pending_passages.py](../../../src/fastapi/app/hatchet_workflows/embed_pending_passages.py)
  + `services/passage_embedder.py`.
- Target collection: Qdrant `reports` (silver.document_passages),
  `public_geoscience`.

---

## 3. bge-reranker-base — cross-encoder reranker

**Classification:** ML model.

- Loaded via `sentence-transformers.CrossEncoder`.
- Where: runs on **CPU** in the `fastapi` container (no GPU contention with
  vLLM during chat).
- Thread bound: `OMP_NUM_THREADS=10`, `TOKENIZERS_PARALLELISM=false`
  ([docker-compose.yml:898-900](../../../docker-compose.yml)) — bge-reranker’s
  intra-op parallelism would otherwise oversubscribe vs vLLM CPU work.
- Timeout split (from [project_latency_fix_2026_05_20](../notes/INDEX.md#project_latency_fix_2026_05_20)):
  - Qdrant `wait_for` 2 s
  - Reranker `wait_for` 8 s
  - Pre-truncate query+passage to 2000 chars
  - Halve candidates (20 → 10) before reranking
  - Result: cold path 6 s real answers vs prior 3 s refusals.
- Invocation: inside the agentic LangGraph `execute_node`, fed by
  `services/fusion.py`.

### Reranker fine-tune pipeline (`reranker_labels` asset group)

[project_reranker_v1](../notes/INDEX.md#project_reranker_v1) — Path C: fine-tune in place via LoRA.

- Synthetic-label asset:
  [src/dagster/georag_dagster/assets/reranker_labels.py](../../../src/dagster/georag_dagster/assets/reranker_labels.py)
  + [reranker_labels_helpers.py](../../../src/dagster/georag_dagster/assets/reranker_labels_helpers.py).
- Writes `eval.reranker_training_pairs`.
- Eval harness: [src/fastapi/scripts/eval_reranker_*.py](../../../src/fastapi/scripts/).
- LoRA trainer: `src/fastapi/scripts/train_reranker_lora.py` —
  runs inside the `fastapi` container with GPU passthrough
  ([docker-compose.yml:1086-1094](../../../docker-compose.yml)). Stop vLLM
  before training (or accept slower throughput) — they share the A4500.
- Eval log artefacts under [docs/](../../) (`eval_rerun_120.log`,
  `lithology_derive_rerun.log`, etc.).

---

## 4. SPLADE++ — sparse retriever

**Classification:** ML model.

- Path: `naver/splade-cocondenser-ensembledistil`.
- Where: GPU on `hatchet-worker-ai` (~440 MiB resident).
- Encodes both ingest passages (sparse vectors in Qdrant `splade_*`
  collection) and queries at chat time.
- Used by `services/fusion.py` as one of the three retrieval legs
  (vector + sparse + BM25 → fused via RRF or DBSF).

---

## 5. tsvector / BM25 — Postgres lexical

**Classification:** Rule-based / classical IR.

- Stored as `tsvector` columns on `silver.document_passages.text`,
  `silver.lithology.description`, etc.
- GIN indexes back the lookup.
- Used by `bm25_search` tool inside the LangGraph.

---

## 6. Intent classifier

[src/fastapi/app/agent/agentic_retrieval/intent_classifier.py](../../../src/fastapi/app/agent/agentic_retrieval/intent_classifier.py).

**Classification:** Rule-based regex + LLM fallback.

- Primary path: compiled regex per intent (`_TRIGGERS`, line 115).
- Fallback path: LLM call (`_llm_classify_intent`) when confidence < 0.6
  AND an HTTP client is provided.
- Returns `IntentResult` with `intent`, `confidence`, `second_choice`,
  `matches`, `tool_target`.

---

## 7. Sheet-type classifier (XLSX)

[src/dagster/georag_dagster/parsers/_sheet_classifier.py](../../../src/dagster/georag_dagster/parsers/_sheet_classifier.py).

**Classification:** Rule-based.

- Routes XLSX sheets to the right canonical bronze table based on
  header signatures + vendor aliases ([_vendor_aliases.py](../../../src/dagster/georag_dagster/parsers/_vendor_aliases.py)).
- Multi-sheet workbook fix (2026-05-23) — empty `sheet_type=''` now
  auto-dispatches via the classifier; aliases shared with CSV inference
  ([project_xlsx_audit_2026_05_23](../notes/INDEX.md#project_xlsx_audit_2026_05_23)).

## 8. CSV delimiter / decimal auto-detect

[src/dagster/georag_dagster/parsers/_csv_io.py](../../../src/dagster/georag_dagster/parsers/_csv_io.py)
+ [_encoding.py](../../../src/dagster/georag_dagster/parsers/_encoding.py).

**Classification:** Rule-based.

Three real CSV gaps closed in 2026-05-23
([project_csv_audit_2026_05_23](../notes/INDEX.md#project_csv_audit_2026_05_23)):
- Delimiter auto-detect.
- Decimal-comma transform.
- Dagster `csv_silver_ingest` concurrency pool.

## 9. Hole-ID extractor

`extract_hole_ids()` in [src/fastapi/app/agent/](../../../src/fastapi/app/agent/).

**Classification:** Rule-based regex.

See [Ch 06 §9](06-retrieval-and-agents.md#9-hole-id-extractor-rule-based).

## 10. Anomaly detector

[src/fastapi/app/agent/anomaly_detector.py](../../../src/fastapi/app/agent/anomaly_detector.py).

**Classification:** ML / statistical (no neural net).

- Z-score + IQR on per-element assays grouped by formation / hole.
- Returns ranked anomalies with confidence scores.
- Wired into the `anomaly_detection` intent.

## 11. Confidence computer

[src/fastapi/app/agent/confidence_computer.py](../../../src/fastapi/app/agent/confidence_computer.py).

**Classification:** ML (statistical calibration).

- Maps retrieval scores + reranker scores + cross-store agreement → a
  calibrated 0-1 confidence.
- Written to `silver.answer_runs.confidence`.

## 12. Lithology derive (rule-based + LLM hybrid)

`docs/lithology_derive_*.log` are eval artefacts. The actual code:
[src/fastapi/app/services/derive_intervals.py](../../../src/fastapi/app/services/derive_intervals.py).

- First pass: rule-based interval derivation from lithology logs.
- Second pass: LLM-assisted disambiguation for rock-code conflicts.
- v2 added confidence flag → drives `silver.lithology.rock_code_confidence`.

## 13. Phase 0 + Phase 5 agent registry (Pydantic AI)

[src/fastapi/app/agents/phase0/](../../../src/fastapi/app/agents/phase0/) etc.
Each Pydantic AI agent has a tool budget + timeout from `workspace.agent_timeouts`.

Agents in use:
- **Index Health** — `hypopg`-driven hypothetical index evaluation.
- **Storage Tiering** — moves bronze objects between hot/warm/cold tiers.
- **Store Reconciliation** — checks consistency across PG/Neo4j/Qdrant.
- **Support Packet** — bundles trace + audit + repro envelope for support.
- **LLM Incident Diagnosis** — multi-agent debugging.
- **Cost Burn Watcher** — Tier 3 unlock gating.

## 14. Models on disk (where + how much)

| Model | Container | Cache path | Approx size |
|---|---|---|---|
| Qwen3-14B-AWQ | `vllm` | `/root/.cache/huggingface` (vllm_hf_cache) | ~17 GB |
| Qwen2.5-VL-7B (optional) | `vllm` | same | ~14 GB |
| bge-small-en | `hatchet-worker-ai` | `/tmp/hf_cache` (fastapi_hf_cache shared) | ~135 MB |
| bge-reranker-base | `fastapi` | `/tmp/hf_cache` (fastapi_hf_cache) | ~280 MB |
| SPLADE++ | `hatchet-worker-ai` | `/tmp/hf_cache` | ~440 MB |
| rapidocr ONNX | `hatchet-worker-ingestion` + `hatchet-worker-ai` | `/tmp/rapidocr_models` (rapidocr_models named volume) | ~150 MB |
| Tesseract | both workers | system path | ~30 MB lang data |
| Docling | both workers | site-packages | bundled |
