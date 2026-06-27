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
- `ANTHROPIC_MODEL=claude-opus-4-8`
- `ANTHROPIC_MAX_OUTPUT_TOKENS=4096`
- `ANTHROPIC_ENABLE_PROMPT_CACHING=true`
- `ANTHROPIC_USE_PRIORITY_TIER=false`
- `MODEL_TIER_FAST=claude-haiku-4-5`
- `MODEL_TIER_STANDARD=claude-sonnet-4-6`
- `MODEL_TIER_DEEP=claude-opus-4-8`

### Qwen2.5-VL (figures)

`VLLM_MODEL=Qwen/Qwen2.5-VL-7B-Instruct` is supported as a §04p Stage-6
config. The VL pass over figure pages is gated on
`DOCLING_VL_ENABLED` and goes through `pdf_vl.py::describe_figure_vl()`.

---

> ## ⚠️ MODEL STACK SWAPPED 2026-06-03 — config/runtime split
>
> The embedding + reranker models were swapped to the Qwen3 line on
> **2026-06-03** (the "Qwen ecosystem swap"), but the swap reached
> production via **`.env` override only** — the code defaults, the
> Dagster re-index assets, and the compose defaults still name the old
> bge models. **Read the two-column table below as the source of truth.**
>
> | Slot | Production (live, env-driven) | Code default (stale) |
> |---|---|---|
> | Dense embedder | `Qwen/Qwen3-Embedding-0.6B`, **1024-dim** ([config.py:899,904](../../../src/fastapi/app/config.py)) | `BAAI/bge-small-en-v1.5`, 384-dim ([embedding_service.py:41](../../../src/fastapi/app/embedding_service.py), [docker-compose.yml:959](../../../docker-compose.yml)) |
> | Cross-encoder reranker | `Qwen/Qwen3-Reranker-0.6B` (via `RERANKER_MODEL_PATH` override) ([config.py:929](../../../src/fastapi/app/config.py)) | `BAAI/bge-reranker-base@2cfc18c9` ([services/reranker.py:77,81](../../../src/fastapi/app/services/reranker.py)) |
>
> **🔴 Live re-index hazard.** The Dagster index assets still declare
> 384-dim `VectorParams` ([index_document_passages.py:67,263](../../../src/dagster/georag_dagster/assets/index_document_passages.py),
> [index_reports.py:64](../../../src/dagster/georag_dagster/assets/index_reports.py),
> [index_public_geoscience.py:82](../../../src/dagster/georag_dagster/assets/index_public_geoscience.py)).
> Production `georag_chunks` is 1024-dim — **re-running a Dagster index
> asset would recreate the collection at 384-dim and break retrieval.**
> Tracked as a Z-roadmap item; the live re-embed used a standalone
> script ([scripts/reembed_qdrant.py:47-48](../../../src/fastapi/scripts/reembed_qdrant.py),
> `_embed_silver_pending_cutover.py`), not the Dagster asset.
>
> **ADR history (now partly superseded):**
> - **ADR-0008** ([0008](../../adr/0008-embedding-model-evaluation.md)) Accepted Option D (domain-FT bge-small 384-dim) — **superseded** by the 2026-06-03 Qwen3 swap, which discards the bge-small domain FT.
> - **ADR-0011** ([0011](../../adr/0011-reranker-domain-adaptation.md)) Proposed reranker domain adaptation (vocab → MLM → full FT on bge-reranker-base) — **dormant**: it predates the Qwen3-Reranker swap and is framed entirely around bge.
> - **ADR-0003** ([0003](../../adr/0003-defer-v2m3-gpu-reranker.md)) Proposed/Deferred bge-reranker-v2-m3.
>
> Full audit-wave detail: [Ch 18 — Model Stack Evolution](18-model-stack-evolution.md).

## 2. Dense embedder — Qwen3-Embedding-0.6B (was bge-small)

**Classification:** ML model (single forward pass per chunk).

- **Production**: `Qwen/Qwen3-Embedding-0.6B`, **1024-dim, cosine**
  ([config.py:899-911](../../../src/fastapi/app/config.py)). Optional
  Qwen3 instruction-prefix support via `EMBEDDING_QUERY_PROMPT_NAME`
  (default off).
- **Code default still bge-small 384-dim** — see the hazard box above.
- Loaded via `sentence-transformers` in the `fastapi` lifespan
  ([main.py:564-584](../../../src/fastapi/app/main.py)), or proxied to the
  **embedding sidecar** ([embedding_service.py](../../../src/fastapi/app/embedding_service.py))
  when `EMBEDDING_SERVICE_URL` is set (the 2026-06-24 one-copy fix).
- Invocation: [embed_pending_passages.py](../../../src/fastapi/app/hatchet_workflows/embed_pending_passages.py)
  + `services/passage_embedder.py`.
- Target collection: Qdrant **`georag_chunks`** (canonical, ADR-0010),
  plus `georag_reports` (legacy) + `public_geoscience`.
- Cutover: production cutover initiated 2026-06-04 — 9,099 silver
  passages re-embedded into the 1024-dim collection at ~113 passages/min
  on A4500; post-swap eval baseline uncommitted/pending
  ([ops/baselines/qwen3-embedding-cutover-2026-06-04.md](../../../ops/baselines/qwen3-embedding-cutover-2026-06-04.md)).

---

## 3. Cross-encoder reranker — Qwen3-Reranker-0.6B (was bge-reranker-base)

**Classification:** ML model.

- **Production**: `Qwen/Qwen3-Reranker-0.6B` — a CausalLM that returns a
  yes/no token-logit ratio, wrapped by a `sentence-transformers`
  `CrossEncoder`-style interface, loaded via the `RERANKER_MODEL_PATH`
  env override ([services/reranker.py:181-211](../../../src/fastapi/app/services/reranker.py)).
- **Code default still `BAAI/bge-reranker-base@2cfc18c9`** ([reranker.py:77-82](../../../src/fastapi/app/services/reranker.py)) — see hazard box.
- Where: **CPU** in the `fastapi` container (no GPU contention with vLLM
  during chat), or the **reranker sidecar** ([reranker_service.py](../../../src/fastapi/app/reranker_service.py))
  when `RERANKER_SERVICE_URL` is set — the 2026-06-24 fix for the
  "6 uvicorn workers each load a reranker copy → OOM" problem
  ([fastapi resource fixes 2026-06-24](../notes/INDEX.md)).
- Thread bound: `OMP_NUM_THREADS=10`, `TOKENIZERS_PARALLELISM=false`.
- Timeout split (from [project_latency_fix_2026_05_20](../notes/INDEX.md#project_latency_fix_2026_05_20)):
  Qdrant `wait_for` 2 s, reranker `wait_for` 8 s, pre-truncate to 2000
  chars, halve candidates 20→10.
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
