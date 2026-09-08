# Chapter 08 — LLM and ML Models

> **Reconciled 2026-09-07** against `src/fastapi/app/config.py`,
> `app/agent/llm_calls.py`, `app/services/embedding.py`,
> `app/services/reranker.py`, `app/services/sparse_encoder.py`,
> `docker-compose.yml` and `.env.production.example`. The previous version
> opened with a self-hosted vLLM server as the LLM tier; that service was
> deleted on 2026-07-30, and the default backend became Azure AI Foundry
> (then Amazon Bedrock on 2026-09-08).
> Sections 7 and 8 pointed into `src/dagster/`, deleted 2026-08-28.
>
> **⚠️ 2026-09-08 — production moved from Azure Container Apps to AWS
> ([ADR-0022](../../adr/0022-aws-replaces-azure-as-the-production-cloud.md)).**
> Every production reference below — Container Apps, Azure Blob, Azure AI
> Foundry, Log Analytics, Flexible Server, the `-cc` app names — is now
> HISTORY. What replaced each is in
> [deploy/aws/README.md](../../../deploy/aws/README.md) and
> [deploy/aws/MIGRATION-PLAN.md](../../../deploy/aws/MIGRATION-PLAN.md).
> **§1, §2 and §3 were rewritten on 2026-09-08.** Ch 18 names this
> chapter as the authority on the current model stack — "where the two
> disagree, Ch 08 wins" — and the model tier is precisely what the cloud
> move changed. A dated notice on a chapter other chapters defer to is not
> enough. What is deliberately KEPT is the Foundry wire contract, marked
> as history, because three of its behaviours were confirmed empirically
> and their Bedrock counterparts have not been: knowing what was verified
> where is the point.


Every model the system runs, by **kind** and **where it executes**.

A model VERSION changed at the move, not just a host, so read the reranker
row carefully.

| Role | Dev (compose) | Production (AWS, since 2026-09-08) |
|---|---|---|
| LLM | Amazon Bedrock, Cohere Command A+ | same — but a Bedrock **Marketplace** endpoint, not serverless: Bedrock's serverless Cohere generative catalogue is Command R/R+ (legacy) |
| Embeddings | `embedding` sidecar, Qwen3-Embedding-0.6B (CPU) | Bedrock, Cohere Embed v4 (1024-dim) |
| Reranker | `reranker` sidecar, Qwen3-Reranker-0.6B (GPU) | Bedrock, Cohere **Rerank 3.5** — NOT v4, which Bedrock does not serve |
| Sparse | `sparse` sidecar, SPLADE++ (CPU) | **the `sparse` service — no hosted equivalent anywhere, on Bedrock or Cohere's own API** |
| Scanned-page OCR | Cohere Parse 5 on Bedrock, Tesseract fallback | same — also a Marketplace endpoint; Bedrock's serverless catalogue has no Parse model at all |

⚠️ **The reranker dropped a major version, and it matters.**
`RERANKER_SCORE_THRESHOLD_HOSTED` (0.2, renamed from `_FOUNDRY`) was
measured against Rerank **v4** on 2026-08-15 and is the only
retrieval-quality gate in the system (hard rule 5, as built). It is carried
over to 3.5 **unvalidated** and must be re-measured on the golden set: too
low and it stops filtering, too high and the refusal rate climbs, and
neither shows up in any metric anything scrapes.

⚠️ **Page-image verbalization has no replacement.** `gpt-5-mini` was an
Azure OpenAI model on the Foundry resource, and unlike everything else in
this table it was never a Cohere model — so "keep the model, change the
host" does not apply. `page_vision_client` reports itself unconfigured
until a Bedrock vision model is chosen. The feature is gated behind
`IMAGE_VERBALIZATION_ENABLED` and has never run in production (§1.4).

## 1. The LLM tier — Amazon Bedrock

`LLM_BACKEND` selects the backend: **`bedrock`** (default) | `vllm` |
`anthropic`. `azure` is a **startup error** naming its replacement, not an
ignored value, and so is any leftover `AZURE_FOUNDRY_*` variable: a
deployment that was never repointed carries well-formed settings addressing
a resource that no longer exists, which would otherwise start cleanly and
die at the first query.

### 1.1 Bedrock (the default)

| Field | Value |
|---|---|
| Model | Cohere Command A+, on a Bedrock **Marketplace** endpoint — Bedrock's serverless Cohere generative catalogue is Command R/R+ (legacy), not this model |
| Wire API | `bedrock-runtime` **Converse** / **ConverseStream**, signed with SigV4. No base URL and no API key; boto3 resolves the endpoint from `BEDROCK_REGION` and the credentials from the ECS task role |
| Model field | `BEDROCK_CHAT_MODEL_ID`, the endpoint ARN. **No default** — naming one would be guessing at a resource that bills while it exists |
| Streaming | ConverseStream, forwarded by FastAPI as `status`/`bind`/`delta`/`citation`/`completed`/`failed` frames — unchanged |

Two shapes differ from every OpenAI-compatible backend and are easy to get
wrong silently: `system` is a **top-level parameter** in Converse, not a
message with `role: "system"` (a system message sent as a user turn still
produces plausible output), and the request is capped so
`prompt_tokens + max_tokens` cannot exceed `BEDROCK_CHAT_MAX_MODEL_LEN` —
Bedrock answers 400 rather than truncating, so an over-long request fails
after paying to build the prompt.

`app/config.py`'s `BEDROCK_*` block is the authority. `effective_llm_url`
raises for `bedrock` as well as `anthropic`, and for the same reason:
neither has an OpenAI-shaped base URL to resolve.

#### What was verified on Foundry, and is NOT verified here

The Foundry contract was confirmed empirically against a live deployment on
2026-07-30, including three things a reader would not assume:

- JSON `response_format` is supported.
- Reasoning arrives in a **separate `reasoning_content` field**, not inside
  the message content.
- Cohere wraps JSON output in `<|START_TEXT|>` / `<|END_TEXT|>` sentinel
  tokens, which the client strips.

**None of the three was re-verified on Bedrock.** `app/agent/llm_bedrock.py`
is written to tolerate either shape — it strips the sentinels
unconditionally and handles both Converse's `reasoningContent` blocks and
the Foundry-era sibling field — but tolerance is not knowledge.
`ops/validation/bedrock_probe.py` exists to close this, and ADR-0022 makes
its committed report the gate on trusting any of these adapters.

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

`MODEL_TIER_FAST` was a Settings field until 2026-09-07, when it went with
its only reader, `app/agent/llm_classifier.py`. `MODEL_TIER_STANDARD` and
`MODEL_TIER_DEEP` were never Settings fields at all — the tier names survive
only in `agent/pricing.py`'s rate table keys. `scripts/check_settings_have_readers.py`
gates this in CI: a field with no reader fails the build, because a setting
nothing reads is a control that looks like it works.

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

`EMBEDDING_BACKEND` is **`bedrock` by default in code and in compose** since
2026-09-08, so an unset value on a production task selects Cohere Embed v4
rather than a model host that does not exist there. `.env.example` sets
`local` explicitly to use the dev sidecar. `foundry` — the default between
2026-09-06 and 2026-09-08 — now RAISES rather than falling through to the
sidecar branch, because on a host with no sidecar that fall-through means a
query path that retrieves nothing while reporting success.

| Backend | Path |
|---|---|
| `bedrock` | `bedrock-runtime.invoke_model`, Cohere Embed v4 asked for 1024-dim output ([`services/embedding.py`](../../../src/fastapi/app/services/embedding.py)). **Wire shape unverified** — the Foundry path was confirmed live on 2026-07-30, this one has not been; run `ops/validation/bedrock_probe.py` |
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

`RERANKER_BACKEND` also defaults to `bedrock`, and `foundry` is likewise
rejected at startup.

| Backend | Path |
|---|---|
| `bedrock` | the **`bedrock-agent-runtime` Rerank API** — not `InvokeModel`, and not Cohere's own `/v2/rerank` surface. Scores come back as `relevanceScore`, not `relevance_score`. Cohere **Rerank 3.5**, scoring all N documents in one call ([`services/reranker.py`](../../../src/fastapi/app/services/reranker.py)). **Wire shape unverified** |
| `cross_encoder` / `qwen3_causal` | the `reranker` sidecar, `Qwen/Qwen3-Reranker-0.6B` — a CausalLM returning a yes/no token-logit ratio behind a CrossEncoder-shaped interface |

⚠️ Foundry served Rerank **v4**; Bedrock serves **3.5**. See the version
warning at the top of this chapter — `RERANKER_SCORE_THRESHOLD_HOSTED` was
measured against v4 and is the only retrieval-quality gate in the system.

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
- **Sparse has no hosted equivalent anywhere** — not on Foundry, not on
  Bedrock, not on Cohere's own API. Whichever backend the dense side uses,
  SPLADE++ is self-hosted or the sparse leg is simply absent. ADR-0022
  decision 4 chose self-hosted, which is why `sparse` is the one model
  sidecar that exists as a production ECS service. The single most
  important asymmetry in the production model stack.

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
| Cohere Command A+, Embed v4, Rerank 3.5, Parse | Amazon Bedrock (external managed service; Command A+ and Parse on Marketplace endpoints) | n/a | n/a |

The `vllm_hf_cache` volume and the Qwen3-14B / Qwen2.5-VL weights went with
the vLLM service on 2026-07-30. `bge-small-en` and `bge-reranker-base` were
the pre-2026-06 defaults and are no longer downloaded.
