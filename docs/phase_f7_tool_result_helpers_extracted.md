# Phase F.7 — Orchestrator refactor, module 2: `tool_result_helpers.py`

**Status:** Complete. Pattern from F.6 reused without modification.

## What moved

New module `src/fastapi/app/agent/tool_result_helpers.py` (255 LOC)
holds the four pure helpers that operate on tool-result dataclasses:

* `_build_collar_aggregates` — pre-computes deepest / shallowest /
  averages / spatial extents over `SpatialQueryResult.collars`
* `_mmr_select_chunks` — Max-Marginal-Relevance dedupe over
  `DocumentChunk` payloads
* `_is_empty_tool_result` — the Phase F.4 helper that drops zero-row
  tool results from `tool_results` before citation assignment
* `_build_retrieval_summary` — formats the per-store retrieval status
  line for the phase checklist

`orchestrator.py` now re-exports all four. Any caller that imported
`from app.agent.orchestrator import _is_empty_tool_result` (etc.)
continues to work.

## Numbers

| Metric | Before F.7 | After F.7 |
|---|---|---|
| `orchestrator.py` | 4,772 LOC | **4,572 LOC** (−200) |
| `tool_result_helpers.py` | — | 255 LOC |
| `query_classification.py` (F.6) | 594 LOC | 594 LOC |
| Combined three modules | 5,366 | 5,421 (+55 docstring + `__all__` overhead) |
| Pytest (6-suite refactor canary) | 195 passed / 6 pre-existing failures | **195 passed / 6 pre-existing failures** — identical |
| F.5 deposit verification | PASS | **PASS** |

Cumulative orchestrator.py shrink across F.6 + F.7: **5,265 → 4,572 LOC (−693)**.

## Pattern (unchanged from F.6)

1. Create the sibling module with the moved bodies + an `__all__`.
2. Use the in-container `tmp/refactor_orch_f7.py` script to splice
   out the old definitions and replace them with a single re-export
   block. (Same approach as F.6 — content-boundary anchors instead of
   hard-coded line numbers so the script stays correct as the file
   shrinks.)
3. Restart FastAPI (PYTHONPATH=/app ensures the bind mount wins).
4. Run the validator-adjacent test suite + the F.5 deposit canary.

## Pre-existing failures (not introduced by F.7)

Same six failures observed pre-F.7:

* 5× `TestOllamaTierResolution` — stale post-vLLM-migration tests
* 1× `TestGuardBundle::test_completeness_failure_propagates` —
  asserts pre-tolerance behavior, predates Phase E.3.1

Both should be addressed in their own cleanup ticks. Out of scope
here.

## Carry-overs

Per `docs/master_plan_orchestrator_refactor.md` the remaining
extractions are:

| Phase | Module | Approx size | Risk |
|---|---|---|---|
| F.8 | `graph_entities.py` | 95 LOC | Low-medium (async Neo4j + Redis, isolated) |
| F.9 | `run_cache.py` | 170 LOC | Low (well-bounded interface) |
| F.10 | `prompt_builders.py` | 400 LOC | Medium (many keyword constants, version-versioned cache keys) |
| F.11 | `context_builder.py` | 300 LOC | Medium (couples to citation IDs + bound_set) |
| F.12 | `llm_calls.py` | 830 LOC | Higher (Anthropic + OpenAI-compat backends + retry/failover) |
| F.13 | Final rename | — | Cleanup: `orchestrator.py` → `orchestrator/` package, with `run_deterministic_rag` in `orchestrator/run.py` |
