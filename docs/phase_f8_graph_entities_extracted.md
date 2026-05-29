# Phase F.8 — Orchestrator refactor, module 3: `graph_entities.py`

**Status:** Complete. Same import-redirect pattern as F.6 + F.7.

## What moved

New module `src/fastapi/app/agent/graph_entities.py` (132 LOC) holds:

* `_UNIVERSAL_GRAPH_ENTITIES` — the 4-element fallback lithology code list.
* `fetch_project_graph_entities` — async Neo4j fetch with Redis caching
  and graceful degradation.

`orchestrator.py` re-exports both. Any caller that imported either
symbol from `app.agent.orchestrator` continues to work unchanged.

## Numbers

| Metric | Before F.8 | After F.8 |
|---|---|---|
| `orchestrator.py` | 4,572 LOC | **4,477 LOC** (−95) |
| `graph_entities.py` | — | 132 LOC |
| Cumulative shrink since F.6 start | — | **5,265 → 4,477 (−788 LOC, −15%)** |
| Refactor canary tests | 195 passed / 6 pre-existing failures | **195 passed / 6 pre-existing failures** — identical |
| F.5 deposit verification | PASS | **PASS** |

## Pre-existing failures unchanged

Same six failures as the post-F.7 baseline. Both clusters
(`TestOllamaTierResolution` x5 + `test_completeness_failure_propagates`)
are scheduled for cleanup in the next item.

## Remaining refactor work

Per the master plan:

| Phase | Module | Approx size | Risk |
|---|---|---|---|
| F.9 | `run_cache.py` | 170 LOC | Low (well-bounded interface) |
| F.10 | `prompt_builders.py` | 400 LOC | Medium (system-prompt + project preamble) |
| F.11 | `context_builder.py` | 300 LOC | Medium (couples to citation IDs + bound_set) |
| F.12 | `llm_calls.py` | 830 LOC | Higher (Anthropic + OpenAI-compat backends + retry/failover) |
| F.13 | Package rename | — | Cleanup: `orchestrator.py` → `orchestrator/` package |
