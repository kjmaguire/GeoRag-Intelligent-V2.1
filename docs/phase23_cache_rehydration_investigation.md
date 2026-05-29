# Phase 23 Investigation — Cache rehydration is unimplemented; vLLM 400 on graph payloads

**Document version:** 1.0
**Status:** Investigation only. No code change landed.
**Predecessors:** `docs/phase22_handoff.md`, `docs/phase21_handoff.md`.

---

## 1. Two interlocking bugs

### Bug A — Cache rehydration is unimplemented (silent context loss)

`run_deterministic_rag` in `src/fastapi/app/agent/orchestrator.py`
has a Redis `CachedRetrievalContext` cache keyed on (query,
project_id, categories, data versions, system_prompt_version).

The cache **write** path (lines 3853–3958) correctly serialises
`_fused_candidates` as `CachedRetrievalCandidate` records with
their full payload dicts. Phase 21's guard added a non-empty
+ no-partial-failures gate on the write.

The cache **read** path:

- Lines 3179–3187: deserialises the cached context, sets
  `_cache_hit = True`, logs `CACHE HIT key=… candidates=N`.
- Line 3217–3220: rehydrates `_sparse_boost_applied` and
  `_sparse_boost_factor` from the cached context.
- Lines 3979–3988 (the `elif _cache_hit and …` branch):
  rehydrates `partial_failures` from cached `partial_failure_details`.

That is the *entire* cache-hit rehydration. **`candidates_reranked`
is never read.** Tool execution is gated on `not _cache_hit`
(lines 3303, 3379, 3743, etc.) so every tool — spatial, documents,
graph, assay, downhole — is skipped. `tool_results` stays `[]`.

Downstream:
- `_build_retrieval_summary(tool_results)` returns the empty
  string.
- `context_chars` lands around 19 (just static block headers).
- The LLM receives near-empty CONTEXT, and the system prompt's
  "If the context is empty say 'I don't have data on that in
  this project.'" instruction fires. The user gets a refusal
  even though the cache had 29 valid candidates.

The comment on line 3771 — "rehydrating from
CachedRetrievalContext.candidates_reranked" — names the missing
work explicitly. It was scaffolded but never built.

### Bug B — vLLM 400 on dense graph payloads

When tool execution does run for graph-classified queries
(`query_graph_by_label` returns up to 50 entities), the response
record stage hits vLLM with the rendered graph entity list plus
full property bags. For a query like
`gq-013-graph-formations` (which seeds 29 candidates), vLLM
rejects the request:

```
LLM call failed: Client error '400 Bad Request' for url
'http://vllm:8000/v1/chat/completions'
```

The orchestrator's exception handling at line 4604 then
references the unset `response` variable, producing
`UnboundLocalError: cannot access local variable 'response'
where it is not associated with a value` — a secondary cascade
masking the root vLLM error.

`MAX_CONTEXT_GRAPH_ENTITIES = 20` caps the rendered list, but
each entity's `properties` bag (description, age, code,
formation_type, deposit_type, …) carries hundreds of bytes;
20 of them comfortably exceed vLLM's per-request payload
threshold under the current `MAX_CONTEXT_TOKENS = 15_000`
ceiling combined with retrieval-context headers + system prompt.

---

## 2. Why these interlock — and why the test suite hides both

The cache silently masks the vLLM 400 bug. Workflow:

1. First-ever run of gq-013 after restart: cache miss → tools
   execute → 29 entities returned → context built → vLLM 400 →
   `UnboundLocalError` → INTERNAL_ERROR to the user → empty
   `_cached_candidates` → Phase 21's guard prevents the write.
2. Subsequent same-string runs: cache miss again (Phase 21
   guard worked) → tools execute → vLLM 400 → INTERNAL_ERROR.

Wait — but the trace in Phase 21+22 showed `candidates=29` in
cache. So the write did happen somewhere. Two explanations:

- An older v6 cache entry from before Phase 21's guard was added
  is still TTL-alive in Redis.
- Or the partial_failures path doesn't fire for vLLM 400 (which
  it shouldn't — vLLM failure happens *after* retrieval), so
  `_cached_candidates` still has 29 entries from tool execution
  even though synthesis crashed.

The second is more likely. Phase 21's guard checks `partial_failures`
which only tracks per-retrieval-tool failures, not LLM failures.
The cache writes the candidates before the LLM call, so a 400
during synthesis leaves the cache populated.

Then on next run, cache hits → `_cache_hit=True` → tools skip →
LLM gets empty context → refusal text. So **Bug A masks Bug B**:
the LLM 400 only surfaces on cache miss; on cache hit the user
sees a polite "I don't have data" refusal instead.

The full pytest suite mostly cache-misses (because data_version
churns when each test inserts an answer_run row, bumping
`project_data_version` mid-suite) so Bug B isn't dominant
across the suite. But running the same query twice in quick
succession deterministically hits Bug A.

---

## 3. Proposed fixes — Phase 24+

### Fix A (Bug A — cache rehydration)

In the `elif _cache_hit and _cached_retrieval_ctx is not None`
block around line 3979, reconstruct tool result objects from
`_cached_retrieval_ctx.candidates_reranked`:

```python
# Group cached candidates back into per-tool result objects.
docs_by_store, graph_by_store, spatial_by_store = [], [], []
for cand in _cached_retrieval_ctx.candidates_reranked:
    if cand.source_store == "qdrant":
        docs_by_store.append(reconstruct_chunk(cand.payload))
    elif cand.source_store == "neo4j":
        graph_by_store.append(reconstruct_entity(cand.payload))
    elif cand.source_store == "postgis":
        spatial_by_store.append(reconstruct_collar(cand.payload))

if docs_by_store:
    tool_results.append(("search_documents",
        DocumentSearchResult(chunks=docs_by_store, count=len(docs_by_store))))
if graph_by_store:
    tool_results.append(("traverse_knowledge_graph",
        GraphTraversalResult(entities=graph_by_store,
                              count=len(graph_by_store),
                              data_source="cache")))
# ... etc
```

Risk surface: `CachedRetrievalCandidate.payload` is `dict | None`
(dataclass→asdict). Need to reconstruct dataclass instances. Likely
introduces a `_payload_to_obj(store, payload)` helper in
`retrieval_cache.py`.

### Fix B (Bug B — vLLM 400)

Either:
- **Cap per-entity property bytes** in `_build_retrieval_summary`
  (truncate `description` to 200 chars, drop verbose
  `formation_type` strings).
- **Or** add a retry path that pares the graph entity list in
  half and re-tries the LLM call before raising.
- **Or** plain `try/except` around the LLM call so the
  `response` variable is always set to a graceful-degradation
  `GeoRAGResponse` (no more `UnboundLocalError` cascade).

The last is the safest first fix because it stops the
`UnboundLocalError` from masking the underlying vLLM signal.

---

## 4. What this phase delivered

Investigation only. The code change attempted in this session
(set `_cache_hit = False` on read to force rerun) was reverted
because:
- It exposed Bug B (vLLM 400 + UnboundLocalError) for any cache
  hit on a graph query.
- Test pass count fell 24 → 22 with an exception cascade on
  gq-013 / gq-011 / gq-017.
- The right fix is Fix A + Fix B in tandem, scoped properly
  in Phase 24.

This document is the carry-over.

---

## 5. Carry-overs for Phase 24+

| ID | Item | Where | Priority |
|----|------|-------|----------|
| **R-P23-CACHE-REHYDRATE** | Implement candidates_reranked → tool_results rehydration | `orchestrator.py:3979` + `retrieval_cache.py` | **Very high** — silently breaks every same-query repeat for users |
| **R-P23-VLLM-400** | Cap per-entity payload + add LLM-call try/except so `response` is always assigned | `orchestrator.py:4491` + `_build_retrieval_summary` | **High** — UnboundLocalError cascade |
| **R-P19-DOC** | NI 43-101 chunk seed for gq-026 | `gold.documents` | High |
| **R-P22-GRAPH-FORMATION** | gq-013 (CGL/GPT) — needs both fixes above, plus prompt tweak | mixed | High |
| **R-P14-3.6** | Test assertion relaxations | tests | Medium |
| **R-P19-POPULATE** | Fix populate_neo4j Report.title uniqueness | populate script | Medium |
| **R-P15-1** | Bundled orchestrator prompts migration | orchestrator | Medium |

---

## 6. Files of record

This phase added no code. Only this investigation doc + its
verifier + sweep:

```
docs/phase23_cache_rehydration_investigation.md                    (this file)
docs/phase23_handoff.md                                            (handoff pointer)
scripts/phase23_master_sweep.sh                                    (sweep)
scripts/phase23_step1_verify.sh                                    (verifier)
```

End of Phase 23 investigation.
