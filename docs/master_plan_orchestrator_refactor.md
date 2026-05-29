# Master plan — `app/agent/orchestrator.py` refactor

**Status:** In progress (Phase F.6 = pilot extraction)
**Current size:** 5,265 LOC, 25 top-level functions
**Target shape:** ~8 focused modules under `app/agent/orchestrator/` package, with the public surface (`run_deterministic_rag` + `fetch_project_graph_entities`) re-exported from `app/agent/orchestrator/__init__.py`.

## Why now

Every Phase F.\* tick has touched this file. It's load-bearing for retrieval,
citation, hallucination guards, and LLM dispatch. The 5K-LOC single-file
shape makes diffs noisy, encourages copy-paste, and conceals dead code.
The multi-expert eval (overnight 2026-05-14) called this out as the #1
tech-debt risk.

## Decomposition map

| # | Target module | Lines moved | Functions / constants extracted |
|---|---|---|---|
| 1 | `app/agent/query_classification.py` | ~430 | `_SPATIAL_KEYWORDS`, `_DOCUMENT_KEYWORDS`, `_DOWNHOLE_KEYWORDS`, `_PUBLIC_GEOSCIENCE_KEYWORDS`, `_JURISDICTION_ALIASES`, `_CANONICAL_TYPE_HINTS`, `_COMMODITY_TOKENS_TO_CODE`, `_ASSAY_KEYWORDS`, `_GRAPH_KEYWORDS`, `_LABEL_KEYWORDS`, `_ELEMENT_KEYWORDS`, `_GEO_SYNONYMS`, `_classify_query`, `_extract_public_geoscience_hints`, `_extract_label_from_query`, `_detect_assay_element`, `_select_temperature`, `_expand_query`, `_sanitize_query`, `_extract_graph_entities` |
| 2 | `app/agent/graph_entities.py` | ~95 | `_UNIVERSAL_GRAPH_ENTITIES`, `fetch_project_graph_entities` |
| 3 | `app/agent/prompt_builders.py` | ~400 | `_select_system_prompt`, `_build_user_message`, `_build_project_facts`, `_build_project_preamble`, plus prompt-version constants |
| 4 | `app/agent/llm_calls.py` | ~830 | `_call_openai_compatible_llm`, `_resolve_local_llm_fallback_target`, `_call_anthropic_llm`, `_call_llm`, plus the `_llm_call_counter` ContextVar + `LLMCallBudgetExceeded` |
| 5 | `app/agent/tool_result_helpers.py` | ~250 | `_build_collar_aggregates`, `_mmr_select_chunks`, `_is_empty_tool_result`, `_build_retrieval_summary` |
| 6 | `app/agent/context_builder.py` | ~300 | `_build_context` and its helpers |
| 7 | `app/agent/run_cache.py` | ~170 | `_fetch_data_versions`, `_cache_key` |
| 8 | `app/agent/orchestrator/run.py` | ~2,350 | `run_deterministic_rag` (the main orchestrator function — still the largest piece, but now consuming the modules above instead of co-defining them) |

**Combined target:** seven sibling modules + one runner = ~4,825 LOC across
8 files vs. 5,265 in one file. The line count goes down modestly because
common imports are deduplicated; the real win is each file fits a single
mental model.

## Pilot extraction (Phase F.6 — this tick)

Module 1 (`query_classification.py`) is the safest first extraction:

* All-synchronous, no I/O dependencies
* Stateless — no module-level mutable state
* Tightly cohesive — eight related classification helpers + their keyword tables
* No down-stream callers OTHER than `orchestrator.py` itself

The migration follows the **import-redirect** pattern:

1. Create `app/agent/query_classification.py` with the new module body.
2. In `orchestrator.py`, replace each old definition with a re-export:
   `from app.agent.query_classification import _classify_query`
3. Run the existing test suite. No test should need to change — the public
   API (`from app.agent.orchestrator import _classify_query`) is preserved.
4. Add module-level tests for the new module's surface (only if coverage was
   thin before — most of these helpers are exercised via integration tests).

## Acceptance criteria for each module extraction

* All existing `from app.agent.orchestrator import X` callers must still work.
* `docker compose exec fastapi pytest` exits 0 (or shows the same failures
  as the pre-refactor baseline).
* Every constant + helper that was previously private (`_foo`) remains
  private — only the function/constant moves; the underscore stays.
* No behavior change. This is a structural refactor, not a semantic one.
* Phase doc-handoff named `phase_f6_X_complete.md` per extraction.

## Out of scope for the refactor

* Renaming any public function or changing any keyword-set contents
* Tightening type annotations beyond what the move requires
* Performance tuning (these are pure-function helpers — they're already O(n))
* Adding new tests beyond covering the new module surface
* Touching `run_deterministic_rag` body except to update its imports

## Order of operations

| Phase | Module | Rough size | Risk |
|---|---|---|---|
| **F.6** (pilot) | `query_classification.py` | 430 LOC | Low (pure functions, no I/O) |
| F.7 | `tool_result_helpers.py` | 250 LOC | Low (pure functions) |
| F.8 | `graph_entities.py` | 95 LOC | Low-medium (async + Neo4j, but isolated) |
| F.9 | `run_cache.py` | 170 LOC | Low (well-bounded interface) |
| F.10 | `prompt_builders.py` | 400 LOC | Medium (many keyword constants, version-versioned cache keys) |
| F.11 | `context_builder.py` | 300 LOC | Medium (couples to citation IDs + bound_set) |
| F.12 | `llm_calls.py` | 830 LOC | Higher (touches the OpenAI-compat + Anthropic backends + retry logic) |
| F.13 | (final) | n/a | Cleanup: rename `orchestrator.py` → `orchestrator/__init__.py` re-exporting `run_deterministic_rag` from `orchestrator/run.py` |

Each extraction is its own commit. Bisecting on regressions stays
practical because every commit is "still passes tests."
