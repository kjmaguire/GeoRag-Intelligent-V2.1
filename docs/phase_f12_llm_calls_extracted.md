# Phase F.12 — `llm_calls.py` extracted

**Status:** ✅ Done. Orchestrator dropped **4,301 → 3,498 LOC (−803)**.
The 854-LOC LLM-call machinery that the carry-over flagged as
"high risk of breaking the entire RAG pipeline" is now its own focused
module behind a re-export. Canaries: 35/35 pass. End-to-end smoke test:
real vLLM call returns a 63-collar answer in 12.3s.

## What moved

`src/fastapi/app/agent/llm_calls.py` (new, 723 LOC) now owns:

| Symbol | Purpose |
|---|---|
| `_llm_call_counter` | Per-query `ContextVar[int]` budget counter |
| `LLMCallBudgetExceeded` | Raised when `MAX_LLM_CALLS_PER_QUERY` is exceeded |
| `_build_user_message` | Assembles the per-turn `CONTEXT + USER QUESTION + ANSWER:` body |
| `_call_openai_compatible_llm` | vLLM/Ollama wire format — streaming, Qwen3 sampling, JSON mode, prefix-cache logging |
| `_resolve_local_llm_fallback_target` | `(base_url, model)` for the Anthropic→local cross-backend failover |
| `_resolve_deepseek_fallback_target` | Legacy alias (Phase-2 cutover artifact) |
| `_call_anthropic_llm` | Anthropic Messages API with prompt caching, priority tier, adaptive-thinking telemetry, cost accounting |
| `_call_llm` | Backend dispatcher — picks Anthropic vs OpenAI-compat, enforces budget, splices CORRECTION on retry |

`orchestrator.py` retains the retry + failover loop in
`run_deterministic_rag` (lines ~2400-2740). Only the per-call wire
format moved.

## How the cycle was avoided

The two HTTP callers fall back to `_SYSTEM_PROMPT_DEFAULT` from
`orchestrator.py` when the caller doesn't pass one. We can't import
that constant at module load time (orchestrator imports from us), so
the new module defines a `_default_system_prompt()` helper that
imports lazily on first call:

```python
def _default_system_prompt() -> str:
    from app.agent.orchestrator import _SYSTEM_PROMPT_DEFAULT  # lazy
    return _SYSTEM_PROMPT_DEFAULT
```

All production callers pass an explicit `system_prompt` (the C5
classifier-selected variant), so the lazy path only fires in tests
and the rare back-compat call.

## Re-export contract

`orchestrator.py` now exposes the same symbols via:

```python
from app.agent.llm_calls import (  # noqa: E402
    LLMCallBudgetExceeded,
    _build_user_message,
    _call_anthropic_llm,
    _call_llm,
    _call_openai_compatible_llm,
    _llm_call_counter,
    _resolve_deepseek_fallback_target,
    _resolve_local_llm_fallback_target,
)
```

Identity verified at runtime:

```
>>> from app.agent.orchestrator import _call_llm
>>> from app.agent.llm_calls import _call_llm as direct_call
>>> _call_llm is direct_call
True
```

External callers that did `from app.agent.orchestrator import _call_llm`
(or any of the 7 other symbols) keep working unchanged.

## Verification

| Check | Result |
|---|---|
| `import app.agent.orchestrator` succeeds | ✅ |
| Re-export identity (`orch._X is llm._X` for all 8 symbols) | ✅ |
| `tests/test_context_packing.py` | 4 / 4 pass |
| `tests/test_response_assembler_pgeo.py` | 19 / 19 pass |
| `tests/test_pdf_renderer.py` | 12 / 12 pass |
| End-to-end smoke (`run_deterministic_rag` against real vLLM) | 12.3s, 63 collars returned, 1 citation |

## Orchestrator-refactor scoreboard

| Phase | LOC saved |
|---|---|
| F.6 — `query_classification.py` | −280 |
| F.7 — `tool_result_helpers.py` | −250 |
| F.8 — `graph_entities.py` | (pending) |
| F.9 — `query_project_overview` | +50 |
| F.10 — prompts/ reconciliation | mirror-only, no LOC |
| F.11 — `context_builder.py` | −216 |
| **F.12 — `llm_calls.py`** | **−803** |
| F.13 — package rename | (pending) |

`orchestrator.py` is now **3,498 LOC**, down from **5,267 LOC** at the
start of the orchestrator-refactor track. The remaining LOC is mostly
`run_deterministic_rag` + the cache key/version helpers + a few small
utilities — all of which are operational state that the orchestrator is
the right home for.

## What this unblocks

* **F.13 — package rename** (final cleanup task). The LLM-call surface
  is now isolated; F.13 can rename `app.agent.*` ↔ `app.rag.*` without
  having to thread an 854-LOC monolith through the rename.
* **Future LLM-backend additions.** A new backend (e.g. `mistral.ai`)
  is now a single function added to `llm_calls.py` + a dispatch arm in
  `_call_llm` — no orchestrator surgery.
* **Cost-control / observability work.** The metrics + audit-label
  surface (PROMPT_TOTAL_TOKENS, LLM_COST_USD, LLM_CALL_BUDGET_EXCEEDED)
  is all in one module now, so changes there don't touch the
  orchestrator's retry/failover loop.

## Files

* **New:** `src/fastapi/app/agent/llm_calls.py` (723 LOC)
* **Edited:** `src/fastapi/app/agent/orchestrator.py` (−803 LOC; 7-symbol
  re-export added)
