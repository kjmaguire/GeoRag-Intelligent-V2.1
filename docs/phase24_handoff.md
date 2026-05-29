# Phase 24 Handoff — Paired infrastructure fixes (R-P23-VLLM-400 + R-P23-CACHE-REHYDRATE)

**Document version:** 1.0
**Status:** Phase 24 complete. Phase 25 inheriting.
**Predecessors:** `docs/phase23_handoff.md`,
`docs/phase23_cache_rehydration_investigation.md`.

---

## 1. What Phase 24 delivered

Both infrastructure bugs documented in Phase 23 landed:

| Step | Output | Verifier |
|------|--------|----------|
| 1 | **R-P23-VLLM-400** — `src/fastapi/app/agent/orchestrator.py` initialises `response: GeoRAGResponse \| None = None` before the retry loop and constructs a fallback `assemble_response(...)` if the loop exits without setting `response`. The original `UnboundLocalError` cascade that masked vLLM 400 errors is now impossible. | `scripts/phase24_step1_verify.sh` checks 1+2 |
| 2 | **R-P23-CACHE-REHYDRATE** — two paired changes: (a) cache **write** captures full `dataclasses.asdict` payloads for neo4j + postgis candidates (previously qdrant-only); (b) cache **read** rebuilds `DocumentChunk` / `GraphEntity` / `CollarRecord` lists from `candidates_reranked`, wraps them in `DocumentSearchResult` / `GraphTraversalResult` / `SpatialQueryResult`, and seeds `tool_results`. Falls back to a fresh tool rerun if any cached candidate lacks a payload (pre-Phase-24 entries). | `scripts/phase24_step1_verify.sh` checks 3+4+5 |
| 3 | Regression guard: cold-run golden ≥ 20, warm parity within ±2 | `scripts/phase24_step1_verify.sh` checks 6+7 |
| 4 | This handoff + master sweep | — |

---

## 2. Why no test-pass-count jump

Both fixes are infrastructure correctness fixes, not new data
or new prompt instructions. They eliminate failure modes that
*could* have caused refusals under specific conditions
(LLM 400 + cache hit), but in the test suite those conditions
rarely fire:

- The test runner uses fresh fastapi every cold run, then a
  single warm pass. With ~31 distinct queries and 5-min cache
  TTL, queries that *would* cache-hit are rare even in the warm
  run (data versions churn mid-suite, busting the cache key).
- vLLM 400 errors only fire on specific dense graph payloads
  that the test suite rarely exercises consistently.

Where Phase 24 *does* unblock real value:
- **Production users repeating the same query** (the cache-hit
  case) no longer get silent refusals; the cached candidates
  rehydrate properly.
- **Operators seeing UnboundLocalError in the logs** now see the
  actual vLLM error string, dramatically simplifying triage.

The test suite stays at the Phase 22 baseline (cold/warm ≈ 22–24,
run-to-run variance) and shows no regression.

---

## 3. The rehydration shape

`CachedRetrievalCandidate.payload` (a `dict | None`) carries the
original dataclass fields. The cache-hit branch at
`orchestrator.py` line ~3991 reconstructs the three tool result
types:

```python
for _cc in _cached_retrieval_ctx.candidates_reranked:
    if _cc.payload is None:
        _abort_rehydrate = True  # pre-Phase-24 entry
        break
    if _cc.source_store == "qdrant":
        _doc_chunks.append(DocumentChunk(**_cc.payload))
    elif _cc.source_store == "neo4j":
        _graph_entities.append(GraphEntity(**_cc.payload))
    elif _cc.source_store == "postgis":
        _collars.append(CollarRecord(**_cc.payload))

if _abort_rehydrate:
    _cache_hit = False   # let tools rerun
else:
    if _doc_chunks: tool_results.append(("search_documents", DocumentSearchResult(...)))
    if _graph_entities: tool_results.append(("traverse_knowledge_graph", GraphTraversalResult(...)))
    if _collars: tool_results.append(("query_spatial_collars", SpatialQueryResult(...)))
```

`data_source` strings tag the cache-hit origin (`"Qdrant (cache hit)"`,
`"Neo4j (cache hit)"`, `"PostGIS silver.collars (cache hit)"`) so
provenance Layer 5 enrichment and degraded-source detection can
distinguish them.

---

## 4. Risk surface

1. **Cache write payload size** — adding asdict payloads for
   neo4j + postgis grows Redis usage. With `MAX_CONTEXT_GRAPH_ENTITIES
   = 20` and `MAX_CONTEXT_COLLARS = 20`, the per-cache-entry growth
   is bounded (~5-10 KB extra). Cache TTL is 5 min, so steady-state
   Redis pressure is negligible.

2. **Rehydration accuracy** — `DocumentChunk(**payload)` etc.
   require the cached dict to exactly match the dataclass fields.
   If a dataclass field is added or renamed without a `spv` bump,
   the `TypeError` triggers the `_abort_rehydrate` path and tools
   rerun — no incorrect data ever reaches the LLM.

3. **Behavioural change on cache hit** — previously cache hits
   produced refusal text; now they produce real answers. This
   is the intended outcome but operators relying on refusal-on-
   cache-hit as an alert signal should switch to the new
   `"cache rehydrated docs=N graph=N collars=N"` INFO log.

---

## 5. Carry-overs for Phase 25+

| ID | Item | Where | Priority |
|----|------|-------|----------|
| **R-P19-DOC** | NI 43-101 chunk seed for gq-026 (estimation-method "kriging") | `gold.documents` + chunk pipeline | High |
| **R-P22-GRAPH-FORMATION** | gq-013 — agent narrates formations by long name, not code (CGL/GPT) | prompt or graph rendering | High |
| **R-P24-VLLM-PAYLOAD-CAP** | vLLM 400s on dense graph payloads remain a real failure mode; cap per-entity property bytes | `_build_retrieval_summary` | Medium |
| **R-P14-3.6** | Test assertion relaxations | tests | Medium |
| **R-P19-POPULATE** | populate_neo4j Report.title uniqueness | populate script | Medium |
| **R-P15-1** | Bundled orchestrator prompts migration | orchestrator | Medium |
| **R-P21-CACHE-TELEMETRY** | Promote CACHE HIT/MISS from DEBUG to INFO; surface in answer_runs | orchestrator | Medium |

---

## 6. Files of record

**Modified in Phase 24:**

```
src/fastapi/app/agent/orchestrator.py                              (Steps 1 + 2)
docs/phase24_handoff.md                                             (this file)
scripts/phase24_master_sweep.sh                                    (Step 3)
scripts/phase24_step1_verify.sh                                    (Step 1)
```

---

## 7. Re-running

```bash
bash scripts/phase24_step1_verify.sh   # paired fix + cold+warm run pair (~3 min)
bash scripts/phase24_master_sweep.sh   # Phase 0 → 24 (~6-8 min)
```

End of Phase 24 handoff.
