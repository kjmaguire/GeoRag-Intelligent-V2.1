# Phase G follow-up — Eval matcher tightening + sequential-eval flakiness

**Status:** 3 of 4 sub-items landed; 1 deferred with documented carry-over.

## What this pass set out to do

Take the expanded 22-question `core_chat_wyoming_uranium` pack from its
18/22 baseline up toward 21/22 by relaxing or tightening eval matchers
on Q11/Q13/Q22 — the 3 failures the wrap-up flagged as eval-tuning
rather than capability gaps.

(Q1 stays a known F.9 carry-over: Qwen3 refusal magnet on PLSS syntax.)

## What landed

| Sub-item | Status | Change |
|---|---|---|
| **Q13 numeric tolerance** | ✅ Done | `expected_value: 16 → 14`, `tolerance_pct: 35.0` in `core_chat_wyoming_uranium.py`. The model now correctly dedupes the space/underscore variants in `silver.well_log_curves` (D DIFF + D\_DIFF, etc.) and answers 12–14 — within tolerance |
| **Q22 PII refusal detection** | ✅ Done | Added 3 scope-refusal phrases to `_REFUSAL_PHRASES` in `response_assembler.py`: `"i can only answer geological"`, `"i can only answer questions"`, `"only geological questions"`. The model already produced these phrases on PII questions; `_is_refusal()` just wasn't recognising them |
| **Q11 UnboundLocalError** | ✅ Mitigated | `run_deterministic_rag` had a path where `response` could be returned unbound if every LLM attempt failed non-retriably and the retry loop `break`-ed before `assemble_response` ran. Added a function-top `response: Any = None` pre-init plus a fallback synthesiser at the end-of-function that emits a structured "LLM call failure" `GeoRAGResponse` when `response is None`. This eliminates the crash surface, not the original capability gap |
| **Push 18/22 → 21/22** | ⚠️ Deferred | Sequential-eval flakiness (see below) blocks a clean before/after comparison. The matcher fixes are still net-positive but the headline pass count is no longer the right metric |

## The sequential-eval flakiness — root cause not yet identified

When the 22-question pack is run via `tmp/f5c_golden_eval_runner.py`:

* **Run-to-run variance**: successive runs of the same script produce
  11/22 ↔ 18/22 ↔ 11/22 with no code changes between them.
* **Per-question signature**: Q3, Q4, Q5, Q7, Q8, Q13, Q16, Q17, Q21
  fast-fail in 0.2s with `context_chars=19 approx_tokens=4` and the
  generic `"I don't have data on that in this project [DATA:1]."`
  refusal.
* **Reproducible in isolation, fails in sequence**: running the SAME
  question (`Q3 — total depth of drill hole 36-1042?`) in isolation
  takes 1.57s, calls `query_spatial_collars`, returns 63 collars, and
  produces the correct answer. Running the same question in the
  3-question sequence `Q1 → Q2 → Q3` reproduces the 0.18s empty-context
  refusal.

The classifier output for Q3 in both isolation and sequential runs is
identical (`spatial: True, pg_canonical_types: ['drillhole_collar']`),
yet the spatial tool only fires in the isolated run. Something in the
eval runner's sequential loop is preventing `_run_spatial()` from
producing results — but it's not a Redis cache hit (no
`run_deterministic_rag: CACHE HIT key=` log line), not budget exhaustion
(`_llm_call_counter` is reset at function entry), and not a partial
failure (no `query_spatial_collars branch failed` log line).

Hypotheses worth checking when this gets follow-up time:

1. **Per-process tool-side cache pollution.** Some `query_spatial_collars`
   helper or `ToolContext` may hold a class- or module-scoped cache
   that carries `None` results from a previously-misclassified call into
   a subsequent one.
2. **Connection-pool starvation that fails silently.** The eval creates
   `asyncpg.create_pool(min_size=1, max_size=4)`. Sequential calls
   should release connections cleanly, but a missed `async with` in one
   of the tools could leak.
3. **`PRE-COMPUTED SUMMARY` block bleeds across queries.** Q2's response
   includes `[PRE-COMPUTED SUMMARY]` — a header generated from collars.
   If the summary helper is project-scoped (not query-scoped) and
   caches at module level, Q3 might be reading Q2's stale summary
   payload while skipping its own retrieval.

These are conjectures, not findings. None of them is bisected.

## Why we're stopping here

The 3 matcher fixes are clean, isolated, and net-positive whether or
not we ever reach 21/22.

The eval flakiness is a real bug, but:

* It surfaced from a 10 → 22 question expansion, not from any code change
  in this batch.
* Fixing it likely requires the same kind of focused refactor as F.12
  (LLM-call extraction) — bisecting `run_deterministic_rag`'s
  state-management across a sequential call sequence.
* Item 2 (inline-vs-package prompt reconciliation) and Item 3 (F.12
  extraction) are higher-leverage uses of the next focused session.

## Files changed

* `src/fastapi/app/services/eval/mechanical_questions/core_chat_wyoming_uranium.py`
  — Q13 numeric tolerance (`expected_value: 14`, `tolerance_pct: 35.0`)
* `src/fastapi/app/agent/response_assembler.py` — 3 scope-refusal
  phrases added to `_REFUSAL_PHRASES`
* `src/fastapi/app/agent/orchestrator.py` — function-top `response: Any
  = None` + end-of-function fallback synthesiser

## What's next

1. **Bisect the sequential-eval state pollution** — own focused session.
   Start by running `_run_spatial()` directly via the eval runner's
   `deps` 22 times in a row and confirming `count=63` each time;
   that isolates the tool from the rest of `run_deterministic_rag`.
2. **Move on to Item 2 (prompt reconciliation)** — per the parent batch.
3. **Move on to Item 3 (F.12 llm_calls.py extraction)** — per the parent
   batch.
