# ADR 0021: Azure AI Foundry (Cohere Embed v4 / Rerank v4) replaces the self-hosted embedding and reranker in production

- **Date**: 2026-09-06 (record). The backends landed and were verified
  against live deployments on 2026-07-30; the code and compose defaults
  were flipped to Foundry on 2026-09-06 (#215). This ADR is the retroactive
  record the 2026-09-06 architecture reconciliation (#213) found missing.
- **Status**: Accepted
- **Deciders**: Kyle Maguire (SME)
- **Supersedes**: ADR-0008 (embedding model choice — domain-fine-tuned
  `bge-small`, 384-dim) for production; ADR-0003 (defer the GPU reranker
  host upgrade) — the question it deferred is moot in production. Both stay
  in force only as history of the dev-workstation stack. Amends
  `georag-architecture.html` §02, §04h, §05c, §08 and §11 (all already
  updated in v1.52 / v1.53).
- **Related**: ADR-0018 (single-GPU allocation on the dev workstation —
  unaffected), `src/fastapi/app/services/embedding.py`,
  `src/fastapi/app/services/reranker.py`,
  `src/fastapi/app/services/_foundry_retry.py`,
  `src/fastapi/app/services/ingest/passage_embedder.py`,
  `src/fastapi/app/agent/tools.py` (`search_documents`),
  `scripts/reset_embeddings_for_reencode.py`, `src/fastapi/scripts/reembed_qdrant.py`.

## Context

Dense retrieval in GeoRAG ran on self-hosted models: `Qwen/Qwen3-Embedding-0.6B`
(1024-dim, since the 2026-06-04 swap from `bge-small`) on the `embedding`
sidecar and `Qwen/Qwen3-Reranker-0.6B` on the `reranker` sidecar, the
latter needing the dev workstation's one RTX A4500 (ADR-0018). Sparse
retrieval is SPLADE++ on CPU.

Production moved to **Azure Container Apps** on 2026-07-30. The
environment has **no GPU**: every app runs on CPU workload profiles and
the whole stack scales to zero nightly. Measured on the ingestion path,
Qwen3-Embedding on CPU does roughly 4 chunks/s against ~144 chunks/s on the
A4500 (`passage_embedder.py`), and a 0.6B causal-LM reranker scoring 40
candidates per query on CPU cannot meet the reranker time budget at all.
The LLM already ran on the Azure AI Foundry resource `georag-foundry-cc`
(Cohere Command A+ over the OpenAI v1 API), and the same resource offers
Cohere **Embed v4** and **Rerank v4** deployments under the same endpoint
and key.

Two constraints shaped the choice:

1. **The Qdrant collection is 1024-dim.** `georag_chunks` was recreated at
   1024 for Qwen3; the 2026-06-01 incident (a 384-dim writer against a
   1024-dim collection, retrieval refusing every question) is why dimension
   drift is guarded at startup, in the embed sweep and in
   `reembed_qdrant.py`. Any replacement had to produce 1024-dim vectors or
   force another collection rebuild.
2. **SPLADE++ has no hosted equivalent.** Sparse encoding stays
   self-hosted wherever it runs: the `sparse` sidecar in compose,
   in-process (`sparse_encoder._get_sparse_model`, CPU) on Azure where no
   sidecar app exists.

## Options considered

| Option | Where it runs | Effort | Outcome |
|---|---|---|---|
| A. Keep the Qwen3 sidecars on Container Apps CPU | ACA, CPU | Low | Rejected — ~4 chunks/s ingest; reranker blows `TIMEOUT_RERANKER_S`; both apps scale to zero nightly so every morning is a cold model load. |
| B. ACA GPU workload profile for the sidecars | ACA, GPU | Medium | Rejected — an always-on GPU node for a pilot with < 10 users; the nightly scale-to-zero jobs and the cost objection recorded in `deploy/azure/README.md` cut the other way. |
| C. Azure OpenAI `text-embedding-3-large` (+ no rerank product) | Foundry | Medium | Rejected — no rerank endpoint, so the reranker problem remains; 3072-dim default would force a Qdrant rebuild or Matryoshka truncation with no calibration data. |
| D. **Cohere Embed v4 + Rerank v4 on the existing Foundry resource** | **Foundry** ✅ | Medium | Chosen — same endpoint/key as the LLM, rerank exists, Matryoshka output at 1024 keeps the collection schema. |

## Decision

Production embeds with **Cohere Embed v4** and reranks with **Cohere
Rerank v4** on `georag-foundry-cc`, selected by `EMBEDDING_BACKEND=foundry`
and `RERANKER_BACKEND=foundry`. Since 2026-09-06 both values are the code
default (`services/embedding.py`, `services/reranker.py`) and the
`docker-compose.yml` fallback; `.env.example` sets `local` /
`cross_encoder` explicitly so the dev stack keeps the self-hosted sidecars.

Wire contracts, verified 2026-07-30 against live deployments and distinct
from the LLM's chat-completions surface:

- `POST {AZURE_FOUNDRY_ENDPOINT}/providers/cohere/v2/embed` with
  `{"model": <AZURE_FOUNDRY_EMBED_DEPLOYMENT>, "texts": [...], "input_type":
  "search_document"|"search_query", "embedding_types": ["float"],
  "output_dimension": 1024}`.
- `POST {AZURE_FOUNDRY_ENDPOINT}/providers/cohere/v2/rerank` with
  `{"model": <AZURE_FOUNDRY_RERANK_DEPLOYMENT>, "query", "documents",
  "top_n"}`, returning a calibrated `[0, 1]` `relevance_score`.

### What stays the same

- `georag_chunks` schema: 1024-dim dense + SPLADE++ sparse. No Qdrant
  migration; a full re-embed is still mandatory on switch (different model,
  different vector space).
- The retrieval pipeline shape (§04h): Qdrant hybrid prefetch + RRF, then
  rerank inside `search_documents`, top 40 in / top 12 out.
- `settings.EMBEDDING_MODEL_NAME` / `RERANKER_MODEL_NAME` remain as parity
  checks and as the dev-stack model ids; they are not runtime selectors.
- The dev workstation stack (ADR-0018): Qwen3-Embedding on CPU, SPLADE++
  on CPU, Qwen3-Reranker on the GPU.

### What changed

- `services/embedding.py`: `_FoundryEmbedding` behind the
  `SentenceTransformer.encode` surface, plus `embed_query()` for Cohere's
  `search_query` input type; `AZURE_FOUNDRY_EMBED_DEPLOYMENT`,
  `AZURE_FOUNDRY_EMBED_DIMENSION=1024`, `AZURE_FOUNDRY_EMBED_TIMEOUT_S=30`.
- `services/reranker.py`: `_FoundryReranker` short-circuits before any
  local model load; `AZURE_FOUNDRY_RERANK_DEPLOYMENT`,
  `AZURE_FOUNDRY_RERANK_TIMEOUT_S=8.0` clamped to half the caller budget,
  two retries at 2 s / 4 s (2026-08-20), version string
  `cohere-foundry:<deployment>` persisted to `answer_runs.reranker_version`.
- `services/_foundry_retry.py`: shared 429/5xx backoff honouring
  `Retry-After`, because Foundry TPM quota is shared per subscription and a
  corpus re-embed trips transient 429s.
- `agent/tools.py`: candidates truncated to 8000 chars for Foundry
  (2000 for the local cross-encoder); scores are used raw (no sigmoid);
  `RERANKER_SCORE_THRESHOLD_FOUNDRY=0.2` applied instead of the logit floor
  (2026-08-15).
- `config.py`: `RETRIEVAL_TOP_N=40` (Foundry scores all N in one call).
- `passage_embedder.load_embedding_model()` reads the same flag, so ingest
  and query embed with the same model.
- CI: `e2e-smoke` and `eval-gate` run with `EMBEDDING_BACKEND=foundry`
  against `tests/e2e_smoke/stub_backend.py`, so the Foundry code path is the
  one exercised end-to-end.

## Migration mechanics (switching a deployment's backend)

1. Deploy Embed v4 and Rerank v4 on the Foundry resource; note the
   deployment names. Reversible.
2. Set `EMBEDDING_BACKEND=foundry` on **both** `fastapi-cc` (query path)
   and `hatchet-worker-cc` (ingest path), and `RERANKER_BACKEND=foundry`
   on `fastapi-cc`, with the `AZURE_FOUNDRY_*_DEPLOYMENT` names. A mismatch
   between the two embedding apps writes one vector space and queries
   another. Reversible.
3. Run `scripts/reset_embeddings_for_reencode.py` to NULL `embedding_id`
   on every passage, then let `embed_pending_passages` (every 10 minutes)
   re-embed; watch the `EMBED_PENDING_PASSAGES` gauge drain. Point of no
   return for the old vectors is the first upsert into `georag_chunks`;
   snapshot the collection first if rollback matters.
4. Re-run the golden set (§07e; `scripts/run_golden_benchmark.py` records
   the backend fingerprint) before flipping traffic.
5. Confirm `answer_runs.reranker_version` reads `cohere-foundry:<deployment>`
   on new runs.

## Gotchas hit (worth knowing for next time)

1. **Defaults selected a host that does not exist on Azure.** Until
   2026-09-06 the code defaults were `local` / `cross_encoder`; an unset
   variable on an Azure app silently ran with no embedding model and an
   RRF-only reranker. Fixed in #215; both defaults are now `foundry`.
2. **The score floor was a no-op.** `RERANKER_SCORE_THRESHOLD=0.0` is a
   sign check for logits; Cohere returns a `[0, 1]` probability, so every
   candidate passed. `RERANKER_SCORE_THRESHOLD_FOUNDRY=0.2` fixed it
   (2026-08-15) and is the only retrieval-quality gate in the system today.
3. **Per-call timeout equal to the caller budget.** An 8 s HTTP timeout
   under an 8 s `wait_for` left no room for a retry; the budget is now
   derived from `TIMEOUT_RERANKER_S` with a margin (2026-08-20).
4. **Shared TPM quota.** A full re-embed can 429 for reasons unrelated to
   the request; before `_foundry_retry.py`, a 429 was treated as a
   permanent per-batch failure and passages were silently skipped.
5. **Foundry blocked 1,421 of 2,524 calls on 2026-08-17 and nothing
   noticed** until the `georag-foundry-cc-client-errors` alert was added.
6. **`input_type` asymmetry matters.** Ingestion uses `search_document`,
   queries must use `search_query`; callers unaware of the distinction fall
   back to `encode()` via a `hasattr(model, "embed_query")` check.
7. **No embedding cache exists** (§05c); re-embedding is bounded only by
   `embedding_id` being NULL.

## Consequences

### Positive

- Production needs no GPU and no model host; ingest and query scale with
  the Foundry quota rather than a sidecar.
- One resource, one key, one alerting surface for LLM, embed, rerank and
  (since ADR-0019) OCR.
- Cohere Rerank v4's calibrated scores gave the system its first usable
  retrieval-quality floor.

### Negative

- Vendor and network dependency on Foundry for every query; a Foundry
  outage degrades the reranker to RRF order and, with no embedding model,
  empties document retrieval (Qdrant falls back to `pg_trgm` only when
  Qdrant itself is down).
- No recorded golden-set comparison between Qwen3 (dev) and Cohere
  (production); the +13.9% NDCG@10 figure in
  `docs/architecture/manual/18-model-stack-evolution.md` is Qwen3 vs bge,
  not Cohere vs Qwen3. Dev and production therefore retrieve differently.
- Per-call cost and quota, shared with the LLM on the same resource.
- Cohere Parse v5 on the same resource is a Preview SKU with a listed
  retirement date (ADR-0019); Embed v4 / Rerank v4 deployment lifecycles
  should be tracked the same way.

## Verification

- `src/fastapi/tests/test_backend_selection.py` (backend selection, loud
  failure on partial config, and the 2026-09-06 default pins),
  `test_foundry_retry.py`, `test_reranker_retry_budget.py`,
  `test_agent_tools.py` (raw-score handling and the Foundry threshold).
- `e2e-smoke` / `eval-gate` CI jobs run the Foundry path against the stub.
- Startup logs `Embedding model via Azure AI Foundry: deployment=… dim=1024`
  and `Reranker ready: backend=foundry`.

## Follow-ups (not part of this ADR)

- Record a golden-set comparison of Cohere Embed v4 / Rerank v4 against the
  dev Qwen3 stack — before the next reranker or embedding change, so §07e's
  promotion gate has a production baseline.
- Track Embed v4 / Rerank v4 deployment retirement dates alongside Parse v5.
- Decide whether SPLADE++ in-process on Azure CPU is acceptable long-term or
  needs its own app.
- Mark ADR-0003 and ADR-0008 as superseded for production (done in this
  change).
