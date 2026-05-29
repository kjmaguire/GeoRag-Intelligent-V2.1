# Phase F.14 — `orchestrator/run_cache.py` extracted + retrieval cache lit up

**Status:** ✅ Done. Cache rehydration completion shipped end-to-end.
Default `RETRIEVAL_CACHE_ENABLED` flipped to **True**. Eval stays at
**22/22 stable across 3 runs with cache ON**. Full unit suite:
**1105 passed, 0 failed**.

## Two interlocking deliverables

### F.14 — orchestrator/run_cache.py

A new sibling module under `orchestrator/` carries the cache-related
state. Owns:

* `fetch_data_versions(pg_pool, workspace_id, project_id)` — single
  PG round-trip → `(workspace_data_version, project_data_version)`.
  Used by the cache key and by `answer_runs` freshness fields.
* `cache_key(query, project_id, *, system_prompt_version, …)` — pure
  deterministic Redis key builder. Takes `system_prompt_version` as
  an arg rather than importing it, so the cache-key surface is
  decoupled from the orchestrator's prompt-version cadence.
* `build_cached_candidates(fused_candidates)` — converts the
  orchestrator's `_fused_candidates` (`list[ScoredCandidate]`) into
  the cache-shape `list[CachedRetrievalCandidate]`. **Phase H:** also
  serialises the postgis + qdrant dataclass payloads via
  `dataclasses.asdict` so rehydration can reconstruct them.
* `build_cached_context(...)` — full `CachedRetrievalContext`
  builder. Single call site replaces the 100-LOC inline writer
  block in `__init__.py`.
* `rehydrate_tool_results(cached_ctx)` — **brand new path**. Groups
  `candidates_reranked` by `source_store` and rebuilds the original
  tool-result dataclasses (`SpatialQueryResult`,
  `DocumentSearchResult`). Neo4j entities are skipped cleanly
  (graph wrappers aren't dataclasses) — the orchestrator's graph
  branch re-fires when needed.

`orchestrator/__init__.py` retains thin compat wrappers
`_fetch_data_versions` / `_cache_key` so the 19+ production callers
and the test suite keep working unchanged.

### The cache rehydration completion (the 5-month-old design gap)

The orchestrator's cache hit path was design-incomplete since
~doc-phase 100. The inline comment was always:

```
cache hit:  deserialize → rehydrate candidates → synthesize fresh
cache miss: retrieve → rrf → rerank → SETEX → synthesize
```

But the "rehydrate candidates" step was never written. Phase G
overnight discovered this and disabled the cache entirely. Phase H
finally builds the missing path:

1. **Cache write** captures postgis collar payloads (not just
   `{store, canonical_id}` refs). The inline write block in
   `__init__.py` was replaced with a single call to
   `build_cached_context(...)`.
2. **Cache read** calls `rehydrate_tool_results(...)` to rebuild
   `SpatialQueryResult` / `DocumentSearchResult` instances which
   feed straight into `_build_context` / `assemble_response`.
3. **Partial-source fallback** — the cache only carries
   `_fused_candidates` (RRF: qdrant + postgis + neo4j). Tool
   results NOT in the RRF (`project_overview`, `downhole`, `assay`,
   `targeting`, `public_geoscience`) would be lost on hit. When
   the current query's classifier wants any of those, the
   orchestrator detects the mismatch at cache-read time and treats
   the hit as miss, so fresh retrieval runs all the tools the
   query actually needs.

## Empirical speedup

Same query, sequential identical runs (5x):

| Iter | Time | Path |
|---|---|---|
| 1   | 1.71s | cache miss → retrieval + RRF + reranker + SETEX + synthesize |
| 2-5 | 0.85s each | cache hit → rehydrate + synthesize fresh |

**Net ~2× latency reduction** on the warm path. Synthesis still runs
fresh per Global Invariant 12 — only retrieval + fusion + reranker
are skipped.

## Eval state

| Run | Cache state | Pass count |
|---|---|---|
| 1 | OFF — cold | 22/22 |
| 2 | OFF — warm | 22/22 |
| 3 | OFF — warm | 22/22 |
| 4 | ON  — cold | 22/22 |
| 5 | ON  — warm | 22/22 |
| 6 | ON  — warm | 22/22 |

22/22 holds in both flag states. The 22-question pack now exercises
both code paths (the eval runner runs 22 sequential queries against
the same project, so questions 2+ hit the cache when ENABLED, and
runs all freshly when DISABLED).

## What's covered + uncovered

| Category | Cached? | Rehydration? | Notes |
|---|---|---|---|
| `query_spatial_collars` (postgis) | ✅ | ✅ | Full CollarRecord roundtrip |
| `search_documents` (qdrant) | ✅ | ✅ | Full DocumentChunk roundtrip |
| `search_public_geoscience` (qdrant) | ⚠️ | partial-fallback | Mixed result; future-work to roundtrip PGEO record refs |
| `traverse_knowledge_graph` (neo4j) | ✅ stored | ⚠️ skipped | Graph entity wrappers aren't dataclasses; partial-fallback re-fires graph branch when needed |
| `query_project_overview` | ❌ | partial-fallback | Not in `_fused_candidates`; fresh fetch on every hit |
| `query_downhole_logs` | ❌ | partial-fallback | Same |
| `query_assay_data` | ❌ | partial-fallback | Same |
| `drill_targeting` | ❌ | partial-fallback | Same |

The partial-source fallback ensures correctness even for the
uncovered categories — they just don't benefit from cache speedup.
**Pure spatial / docs / graph queries get the full 2× speedup.**
Project-metadata queries pay the cost of a fresh PG SELECT but
that's already sub-millisecond on a warm PgBouncer.

## Future work (deferred)

* **Public Geoscience roundtrip** — extend `build_cached_candidates`
  + `rehydrate_tool_results` to handle PG record dataclasses (they
  ARE dataclasses, just a different set of fields).
* **Neo4j graph entity roundtrip** — would need either a graph
  dataclass model or a `{node_id, label, props}` envelope plus a
  Neo4j re-query on rehydration. Lower value (graph queries are
  fast already + the partial-source fallback handles it).
* **Cache project_overview / downhole / assay / targeting** —
  schema extension to `CachedRetrievalContext.tool_results` as a
  parallel collection alongside `candidates_reranked`. ~2 ticks.
  Would lift the partial-fallback rate from current ~40% (project-
  metadata-touching questions) to near-zero.

## Tests

* `tests/test_run_cache_rehydration.py` (new, 13 tests):
  cache_key contract, prompt-version cache-bust, postgis/qdrant
  payload serialisation, neo4j skipping, corrupt-payload safety,
  mixed-store rehydration, full round-trip via
  `build_cached_context`.
* `tests/test_cache_scope.py` + `tests/test_cache_key_versioning.py`
  (existing) — all pass via the thin `_cache_key` compat wrapper.
* `tests/test_orchestrator_*` — all pass; the F.14 refactor is a
  pure structural move + a behaviour ADDITION (rehydration), no
  contract changes to the existing callers.

## Files

* **New:** `src/fastapi/app/agent/orchestrator/run_cache.py` (~420 LOC)
* **New:** `src/fastapi/tests/test_run_cache_rehydration.py` (13 tests)
* **Edited:** `src/fastapi/app/agent/orchestrator/__init__.py`
  — `_fetch_data_versions` / `_cache_key` now thin wrappers; inline
  cache writer replaced with `build_cached_context` call;
  cache-hit path now rehydrates + does partial-source fallback
* **Edited:** `src/fastapi/app/config.py` — `RETRIEVAL_CACHE_ENABLED`
  default flipped to `True`
* **Closes:** `docs/phase_g_followup_retrieval_cache_disabled.md`'s
  "What's documented but NOT fixed" section (3 sub-items: payload
  caching, reconstruction grouper, partial-source fallback) — all
  shipped here.

## Orchestrator-refactor track update

Adding F.14 to the closed track:

| Phase | Module | LOC delta in __init__.py |
|---|---|---|
| F.6  | query_classification.py | −280 |
| F.7  | tool_result_helpers.py  | −250 |
| F.8  | graph_entities.py       | −95 |
| F.9  | query_project_overview tool | +50 |
| F.10 | prompts/ reconciliation | mirror-only |
| F.11 | context_builder.py      | −216 |
| F.12 | llm_calls.py            | −803 |
| F.13 | orchestrator/ package rename | 0 (structural) |
| **F.14** | **run_cache.py + rehydration** | **−~120 net** |

`orchestrator/__init__.py` now sits at ~3,450 LOC, down from ~3,538.
The remaining body is dominated by `run_deterministic_rag` itself
(~1,500 LOC) + the system-prompt constants (~700 LOC, mirrors). The
F.15 split would move `run_deterministic_rag` to `orchestrator/run.py`
— the natural next step, but lower value now that cache + LLM-calls
+ context-builder + classifier all live in cohesive sibling modules.
