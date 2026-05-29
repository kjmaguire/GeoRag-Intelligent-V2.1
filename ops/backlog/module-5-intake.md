# Module 5 (LLM Synthesis) — pre-approved intake items

Items flagged during Module 4 work that are pre-approved for Module 5 execution.

---

## Retrieval-only caching is live as of 2026-04-21

**Context:** Module 4 Phase B addendum (cache-scope fix 2026-04-21) moved the
Redis cache boundary from `GeoRAGResponse` (answer-level — spec violation) to
`CachedRetrievalContext` (retrieval-only — spec-compliant per §05c).

**Impact on Module 5:** Synthesis runs on every query, including cache hits.
The evidence set (candidates_reranked) is identical whether the retrieval was
fresh or rehydrated from cache. Module 5 synthesis optimizations apply to ALL
queries.

**Module 5 owns these fields in `answer_runs`:**
- `evidence_truncated_count` — number of candidates dropped due to context-window budget
- `backend_chain` — ordered list of backends attempted (DeepSeek -> Claude fallback, etc.)
- `model_name`, `input_tokens`, `output_tokens` — LLM observability
- `cache_read_tokens`, `cache_creation_tokens` — Anthropic prompt-cache token tracking

**Module 5 owns `answer_run_id` return path:**
Use the `answer_run_id` returned from `insert_answer_run()` in orchestrator to
link synthesis metadata back to the retrieval row. The `answer_run_id` is
returned from `run_deterministic_rag()` as part of the GeoRAGResponse so
downstream consumers can reference the specific run.

**Prompt caching (`_SYSTEM_PROMPT_VERSION`):** Module 5 scope. The system prompt
version must be included in the Anthropic cache_control blocks. It is independent
of `RETRIEVAL_STRATEGY_VERSION` — prompt changes bump `_SYSTEM_PROMPT_VERSION`,
not the retrieval strategy version.

**Speculative decoding:** Module 5 scope for vLLM backend. `speculative_acceptance_rate_sample`
field in `answer_runs` is reserved for this.

**cache_hit_of_run_id linkage:** The `original_answer_run_id` field inside
`CachedRetrievalContext` is populated by a post-INSERT Redis update on the
cache-miss path (see orchestrator.py). On cache hit, `cache_hit_of_run_id` is
set on the new `answer_runs` row. Module 5 should verify this linkage works
end-to-end in smoke tests.

---

## LLM fallback chain tracking

Module 5 must populate `answer_runs.backend_chain` with the ordered list of
backends attempted. Example: `["ollama", "anthropic"]` when the primary Ollama
backend times out and Claude API is used as fallback.

The fallback is triggered by: connection error, timeout, or 5xx response from
the primary. Config fields: `LLM_PRIMARY_URL`, `LLM_FALLBACK_URL`,
`LLM_FALLBACK_ENABLED` (per §08 LLM & AI Chat Architecture).

---

*Created 2026-04-21 during Module 4 Phase B addendum close-out.*
