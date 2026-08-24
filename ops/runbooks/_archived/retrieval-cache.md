# Retrieval Cache Runbook

Documents the v5 RAG cache key structure, TTL, invalidation paths, and inspection commands. Use this when debugging stale answers, after an ingestion run, or when bumping retrieval strategy.

**Cache scope (Phase B addendum 2026-04-21):** Only `CachedRetrievalContext` is cached — never the synthesized `GeoRAGResponse`. Synthesis runs fresh on every query. This is spec-compliant per arch §05c and the Global Invariant on answer-level caching.

---

## Key structure

```
georag:rag_cache:v6:<sha256_first16_of_json_inputs>
```

The 16-character hex suffix is the first 16 chars of `sha256(json.dumps(inputs, sort_keys=True))` where `inputs` is:

```json
{
  "q":    "<normalised_query>",
  "wid":  "<workspace_id_or_empty_string>",
  "pid":  "<project_id>",
  "wdv":  <workspace_data_version>,
  "pdv":  "<project_data_version_or_empty_string>",
  "rsv":  "<RETRIEVAL_STRATEGY_VERSION>",
  "spv":  <_SYSTEM_PROMPT_VERSION>,
  "fh":   "",
  "rh":   "",
  "cats": { ...routing_categories... }
}
```

The full key is built in `_cache_key()` in `src/fastapi/app/agent/orchestrator.py`.

**Key prefix history:**
- `v3` — DOCUMENT_SCOPE_VERSION (static int) in hash
- `v4` — live data_version values in hash (Chunk 1, 2026-04-21). Stored full GeoRAGResponse — spec violation per §05c.
- `v5` — live data_version values in hash + retrieval-only cache scope (Phase B addendum, 2026-04-21). Stores CachedRetrievalContext only — spec-compliant.
- `v6` — added `spv` (`_SYSTEM_PROMPT_VERSION`) as explicit key slot (PV-02 fix, cross-module cleanup sweep 2026-04-21). Any prompt edit that increments `_SYSTEM_PROMPT_VERSION` now automatically busts retrieval cache without requiring a `RETRIEVAL_STRATEGY_VERSION` bump.

Old v5 keys are unreachable under the v6 prefix. They TTL out naturally within 5 minutes. Do NOT flush them manually.

---

## What is cached

A `CachedRetrievalContext` JSON object. It contains:
- Retrieval metadata: `query_class`, `sparse_boost_applied`, `fusion_method`, `retrieval_strategy_version`, model versions
- Data version fingerprints: `workspace_data_version_at_cache`, `project_data_version_at_cache`
- `candidates_reranked`: the final top-N candidates after RRF fusion and cross-encoder reranking — one `CachedRetrievalCandidate` per chunk with text, scores, and provenance pointers
- `partial_failure_details`: which stores failed during the original retrieval fan-out
- `original_answer_run_id`: set by a post-INSERT Redis update after the originating `answer_runs` row is written — used to populate `cache_hit_of_run_id` on subsequent cache-hit runs

**What is NOT cached:**
- Synthesized answer text (`text`)
- Citations (`citations`, `citation_lifecycle_state`)
- LLM metadata (`input_tokens`, `output_tokens`, `backend_used`)
- `map_payload`, `viz_payload`, `followups`
- `confidence`

---

## Why retrieval-only?

Per the Global Invariant and arch §05c — fresh synthesis on every query ensures:
1. **Citation lifecycle state** is computed live. Stale cached citations could reference chunks that have since been revised or retracted.
2. **Refusal decisions** stay current. The LLM may decline to answer on a cached retrieval if new safety guidance applies.
3. **LLM observability is honest.** Every query generates a new `answer_runs` row with its own `answer_run_id`. A cached full response would produce no new row, making cost and quality metrics unreliable.
4. **Hallucination prevention layers 2-6** all run on every synthesis call, not just on cache misses.

The retrieval context is the expensive deterministic part (parallel fan-out to PostGIS + Qdrant + Neo4j, RRF fusion, cross-encoder reranking). Synthesis is cheaper and must stay live.

---

## Why each component of the key

| Component | Key | Reason |
|---|---|---|
| Normalised query | `q` | Same text -> same retrieval path. Normalised: lowercased, stripped. |
| Workspace ID | `wid` | Multi-tenant isolation. Currently empty-string sentinel (Module 9 will plumb workspace_id through JWT). |
| Project ID | `pid` | Scopes to a project's data. |
| Workspace data_version | `wdv` | Live integer from `silver.workspaces.data_version`. Dagster ingestion bumps this version -> automatic cache miss. |
| Project data_version | `pdv` | Live integer from `silver.projects.data_version`. Empty string when query is not project-scoped. |
| Retrieval strategy version | `rsv` | String constant from `query_classifier.py` (currently `v3.1-think-off-2026-04-21`). Bumped when any behavioral retrieval change ships. |
| System prompt version | `spv` | Integer constant `_SYSTEM_PROMPT_VERSION` from `orchestrator.py` (currently 8). Added in v6 (PV-02 fix, 2026-04-21) — any prompt bump now automatically busts the retrieval cache without requiring a separate `RETRIEVAL_STRATEGY_VERSION` bump. |
| Filters hash | `fh` | Empty string today. Reserved for user-applied facet filters (Module 9). |
| RBAC scope hash | `rh` | Empty string today. Reserved for per-user access scoping (Module 9). |
| Routing categories | `cats` | The internal routing bucket dict from `_classify_query()`. |

---

## TTL

**5 minutes** (`SETEX key 300 value`).

The `data_version` bump mechanism makes TTL effectively zero for post-ingestion staleness: a new version produces a new key, so the old entry is unreachable regardless of its remaining TTL. Old entries expire naturally — no explicit DEL is required after a version bump.

---

## Invalidation paths

### Natural — TTL expiry

Entries expire after 300 seconds. No action required.

### Ingestion-commit — data_version bump

When a Dagster ingestion run completes and calls `commit_ingestion_run`, it increments `silver.workspaces.data_version` and/or `silver.projects.data_version`. The next query builds a new cache key -> cache miss -> fresh retrieval.

### Deliberate — strategy change

Bump `RETRIEVAL_STRATEGY_VERSION` in `src/fastapi/app/services/query_classifier.py`. All keys with the old `rsv` value become unreachable.

### Manual — force flush

Use only when debugging or when a bad entry was cached and you need it gone immediately:

```bash
# Count matching keys first:
docker exec georag-redis redis-cli -a $REDIS_PASSWORD KEYS "georag:rag_cache:v6:*" | wc -l

# Delete all v5 cache entries:
docker exec georag-redis redis-cli -a $REDIS_PASSWORD KEYS "georag:rag_cache:v6:*" \
  | xargs -r docker exec -i georag-redis redis-cli -a $REDIS_PASSWORD DEL
```

This is safe to run at any time — it only causes cache misses on subsequent queries, not errors.

---

## Inspecting cache state

### Inspect a specific key value

```bash
docker exec georag-redis redis-cli -a $REDIS_PASSWORD GET "georag:rag_cache:v6:<16-char-hex>" | python3 -m json.tool
```

The value is a JSON-serialised `CachedRetrievalContext`. Verify it contains:
- `schema_version` (integer, currently 1)
- `candidates_reranked` (list of CachedRetrievalCandidate)
- No `text`, no `citations`, no `answer_text` — if these appear, something is wrong

### Sample active cache keys

```bash
docker exec georag-redis redis-cli -a $REDIS_PASSWORD KEYS "georag:rag_cache:v6:*" | head -10
```

### Remaining TTL on a key

```bash
docker exec georag-redis redis-cli -a $REDIS_PASSWORD TTL "georag:rag_cache:v6:<16-char-hex>"
```

Returns seconds remaining. `-1` means no expiry set (should not happen). `-2` means key does not exist.

### Hit/miss ratio

```bash
docker exec georag-redis redis-cli -a $REDIS_PASSWORD INFO stats \
  | grep -E "keyspace_hits|keyspace_misses"
```

---

## Cache hit bookkeeping

On every query (cache hit or miss) a new `silver.answer_runs` row is written. This ensures LLM observability is accurate regardless of cache state.

On cache hit, the new `answer_runs` row has `cache_hit_of_run_id` set to the `answer_run_id` of the original run whose retrieval was cached (populated from the cached context's `original_answer_run_id` field). This enables audit queries:

```sql
-- Find all runs that reused a cached retrieval
SELECT answer_run_id, cache_hit_of_run_id, query_text, created_at
FROM silver.answer_runs
WHERE cache_hit_of_run_id IS NOT NULL
ORDER BY created_at DESC
LIMIT 20;

-- For a given original run, find all runs that reused its retrieval
SELECT answer_run_id, query_text, created_at
FROM silver.answer_runs
WHERE cache_hit_of_run_id = '<original-answer-run-id>'::uuid;
```

---

## Cache hit rate expectations

| Scenario | Expected hit rate |
|---|---|
| Active session, same user re-querying | 40-70% |
| Multiple users, diverse geological queries | 10-30% |
| Immediately after ingestion commit (data_version bump) | Near 0% until re-warm |
| After `RETRIEVAL_STRATEGY_VERSION` bump | Near 0% until re-warm |
| Development / unit testing with forced cache miss | 0% (intentional) |

---

*Written 2026-04-21 during Module 4 Phase D. Updated 2026-04-21 for Phase B addendum cache-scope fix (v4 -> v5, GeoRAGResponse -> CachedRetrievalContext). Updated 2026-04-21 for cross-module cleanup sweep PV-02 (v5 -> v6, added `spv` = `_SYSTEM_PROMPT_VERSION` slot).*
