# Phase F.11 — `context_builder.py` extracted

**Status:** Complete. `_build_context` (the 326-LOC pure function that
formats tool results into the LLM's CONTEXT block) moved to its own
sibling module.

## Numbers

| Metric | Pre-F.11 | Post-F.11 |
|---|---|---|
| `orchestrator.py` | 4,517 LOC | **4,301 LOC** (−216) |
| `context_builder.py` | — | 332 LOC |
| Cumulative shrink since F.6 start | — | **5,265 → 4,301 (−18%)** |
| Canary | 244 / 0 | **248 / 0** (+4 from test_context_packing passing) |
| F.5 deposit verification | PASS | **PASS** |
| Golden eval | 9 / 10 | **9 / 10** (unchanged) |

## Pattern (unchanged from F.6/F.7/F.8)

The extraction is purely structural:

1. New module `src/fastapi/app/agent/context_builder.py` carries the
   `_build_context` function verbatim + the imports it needs (the 6
   tool-result dataclasses + 2 tool_result_helpers + settings).
2. `orchestrator.py` replaces the inline body with a single
   `from app.agent.context_builder import _build_context  # noqa: E402`
   re-export so existing callers (including
   `tests/test_context_packing.py`) keep working.

No behavior change. No prompt change. No new dependencies.

## Carry-over

* **F.10 — `prompt_builders.py`** remains blocked on the inline-vs-package
  prompt drift documented in `docs/phase_f10_carry_over_prompt_drift.md`.
* **F.12 — `llm_calls.py`** (854 LOC) is the next biggest extraction
  but carries higher risk (full LLM-call machinery: OpenAI-compat backend,
  Anthropic backend, retry, failover, counter, budget exception). Deferred
  to a focused refactor session.
* **F.13 — final package rename** of `orchestrator.py` →
  `orchestrator/run.py` waits on F.10 + F.12 landing first.

After F.10 + F.12 + F.13, the original 5,265-LOC monolith would be
broken into 8 focused modules of 95-830 LOC each.
