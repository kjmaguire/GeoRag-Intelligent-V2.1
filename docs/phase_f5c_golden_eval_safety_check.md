# Phase F.5c — Golden-eval safety check (10 questions, core_chat_wyoming_uranium)

**Status:** Safety-check complete. **No regression** introduced by F.4 + F.5 + F.5b + PYTHONPATH unification + 28-file sync.

## Result

| Metric | Value |
|---|---|
| Pass rate | **6 / 10** |
| Baseline (Phase E.2.4 onward) | 6 / 10 |
| Δ | **0** — same count, same set of failing questions |
| Wall time, full pack | ~24 s |

## Per-question grid

| # | Question | Result | Time | Notes |
|---|---|---|---:|---|
| 1 | What company drilled the holes in section 28N 79W of Shirley Basin? | **FAIL** | 1.3 s | Model refused — "evidence does not include information about which company drilled" |
| 2 | How many drill holes does the Cameco Shirley Basin project have? | PASS | 0.3 s | "63" surfaced |
| 3 | What is the total depth of drill hole 36-1042? | PASS | 0.9 s | 339.9 ft surfaced |
| 4 | When was drill hole 36-1042 logged? | PASS | 0.4 s | 2012-08-13 surfaced |
| 5 | What geophysical measurements were collected? | **FAIL** | 0.2 s | Model refused — "I don't have data on that in this project" |
| 6 | Max drilled depth across all holes? | PASS | 1.2 s | Max depth surfaced |
| 7 | What county and state is the project in? | **FAIL** | 1.0 s | Model refused — "evidence does not include information about the county or state" |
| 8 | Does the dataset include uranium grade measurements? | **FAIL** | 0.2 s | Model refused — "I don't have data on that in this project" |
| 9 | What is the total uranium production rate? | PASS | 0.2 s | Refusal expected ✓ |
| 10 | What type of uranium deposit is targeted? | PASS | 3.5 s | Roll-front + sandstone-hosted ✓ — *this is the question F.5 + F.5b fixed* |

## Quality improvement that doesn't show in the pass-count

Question #10 (deposit type) was previously the F.5 / F.5b target. Pre-fix
the same question would PASS its text content (entities matched) but
along the way it would:

* Retry the LLM call up to 2 times (numeric_guard, entity_guard,
  completeness_guard all firing)
* Emit `post_assembly_validation: 9 warning(s) (critical=6, high=3, advisory=0)`
* End with `guard_entity_fail` / `guard_numeric_fail` rejection payloads
  unless tolerance covered it

Post-fix:

* **0 retries** — commits on attempt 1
* **0** `post_assembly_validation` warnings (Layers 3, 4, 6 each return zero)
* `citation_guard_eval: all_passed=True, failed_guards=[]`

So the system is **doing materially less work** to reach the same
verdict on every passing question, and the same is true for #10
specifically. The benefit is wall-time, log noise, and LLM cost — not
the pass-count.

## Why the 4 remaining failures are not regressions

All four are sub-2-second refusals where the model says "evidence does
not include information about X." The data IS in the corpus (company
name `CAMECO RESOURCES` in `silver.collars`, county `CARBON` + state
`WY` parsable from documents, GAMMA + GRADE log curves in
`silver.well_log_curves`), but it isn't being **surfaced** by the
classifier-driven tool dispatch. Specifically:

* The deterministic keyword classifier produces no match for words like
  "company," "county," "state," "geophysical measurements,"
  "grade measurements" → falls back to `spatial+documents`.
* Spatial returns collars (good) but the collar table doesn't carry
  the company name in a column the LLM is shown.
* Documents retrieves OCR chunks (noisy) — clean PDF chunks with
  the relevant text get reranked below noise per the doc-phase 188
  audit.

This is the **6/10 ceiling** described in `docs/phase_e31_guard_tuning_complete.md`:

> Tonight's 6/10 is the realistic floor for the data shape we have …
> The path from 6/10 → 9/10 is: structured-data tool wiring (Phase F.4
> done; need similar for company/county/log-curve enums) + OCR
> aggressive-prune + KG-aware retrieval reranking.

None of those four failures touch the four pieces of work checked here
(empty-tool filter, insights strip, Layer 4 whitelist, PYTHONPATH
unification). They're all retrieval-routing gaps, not validator
gaps.

## Verdict

* F.4 + F.5 + F.5b + PYTHONPATH + 28-file sync are **safe to commit**.
* The Shirley Basin deposit-type question (Q10) is now **fast + clean** instead of fast-and-noisy.
* Push toward 7/10+ requires structured-tool wiring (next: company / county / log-curve enums), which is its own phase.
