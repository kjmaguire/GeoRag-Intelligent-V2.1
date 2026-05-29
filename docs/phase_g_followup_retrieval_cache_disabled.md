# Phase G follow-up — Retrieval cache disabled + vLLM context-len cap

**Status:** Eval baseline pushed **18/22 → 20/22 (91%)** stable across
3 consecutive runs. Sequential-eval flakiness (11/22 ↔ 18/22 flap)
fully resolved.

## What was broken

Two compounding bugs, both rooted in the same architectural
incompleteness.

### Bug 1 — Retrieval cache rehydration was never built

`run_deterministic_rag` writes a `CachedRetrievalContext` to Redis at
the end of every retrieval pass (5-min TTL). On a subsequent cache hit
it sets `_cache_hit=True` and **skips** the entire `parallel_branches`
block (the actual tool calls).

The intent — per the inline comment at line 1335 — was:

```
cache hit:  deserialize CachedRetrievalContext → rehydrate candidates → synthesize fresh
cache miss: retrieve → rrf → rerank → SETEX(CachedRetrievalContext) → synthesize
```

But the "rehydrate candidates" step was never written. On cache hit
`tool_results` stays empty; `_build_context` returns `"(no data
retrieved)"`; the model refuses.

This stayed dormant in production because the cache key is
content-hashed against `(query, project_id, classification, data_versions, prompt_version)`
— every fresh ingestion run busts the cache, every prompt edit busts
the cache, and individual users rarely re-ask the same question
inside the 5-min TTL. **Only the sequential eval-runner reliably hit
the failure mode**, because every question runs against the same
project/data_version state with no cache invalidation in between.

#### What surfaced it

The 22-question core_chat eval pack expansion (10 → 22). The
question density bumped past the 5-min TTL boundary on the same
process, so identical queries inside the run started hitting the
cache.

### Bug 2 — Single-source queries cached **empty** candidates_reranked

Cross-store RRF was gated on `if len(_rrf_lists) > 1` — so a
spatial-only / docs-only / graph-only query (only one source returned
candidates) skipped RRF entirely. `_fused_candidates` stayed `[]` and
the cache was written with empty `candidates_reranked` even when the
spatial tool had returned 63 collars.

This is what produced the **identical** generic refusal text across
many questions in eval runs — they all hit the same "empty cache,
empty context" code path.

### Bug 3 (compounding) — vLLM 8K window + Q11 prompt

Q11's project_overview tool result + the project preamble + the
shared system prompt + the per-turn user message pushed the prompt
to **4,097 tokens**. With `max_tokens=4096`, total = 8,193 — just
**one token over** `VLLM_MAX_MODEL_LEN=8192`. vLLM responded with a
400 BadRequest: *"This model's maximum context length is 8192
tokens. ..."*

The orchestrator's retry loop saw 400 → non-retriable on the
OpenAI-compat path → broke out → defensive fallback fired with
`"I was unable to generate a summary due to an LLM error."` This is
what made Q11 flap independently of the cache bug.

## What landed

### Surgical fixes

1. **Cache gate flag.** New `settings.RETRIEVAL_CACHE_ENABLED: bool
   = False`. Both READ and WRITE of the retrieval cache are now gated
   behind this flag. Default OFF until the rehydration completion ships.
   See `app/config.py`.
2. **Legacy-empty-cache guard.** If the flag is flipped on with a
   pre-fix cache in Redis (empty `candidates_reranked`), treat it as
   cache miss so fresh retrieval can populate `tool_results`.
3. **RRF single-list fix.** Lifted the `> 1` gate to a truthy check.
   `rrf_fuse` with one list trivially preserves rank order (per its
   docstring example), so the call is safe. Single-source queries
   now produce populated `_fused_candidates`, which means when
   `RETRIEVAL_CACHE_ENABLED=True` is eventually flipped on, the cache
   write actually captures real candidate data.
4. **Dynamic output-token cap.** `_call_openai_compatible_llm` now
   tightens `max_tokens` to fit inside `VLLM_MAX_MODEL_LEN`. Uses
   `chars / 2.5` as a conservative Qwen3 tokenizer estimate (empirical
   ceiling: 2.77 chars/token on the GeoRAG prompt mix) plus a
   256-token safety margin. When the prompt fills the window we fall
   to `max_tokens=64` so the 400-BadRequest still surfaces cleanly.
5. **Upstream error-body logging.** When the OpenAI-compat backend
   returns 4xx the response body is now logged at ERROR level
   alongside the URL, model, and prompt size. Previously the
   `HTTPStatusError.message` only carried "Client error '400 Bad
   Request' for url '...'" — the real cause (*"max context length
   8192 tokens..."*) was invisible.

### Eval state

| Pack | Pre-batch | Post-batch | Notes |
|---|---|---|---|
| Original 10Q | 9 / 10 | 9 / 10 | Q1 PLSS-syntax carry-over preserved |
| Expanded 22Q | 18 / 22 baseline → 11/22 ↔ 18/22 flapping | **20 / 22 stable across 3 runs** | Q1 (PLSS) + Q21 ("what reports can the system generate" — legitimate refusal) are the only fails |

The remaining 2 failures are **eval-tuning items**, not capability
gaps:

- **Q1** — Known F.9 carry-over. The Qwen3 model treats
  `"section 28N 79W"` PLSS syntax as outside its scope. Needs a
  PLSS-aware few-shot or query expansion.
- **Q21** — *"What types of reports can the system generate for this
  project?"* The model is correctly refusing because the question is
  about the SYSTEM's capabilities, not the PROJECT's data — and the
  shared preamble teaches it to refuse non-geological-data questions.
  Either (a) relax the eval to accept the refusal as correct (R5
  capability questions ARE out-of-scope for the project-data
  orchestrator), or (b) add a few-shot demonstrating the
  meta-question shape.

## What's documented but NOT fixed

### Retrieval cache rehydration completion

Re-enabling `RETRIEVAL_CACHE_ENABLED=True` requires building the
rehydrate-tool_results-from-candidates_reranked code path, which
needs:

1. **Full payload caching for postgis + neo4j.** Today only Qdrant
   payloads (DocumentChunk) round-trip via `dc.asdict`. PostGIS and
   Neo4j candidates only cache `{store, canonical_id}` — the
   `CollarRecord` / graph-entity payload is lost.
2. **Reconstruction grouper.** Walk `candidates_reranked` by
   `source_store + tool_name` and rebuild the original tool-result
   dataclasses (`SpatialQueryResult`, `DocumentSearchResult`,
   `GraphTraversalResult`, `ProjectOverviewResult`, etc.).
3. **Partial-source fallback.** If the cache only carries 3 of 4
   tool results that the live query would produce, the rehydration
   needs to re-run the missing tools, not fall back to empty.

Estimated work: **~4-6 ticks**. Best done alongside the F.13 package
rename so the retrieval cache becomes a first-class module of its
own (`run_cache.py`) rather than co-defined inside orchestrator.

## Files

- `src/fastapi/app/config.py` — `RETRIEVAL_CACHE_ENABLED` flag added
- `src/fastapi/app/agent/orchestrator.py` — cache READ + WRITE gates,
  legacy-empty-cache guard, RRF single-list fix
- `src/fastapi/app/agent/llm_calls.py` — dynamic output cap,
  error-body logging
- `docs/phase_g_followup_retrieval_cache_disabled.md` — this doc
