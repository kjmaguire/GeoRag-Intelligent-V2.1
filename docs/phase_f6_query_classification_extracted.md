# Phase F.6 — Orchestrator refactor, pilot module: `query_classification.py`

**Status:** Complete. First module extracted; subsequent modules planned in
`docs/master_plan_orchestrator_refactor.md`.

## What moved

A new module `src/fastapi/app/agent/query_classification.py` (594 LOC)
collects every keyword-driven classifier helper that used to live in
`orchestrator.py`:

* Keyword sets: `_SPATIAL_KEYWORDS`, `_DOCUMENT_KEYWORDS`,
  `_DOWNHOLE_KEYWORDS`, `_PUBLIC_GEOSCIENCE_KEYWORDS`,
  `_JURISDICTION_ALIASES`, `_CANONICAL_TYPE_HINTS`,
  `_COMMODITY_TOKENS_TO_CODE`, `_ASSAY_KEYWORDS`, `_GRAPH_KEYWORDS`,
  `_LABEL_KEYWORDS`, `_ELEMENT_KEYWORDS`, `_GEO_SYNONYMS`.
* Functions: `_classify_query`, `_extract_public_geoscience_hints`,
  `_extract_graph_entities`, `_extract_label_from_query`,
  `_detect_assay_element`, `_select_temperature`, `_expand_query`,
  `_sanitize_query`.

`orchestrator.py` re-exports all of them so any caller that imported
`from app.agent.orchestrator import _classify_query` (or any other symbol
in the list) keeps working with no change.

## What stayed in orchestrator.py (deliberately)

* `_UNIVERSAL_GRAPH_ENTITIES` (constant)
* `fetch_project_graph_entities` (async, Neo4j + Redis I/O)

These are scheduled for Phase F.8 (`graph_entities.py`) alongside their
infrastructure dependencies. They were swept up by the initial deletion
range during pilot work and restored before commit.

## Numbers

| Metric | Before | After |
|---|---|---|
| `orchestrator.py` | 5,265 LOC | 4,772 LOC (−493) |
| New `query_classification.py` | — | 594 LOC |
| Combined LOC | 5,265 | 5,366 (+101, mostly the new module's docstring + `__all__`) |
| Pytest baseline (4 affected suites) | 128 passed / 5 failed | **128 passed / 5 failed** — identical |
| F.5 verification (Shirley Basin deposit-type) | PASS | **PASS** |

The combined LOC ticked up slightly because the new module ships a
module-level docstring + `__all__` list. The win isn't bytes — it's
cognitive scope. The classifier now has its own home; future
classifier-shape changes diff one focused file.

## Pattern used (import-redirect)

1. Created `query_classification.py` with the new module body.
2. In `orchestrator.py`, deleted the original definitions (lines 91–719)
   and replaced them with a single `from app.agent.query_classification
   import (...)` block.
3. Restored the two symbols that should NOT have moved
   (`_UNIVERSAL_GRAPH_ENTITIES`, `fetch_project_graph_entities`) — see
   the "what stayed" section above.
4. Ran the test suite. No test code changed.

The same pattern will be applied for Phases F.7–F.12 per the master plan.

## Pre-existing failures (not introduced here)

5 tests in `tests/test_model_routing.py` (`TestOllamaTierResolution`)
fail because `Settings` no longer carries `OLLAMA_TIER_ROUTING_ENABLED`
after the vLLM migration. These predate F.6 and predate the multi-expert
eval audit. They should be deleted or rewritten as part of the vLLM
migration cleanup — outside F.6 scope.

## Carry-overs for future phases

* **Phase F.7** — `tool_result_helpers.py` (`_is_empty_tool_result`,
  `_mmr_select_chunks`, `_build_collar_aggregates`,
  `_build_retrieval_summary`). All pure functions. Risk: low.
* **Phase F.8** — `graph_entities.py` (`_UNIVERSAL_GRAPH_ENTITIES`,
  `fetch_project_graph_entities`). Risk: low-medium (async Neo4j +
  Redis, but well-bounded).
* **Phase F.9** — `run_cache.py` (`_fetch_data_versions`, `_cache_key`).
* **Phase F.10** — `prompt_builders.py` (system-prompt selection,
  user-message + project-preamble construction). Largest constants
  section.
* **Phase F.11** — `context_builder.py` (`_build_context`, the citation
  marker / bound_set wiring).
* **Phase F.12** — `llm_calls.py` (Anthropic + OpenAI-compat backends,
  retry, failover; largest module to extract).
* **Phase F.13** — Final cleanup: rename `orchestrator.py` →
  `orchestrator/__init__.py` re-exporting `run_deterministic_rag` from
  `orchestrator/run.py`.

See `docs/master_plan_orchestrator_refactor.md` for ordering rationale.
