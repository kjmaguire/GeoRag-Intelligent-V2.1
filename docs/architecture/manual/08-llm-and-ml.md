# Chapter 08 — LLM and ML Models

> **Reconciled 2026-09-07** against `src/fastapi/app/config.py`,
> `app/agent/llm_calls.py`, `app/services/embedding.py`,
> `app/services/reranker.py`, `app/services/sparse_encoder.py`,
> `docker-compose.yml` and `.env.production.example`. The previous version
> opened with a self-hosted vLLM server as the LLM tier; that service was
> deleted on 2026-07-30 and the default backend is Azure AI Foundry.
> Sections 7 and 8 pointed into `src/dagster/`, deleted 2026-08-28.

Every model the system runs, by **kind** and **where it executes**.

| Role | Dev (compose) | Production (Azure) |
|---|---|---|
| LLM | Azure AI Foundry, Cohere Command A+ | same |
| Embeddings | `embedding` sidecar, Qwen3-Embedding-0.6B (CPU) | Foundry, Cohere Embed v4 |
| Reranker | `reranker` sidecar, Qwen3-Reranker-0.6B (GPU) | Foundry, Cohere Rerank v4 |
| Sparse | `sparse` sidecar, SPLADE++ (CPU) | **self-hosted or absent — no Foundry equivalent** |
| Scanned-page OCR | Cohere Parse v5 on Foundry, Tesseract fallback | same |

## 1. The LLM tier — Azure AI Foundry

`LLM_BACKEND` selects the backend: **`azure`** (default) | `vllm` |
`anthropic`. There is no `foundry` value for the LLM — that word is only
used by `EMBEDDING_BACKEND` and `RERANKER_BACKEND`.

### 1.1 Azure (the default)

| Field | Value |
|---|---|
| Deployment | `Cohere-command-a-plus-05-2026` (Preview), dev and prod |
| Wire API | the unified **OpenAI v1** surface: `{AZURE_FOUNDRY_ENDPOINT}/openai/v1/chat/completions` |
| Model field | `AZURE_FOUNDRY_DEPLOYMENT`, sent as the OpenAI `model` |
| Streaming | SSE, forwarded by FastAPI as `status`/`bind`/`delta`/`citation`/`completed`/`failed` frames |

The wire contract was confirmed empirically against a live deployment on
2026-07-30, including three things a reader would not assume:

- JSON `response_format` is supported.
- Reasoning arrives in a **separate `reasoning_content` field**, not inside
  the message content.
- Cohere wraps JSON output in `<|START_TEXT|>` / `<|END_TEXT|>` sentinel
  tokens, which the client strips.

`app/config.py`'s `AZURE_FOUNDRY_*` block is the authority; `effective_llm_url`
resolves the base URL per backend and raises for `anthropic`, which does not
use an OpenAI-shaped URL.

### 1.2 vLLM (still supported, no longer shipped)

The compose service is gone. `LLM_BACKEND=vllm` remains valid for an
operator pointing at their own OpenAI-compatible endpoint, and a startup
validator fails the service when `VLLM_URL` is empty rather than silently
falling back. `LLM_PRIMARY_MODEL` still defaults to `Qwen/Qwen3-14B-AWQ`,
which is the historical default and not a model this deployment runs.

### 1.3 Anthropic (optional fallback)

`LLM_BACKEND=anthropic` uses the native Anthropic API with a pooled
client; `REQUIRE_POOLED_ANTHROPIC_CLIENT` defaults true so a missing pool
fails loudly instead of constructing a client per call.

| Setting | Default |
|---|---|
| `ANTHROPIC_MODEL` | `claude-opus-4-8` |
| `ANTHROPIC_MAX_OUTPUT_TOKENS` | 4096 |
| `ANTHROPIC_ENABLE_PROMPT_CACHING` | true |
| `ANTHROPIC_USE_PRIORITY_TIER` | false |
| `MODEL_TIER_FAST` | `claude-haiku-4-5` |

`MODEL_TIER_STANDARD` and `MODEL_TIER_DEEP` are referenced by the routing
and pricing telemetry; only `MODEL_TIER_FAST` carries a default in
`config.py`.

Client: [`app/agent/llm_calls.py`](../../../src/fastapi/app/agent/llm_calls.py).

### 1.4 Vision / figures

`VLLM_MODEL` (default `Qwen/Qwen3-14B-AWQ`) is the model name for the
vLLM backend, not a separate VL deployment. Page-image description runs
through `services/ingest/page_verbalizer.py` and the
`verbalize_page_images` workflow, which is **inert unless
`IMAGE_VERBALIZATION_ENABLED` is set** — the hourly cron returns
immediately otherwise. See [Ch 05](05-pdf-stack.md).

## 2. Embeddings

**Classification:** ML model (one forward pass per chunk). 1024-dim, cosine,
matching the `georag_chunks` collection ([Ch 02 §2.2](02-data-stores.md)).

`EMBEDDING_BACKEND` is **`foundry` by default in code and in compose** since
2026-09-06, so an unset value on an Azure app selects Cohere Embed v4 rather
than a model host that does not exist there. `.env.example` sets `local`
explicitly to use the dev sidecar.

| Backend | Path |
|---|---|
| `foundry` | `POST {endpoint}/providers/cohere/v2/embed`, Embed v4 asked for 1024-dim output — verified live 2026-07-30 ([`services/embedding.py`](../../../src/fastapi/app/services/embedding.py)) |
| `local` | the `embedding` sidecar, `Qwen/Qwen3-Embedding-0.6B` pinned at revision `97b0c614`, reached over `EMBEDDING_SERVICE_URL` |

The sidecar exists because six uvicorn workers each loaded their own
~2.4 GiB copy and OOM-killed the container mid-stream (2026-06-24). Never
fold it back into the FastAPI image.

Invocation: [`embed_pending_passages`](../../../src/fastapi/app/hatchet_workflows/embed_pending_passages.py)
plus `services/passage_embedder.py`.

**Switching backends requires a full re-embed** — dimensions match, but the
vector spaces do not. `scripts/reset_embeddings_for_reencode.py` is the tool.

## 3. Reranker

**Classification:** ML model.

`RERANKER_BACKEND` also defaults to `foundry`.

| Backend | Path |
|---|---|
| `foundry` | `POST {endpoint}/providers/cohere/v2/rerank`, Rerank v4, scores all N documents in one call — verified live 2026-07-30 ([`services/reranker.py`](../../../src/fastapi/app/services/reranker.py)) |
| `cross_encoder` / `qwen3_causal` | the `reranker` sidecar, `Qwen/Qwen3-Reranker-0.6B` — a CausalLM returning a yes/no token-logit ratio behind a CrossEncoder-shaped interface |

A degraded reranker is not silent: `georag_rerank_degraded_total` counts
calls that returned RRF-ordered results because the reranker timed out or
raised. Answers on that path carry raw fusion scores an order of magnitude
below a Cohere score, which drags down citation relevance for evidence that
was fine. Nothing scrapes that counter ([Ch 12 §2.1](12-observability.md)).

The `reranker_labels` LoRA fine-tune pipeline was a Dagster asset group and
went with the tree on 2026-08-28. `eval.reranker_training_pairs` still
exists; nothing writes it.

## 4. SPLADE++ — sparse retriever

**Classification:** ML model.

- Path: `naver/splade-cocondenser-ensembledistil`.
- Where: the **`sparse` sidecar** on CPU, reached over `SPARSE_SERVICE_URL`
  ([`services/sparse_encoder.py`](../../../src/fastapi/app/services/sparse_encoder.py)).
  There is no `hatchet-worker-ai`; one merged worker runs everything.
- Encodes ingest passages and chat-time queries into the **named `text`
  sparse vector on `georag_chunks`** — not a separate collection
  ([Ch 02 §2.2](02-data-stores.md)).
- One of the retrieval legs fused in `services/fusion.py` (RRF or DBSF).
- **Sparse has no Foundry equivalent.** Whichever backend the dense side
  uses, SPLADE++ is self-hosted or the sparse leg is simply absent — the
  single most important asymmetry in the production model stack.

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

[`src/georag_geoparsers/georag_geoparsers/_sheet_classifier.py`](../../../src/georag_geoparsers/georag_geoparsers/_sheet_classifier.py)
— the parser package survived Dagster's deletion and is now imported by the
Hatchet ingest workflows.

**Classification:** Rule-based.

- Routes XLSX sheets to the right canonical bronze table based on
  header signatures + vendor aliases ([`_vendor_aliases.py`](../../../src/georag_geoparsers/georag_geoparsers/_vendor_aliases.py)).
- Multi-sheet workbook fix (2026-05-23) — empty `sheet_type=''` now
  auto-dispatches via the classifier; aliases shared with CSV inference
  ([project_xlsx_audit_2026_05_23](../notes/INDEX.md#project_xlsx_audit_2026_05_23)).

## 8. CSV delimiter / decimal auto-detect

[`_csv_io.py`](../../../src/georag_geoparsers/georag_geoparsers/_csv_io.py)
+ [`_encoding.py`](../../../src/georag_geoparsers/georag_geoparsers/_encoding.py).

**Classification:** Rule-based.

Three real CSV gaps closed in 2026-05-23
([project_csv_audit_2026_05_23](../notes/INDEX.md#project_csv_audit_2026_05_23)):
- Delimiter auto-detect.
- Decimal-comma transform.
- A concurrency pool on the CSV ingest path (was Dagster's `csv_silver_ingest`; the limit now lives in the `ingest_tabular` workflow's Hatchet concurrency key).

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
[src/fastapi/app/services/derive_intervals.py](../../../src/fastapi/app/services/ingest/derive_intervals.py).

- First pass: rule-based interval derivation from lithology logs.
- Second pass: LLM-assisted disambiguation for rock-code conflicts.
- v2 added confidence flag → drives `silver.lithology.rock_code_confidence`.

## 13. Phase 0 agent registry

[`src/fastapi/app/agents/phase0/`](../../../src/fastapi/app/agents/phase0/) —
eleven modules, dispatched by the `phase0_agents` workflow, each with a tool
budget and timeout from `workspace.agent_timeouts`. **Pydantic AI is
vestigial**: the framework is installed and the agents are shaped for it,
but the guards that matter run in `orchestrator_validators.py`
([Ch 06](06-retrieval-and-agents.md)). Full list in
[Ch 14](14-status-matrix.md#agents).

Agents in use:
- **Index Health** — `hypopg`-driven hypothetical index evaluation.
- **Storage Tiering** — moves bronze objects between hot/warm/cold tiers.
- **Store Reconciliation** — checks consistency across Postgres and Qdrant. The Neo4j leg went with the graph.
- **Support Packet** — bundles trace + audit + repro envelope for support.
- **LLM Incident Diagnosis** — multi-agent debugging.
- **Cost Burn Watcher** — Tier 3 unlock gating.

## 14. Models on disk (where + how much)

Only the three sidecars hold weights, and only in dev. Production runs no
model locally except Tesseract.

| Model | Where | Cache path | Approx size |
|---|---|---|---|
| `Qwen/Qwen3-Embedding-0.6B` | `embedding` sidecar (CPU) | `/tmp/hf_cache` | ~1.2 GB |
| `Qwen/Qwen3-Reranker-0.6B` | `reranker` sidecar (GPU) | `/tmp/hf_cache` | ~1.2 GB |
| SPLADE++ (`naver/splade-cocondenser-ensembledistil`) | `sparse` sidecar (CPU) | `/tmp/hf_cache` | ~440 MB |
| Tesseract 5.5.2 | `fastapi` and `hatchet-worker` images, built from source | system path | ~30 MB lang data |
| Cohere Command A+, Embed v4, Rerank v4, Parse v5 | Azure AI Foundry (external managed service) | n/a | n/a |

The `vllm_hf_cache` volume and the Qwen3-14B / Qwen2.5-VL weights went with
the vLLM service on 2026-07-30. `bge-small-en` and `bge-reranker-base` were
the pre-2026-06 defaults and are no longer downloaded.
