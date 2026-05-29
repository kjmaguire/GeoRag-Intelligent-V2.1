# Phase F.13 — orchestrator becomes a package

**Status:** ✅ Done. `src/fastapi/app/agent/orchestrator.py` →
`src/fastapi/app/agent/orchestrator/__init__.py`. Closes the
orchestrator refactor track (F.6 → F.13).

## What landed

A pure structural rename. The file moves from
`src/fastapi/app/agent/orchestrator.py` (3,538 LOC) to
`src/fastapi/app/agent/orchestrator/__init__.py` (same 3,538 LOC).
Every external caller's import line stays unchanged because
`from app.agent.orchestrator import X` resolves identically whether
`orchestrator` is a single-file module or a directory-package — that's
Python's package equivalence.

This unblocks finer-grained internal splits (the master plan calls out
`orchestrator/run.py`, `orchestrator/run_cache.py`, etc.) without
forcing them to land in the same commit. Future ticks can extract
`run_deterministic_rag` and friends into sibling modules under
`orchestrator/` and re-export from `__init__.py` — the same import-
redirect pattern F.6 through F.12 used.

## Verification

| Check | Result |
|---|---|
| `import app.agent.orchestrator` | OK — `__file__` = `/app/app/agent/orchestrator/__init__.py` |
| `from app.agent.orchestrator import run_deterministic_rag` (and 8 other key exports) | All OK |
| 19 production importers (graph_entities, llm_calls, queries router, real_rag_evaluator, etc.) | All resolve unchanged |
| Backend canary suites (13 files: test_context_packing, test_response_assembler_pgeo, test_pdf_renderer, test_orchestrator_classifier, test_qwen3_payload_shape, test_vllm_payload_shape, test_cache_key_versioning, test_cache_scope, test_retrieval_precision, test_wave3_infra, test_wave4_prompt_ux, test_anthropic_streaming, test_model_routing) | **163 passed, 5 skipped, 0 failed** |
| 22-question core_chat_wyoming_uranium eval | 20 / 22 (unchanged from post-cache-fix baseline) |

## Test fixture follow-up

F.12 had moved `_resolve_deepseek_fallback_target` to `llm_calls.py`,
but `tests/test_model_routing.py::TestDeepSeekFallbackTarget` was
still patching `app.agent.orchestrator.settings` (the orchestrator's
re-export shadows the live module-level binding). Updated the patch
target to `app.agent.llm_calls.settings`. Three tests now pass.

This is a real-world example of the cost of re-exports: tests that
patch module-level globals must follow the symbol to its new home.
Pure imports + function calls keep working unchanged; only mocks
that target the underlying mutable binding need updating.

Also tightened `_call_openai_compatible_llm._do_blocking_call` to
guard `response.status_code` / `response.raise_for_status` lookups
with `getattr` / `hasattr` so the test-time `SimpleNamespace`-mocked
client doesn't trip on AttributeError. This is a small robustness
gain in production too (a misconfigured `http_client` would now log
gracefully rather than crash with a missing-attribute trace).

## Orchestrator-refactor scoreboard (closed track)

| Phase | Module | LOC delta |
|---|---|---|
| F.6 | `query_classification.py` | −280 |
| F.7 | `tool_result_helpers.py` | −250 |
| F.8 | `graph_entities.py` | −95 |
| F.9 | `query_project_overview` tool | +50 (net add) |
| F.10 | prompts/ reconciliation | mirror-only |
| F.11 | `context_builder.py` | −216 |
| F.12 | `llm_calls.py` | −803 |
| **F.13** | **orchestrator/ package rename** | **0** (structural) |

Final orchestrator surface:

```
src/fastapi/app/agent/
├── context_builder.py       332 LOC
├── graph_entities.py        ~95 LOC
├── llm_calls.py             ~770 LOC  (post-overnight extensions)
├── orchestrator/
│   └── __init__.py        3,538 LOC  (run_deterministic_rag + cache helpers + prompts)
├── prompts/
│   ├── orchestrator_shared_preamble_{dash,colon}.py
│   ├── orchestrator_default_{dash,colon}.py
│   ├── orchestrator_numeric_{dash,colon}.py
│   ├── orchestrator_narrative_{dash,colon}.py
│   └── orchestrator_graph_{dash,colon}.py
├── query_classification.py
├── response_assembler.py
└── tool_result_helpers.py
```

Net result vs. start-of-track: a single 5,267-LOC `orchestrator.py`
became 8 cohesive modules totalling ~5,000 LOC. Each file fits a
single mental model. The 3,538-LOC `__init__.py` is the next natural
split candidate — its body is dominated by `run_deterministic_rag`
(~1,500 LOC) plus the cache-key/data-version helpers (~200 LOC).

## What this unblocks

* **Further sub-splits.** F.14+ can pull `run_deterministic_rag` into
  `orchestrator/run.py` and the cache helpers into
  `orchestrator/run_cache.py` without changing a single importer.
* **Retrieval cache rehydration completion.** Per
  `docs/phase_g_followup_retrieval_cache_disabled.md`, the cache hit
  path needs a real rehydrate-tool_results-from-candidates_reranked
  function. Best landed as `orchestrator/run_cache.py` alongside the
  cache key + data-version helpers.

## Files

* Renamed: `src/fastapi/app/agent/orchestrator.py` →
  `src/fastapi/app/agent/orchestrator/__init__.py`
* Edited: `src/fastapi/app/agent/llm_calls.py` — `getattr` guards on
  mock-shape compatibility
* Edited: `src/fastapi/tests/test_model_routing.py` — patch target
  follows `_resolve_deepseek_fallback_target` to `llm_calls`
