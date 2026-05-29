# Retrieval Pipeline Runbook

Traces the full path from user query to ranked context, including timeouts, partial-failure handling, and the observability writes that close out every run. Use this to understand how retrieval works and to triage poor answers.

---

## Flow at a glance

```
POST /internal/queries
  └─ run_deterministic_rag()
       1. Keyword classify → _classify_query()      (internal routing buckets)
       2. Spec-class label → classify_query()        (answer_runs.query_class)
       3. Identifier detect → detect_identifiers()   (sparse_boost_factor)
       4. Fetch data_version (single PG round-trip)  (freshness authority)
       5. Build v4 cache key                          (sha256 of 8 components)
       6. Redis GET → HIT: return cached → MISS: continue
       7. Parallel fan-out (asyncio.gather, return_exceptions=True):
            a. query_spatial_collars  → PostGIS silver.collars   (5.0s timeout)
            b. search_documents       → Qdrant hybrid georag_reports (2.0s timeout)
            c. search_public_geoscience → Qdrant pg_* + PostGIS  (2.0s timeout)
          + Sequential (post-gather, per routing flags):
            d. query_downhole_logs    → PostGIS silver.lithology_logs
            e. query_assay_data       → PostGIS silver.samples
            f. traverse_knowledge_graph / query_graph_by_label → Neo4j
       8. Cross-store RRF → rrf_fuse() from fusion.py
       9. Cross-encoder rerank (BGE) → top-k per query class
      10. Context build → _build_context()
      11. LLM synthesis → single _call_llm() call
      12. Hallucination layers 2–6 (validation, provenance, constraints)
      13. Observability writes (fire-and-forget, never fail the query):
            INSERT silver.answer_runs        (one row)
            INSERT silver.answer_retrieval_items  ('retrieved' + 'reranked' stages)
      14. Redis SETEX (TTL 300s, v4 key)
      15. Return GeoRAGResponse
```

---

## Which store per query class

| Spec class | Qdrant (georag_reports) | Qdrant (pg_*) | Neo4j | PostGIS collars | PostGIS samples | PostGIS litho |
|---|---|---|---|---|---|---|
| `factual` | YES | YES | YES | YES | — | — |
| `spatial` | YES | YES | — | YES | — | — |
| `document` | YES | YES | — | — | — | — |
| `computation` | YES | YES | — | — | YES | — |
| `viz` | YES | YES | — | YES | — | — |
| `unknown` | YES | YES | YES | YES | — | — |

**Notes:**

- The spec-class label (from `classify_query()`) controls the reranker top-k and answer_runs metadata. The actual store dispatch is driven by the internal routing buckets from `_classify_query()` in orchestrator.py. These are two separate classifiers — both run on every query.
- `factual` routes to the same stores as `unknown`; they differ only in reranker top-k (both 20).
- `downhole` and `assay` tools fire sequentially after the parallel gather when `_classify_query()` sets those flags (e.g. "lithology" keywords → `downhole=True`). They are not yet parallel with the main gather.
- Public geoscience (`search_public_geoscience`) fires whenever `public_geoscience=True` in the routing dict, regardless of spec class.

---

## Per-store timeouts

| Store | Timeout | Source constant | On timeout |
|---|---|---|---|
| PostGIS (collars) | 5.0 s | `TIMEOUT_POSTGIS_S` | `asyncio.TimeoutError` captured by `return_exceptions=True`; exception class logged to `partial_failure_details`; RRF continues with remaining stores |
| Qdrant (documents) | 2.0 s | `TIMEOUT_QDRANT_S` | Same — partial rescue path, RRF uses what returned |
| Qdrant (public geo) | 2.0 s | `TIMEOUT_QDRANT_S` | Same |
| Neo4j (graph) | 2.0 s | `TIMEOUT_NEO4J_S` | Same |
| Reranker (BGE CPU) | 2.0 s | `RERANKER_TIMEOUT_S` in `reranker.py` | Log + continue with RRF-ordered results; `reranker_version` still written to answer_runs |

Timeouts are enforced with `asyncio.wait_for()` wrapping each branch of the gather. Any timed-out branch contributes an empty candidate list to RRF. The partial failure dict is stored as JSONB in `silver.answer_runs.partial_failure_details` — `{"qdrant": "TimeoutError"}` for example. NULL means all stores responded.

Overall guard: no explicit global deadline today. The sum of parallel fan-out (5.0s PostGIS dominates) + sequential branches + reranker + LLM is the practical bound. A global 8s deadline is documented in the architecture spec (Section 06) but is not yet wired as a wrapping `asyncio.wait_for` — Phase C item.

---

## Debugging a poor retrieval

**Step 1 — Get the answer_run_id.**

It is logged at INFO level: `insert_answer_run: inserted answer_run_id=<uuid>`. Also available in the `answer_run_id` field of the JSON response (if the caller surfaces it).

```bash
docker exec georag-fastapi grep "inserted answer_run_id" /var/log/georag/app.log | tail -5
```

**Step 2 — Inspect the answer_run row.**

```sql
SELECT
    answer_run_id,
    query_text,
    query_class,
    sparse_boost_applied,
    reranker_version,
    retrieval_strategy_version,
    workspace_data_version_at_query,
    project_data_version_at_query,
    partial_failure_details,
    created_at
FROM silver.answer_runs
WHERE answer_run_id = '<uuid>';
```

Check:
- `query_class` — is it what you expect? Wrong class means wrong store dispatch and wrong reranker top-k.
- `sparse_boost_applied` — FALSE means no geological identifier was detected; sparse prefetch stayed at 100.
- `partial_failure_details` — non-NULL means at least one store timed out or errored. Check which store.
- `workspace_data_version_at_query` / `project_data_version_at_query` — if these don't match current DB values, the cache was served from a stale version; force-invalidate (see `retrieval-cache.md`).

**Step 3 — Inspect retrieval items per stage.**

```sql
SELECT
    stage,
    source_store,
    rrf_rank,
    rrf_score,
    retriever_score,
    reranker_score,
    included_in_context,
    used_in_citation,
    candidate_ref
FROM silver.answer_retrieval_items
WHERE answer_run_id = '<uuid>'
ORDER BY stage, rrf_rank NULLS LAST;
```

- `retrieved` rows: everything that came back from stores before reranking. RRF rank 1 = highest fused score.
- `reranked` rows: survivors after cross-encoder. Compare their `reranker_score` to the `retrieved` `rrf_rank` — did the reranker substantially reorder? Low `reranker_score` on top candidates means the query's semantic match to chunks is weak.
- If `included_in_context = false` on all rows, context packing dropped everything (rare — means top reranked chunk scored below the relevance gate).

**Step 4 — Compare retrieved vs reranked ordering.**

```sql
SELECT
    r.rrf_rank     AS rrf_rank,
    r.retriever_score,
    rk.reranker_score,
    r.source_store,
    r.candidate_ref
FROM silver.answer_retrieval_items r
LEFT JOIN silver.answer_retrieval_items rk
    ON rk.answer_run_id = r.answer_run_id
    AND rk.stage = 'reranked'
    AND rk.passage_id = r.passage_id
WHERE r.answer_run_id = '<uuid>'
  AND r.stage = 'retrieved'
ORDER BY r.rrf_rank;
```

If the reranker column is NULL for most rows, the reranker rejected them (below top-k for that query class) or did not fire (timeout). Check `partial_failure_details` for reranker timeout.

**Step 5 — Check Redis for a stale cached answer.**

```bash
# Get the cache key from logs (logged at DEBUG as "CACHE HIT key=...")
# Or re-derive by re-issuing the same query and watching logs.
docker exec georag-redis redis-cli -a $REDIS_PASSWORD GET "georag:rag_cache:v4:<key>"
```

If a hit is served for a query you just re-submitted, the data_version in the key hasn't changed since last ingestion. Force-invalidate (see `retrieval-cache.md`) and re-query.

---

## Replay a query

To reproduce retrieval exactly (same classifier, same encoder versions, same data_version):

```bash
# Verify current retrieval_strategy_version:
docker exec georag-fastapi python -c \
  "from app.services.query_classifier import RETRIEVAL_STRATEGY_VERSION; print(RETRIEVAL_STRATEGY_VERSION)"

# Verify current data_versions:
docker exec georag-postgres psql -U georag -d georag \
  -c "SELECT workspace_id, data_version FROM silver.workspaces;"
docker exec georag-postgres psql -U georag -d georag \
  -c "SELECT project_id, data_version FROM silver.projects;"

# Force a cache miss by adding a trailing space to the query (changes the hash),
# or flush the cache for the workspace (see retrieval-cache.md).

# Re-issue via the internal API:
curl -s -X POST http://localhost:8000/internal/queries \
  -H "Content-Type: application/json" \
  -H "X-Internal-Key: $FASTAPI_INTERNAL_KEY" \
  -d '{"query": "...", "project_id": "<uuid>", "stream": false}' | jq .
```

The query will use the same model revisions pinned in the service modules (SPLADE `49cf4c7b`, BGE reranker `5ccf1b81`). If you need to reproduce a historical run from a different `retrieval_strategy_version`, you must check out the commit where that version was active and rebuild the container.

---

## Version-bump side effects

Bumping `RETRIEVAL_STRATEGY_VERSION` in `src/fastapi/app/services/query_classifier.py` changes the `rsv` component of the v4 cache key. Every cached answer for every workspace and project becomes a miss. This is intentional: new retrieval behavior should not serve stale cached answers derived under the old behavior.

When to bump:
- SPLADE model revision changes (new `SPARSE_MODEL_REVISION` in `sparse_encoder.py`)
- Dense embedding model changes
- Reranker model revision changes
- RRF k value changes in `fusion.py`
- New query class added to `query_classifier.py`
- Substantive change to identifier-boost regex patterns

When NOT to bump:
- Bug fix that doesn't change retrieval behavior (e.g., a timeout value adjustment)
- Changes to LLM synthesis, hallucination layers, or citation assembly

Format: `v1-hybrid-YYYY-MM-DD` (current: `v1-hybrid-2026-04-21`).

---

*Written 2026-04-21 during Module 4 Phase D. Update whenever the underlying procedure changes.*
