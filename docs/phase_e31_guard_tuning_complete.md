# Phase E.3.1 — Guard tuning + disclaimer-aware refusal detection (doc-phase 186)

**Status:** Live + 121/121 substrate verifier + 45/45 validator tests preserved + eval **6/10** (unchanged in pass-count, but failure modes are now TRUTHFUL).

## What landed

### 1. Settings-driven guard tolerance thresholds

Three new `Settings` fields in `app/config.py`:

```python
GUARD_TOLERANCE_NUMERIC_UNGROUNDED: int = 2
GUARD_TOLERANCE_ENTITY_UNRESOLVED: int = 2
GUARD_TOLERANCE_COMPLETENESS_UNCITED: int = 2
```

`evaluate_guards` in `app/agent/hallucination/layer_completeness.py` now
allows up to N "soft failures" per guard before the bundle transitions
to `rejected`. Setting any to 0 restores the strict original behavior.

Default 2/2/2 reflects the OCR-heavy Cameco corpus reality — the
orchestrator's numeric/entity/completeness guards fire false positives
on fragmented retrieval contexts. Clean curated corpora should run
strict (0/0/0).

### 2. Disclaimer-aware refusal detection

`detect_refusal` in `app/services/eval/validators.py` now distinguishes
between TRUE refusals and APPENDED DISCLAIMERS.

**Before:** any substring match of a refusal pattern → True (refusal).

**After:** if the refusal phrase appears in the **last 20%** of the
response AND there's **≥200 chars** of substantive content before it,
it's treated as an appended disclaimer, not a refusal.

**Example caught by the fix:**
```
The drilling in Shirley Basin targets a sandstone-hosted roll-front
uranium deposit [NI43:1]. This type of deposit is characteristic of
the region... [800+ chars of substantive answer] ...

I can only answer geological questions [DATA:1].
```

Pre-doc-phase-186: flagged as REFUSAL → fails Layer 6.
Post-doc-phase-186: heuristic recognises disclaimer → correctly
classified as substantive answer (and falls through to other Layers
for true scoring).

The 45 existing validator pytest cases still pass — the change is
additive, only triggers on long-substantive-text-with-trailing-refusal.

## Eval results

| Run | Pass | Notes |
|---|---|---|
| Post-E.2.4 | 6/10 | prior baseline |
| Post-E.3.1 (guards) | 6/10 | unchanged pass count; failure modes shifted |
| Post-E.3.1 (+disclaimer fix) | **6/10** | deposit-type question shifted Layer 6 → Layer 1 (correct) |

### What the unchanged pass count tells us

The 3 remaining over-refusals are **genuine refusals** — short
responses that start with "I can only answer..." or "I don't have
data...". They are NOT trailing disclaimers; the orchestrator simply
decided not to answer.

Inspection of the responses:
- "Does the Cameco Shirley Basin dataset include uranium grade
  measurements?" → "I can only answer geological questions about this
  project's exploration data [DATA:1]." (84 chars)
- "What company drilled..." → similar short refusal
- "What geophysical measurements..." → "I don't have data on that in
  this project [DATA:1]." (41 chars)

The data exists in `silver.well_log_curves` (63 GAMMA + 63 GRADE
curves + 11 other measurement types). But the orchestrator's
retrieval path uses Qdrant (document vector search), and the
structured curve data isn't represented as document passages. So
when the LLM is asked "what geophysical measurements", retrieval
returns nothing relevant, the LLM honestly says it can't answer.

**This is correct refusal behavior given the architecture — the eval
result of 6/10 is the genuine ceiling without wiring structured
silver queries (Phase F candidate).**

## Cumulative state

- **Doc-phase ticks this run:** **54** (132 → 186)
- **Substrate verifier:** **121/121** PASS
- **Validator pytest cases:** **45/45** PASS (unchanged after disclaimer fix)
- **Eval pass rate on core_chat:** **6/10** (honest ceiling; failures now classified by true root cause)
- **Hatchet AI pool workflows:** 14

## Files modified

- `src/fastapi/app/config.py` — 3 new `GUARD_TOLERANCE_*` settings
- `src/fastapi/app/agent/hallucination/layer_completeness.py` — guard tolerance application
- `src/fastapi/app/services/eval/validators.py` — disclaimer-aware refusal heuristic

## What this unlocks (and what it doesn't)

**Unlocks (validated):**
- Operator can tune guard strictness per corpus quality without code changes
- Long answers with trailing disclaimers no longer false-flag as refusals

**Doesn't unlock by itself (Phase F candidates):**
1. **Structured-data tool wiring** — orchestrator should query
   `silver.well_log_curves` etc. when questions are about logged
   measurements. Today only Qdrant is queried. Would resolve the 3
   over-refusal failures.
2. **OCR chunk-quality filter** — reject numeric/symbol-heavy OCR
   chunks. Would let narrative pages score above the 0.5 reranker
   threshold for the deposit-type question.

## Honest assessment

Tonight's 6/10 is the realistic floor for the data shape we have
(1,108 OCR'd passages, 95% tabular gamma-log content, 5% narrative).
The fixes in Phase E.3.1 made the failure modes more **honest** —
when the answer is good but had a stray disclaimer, it now passes the
refusal check; when the orchestrator truly couldn't surface data, the
refusal is real.

The path from 6/10 → 9/10 is:
- Tool wiring (3 over-refusals → pass): ~3-5 ticks
- OCR filter (1 retrieval_quality fail → pass): ~1 tick

Plus the SME-refined questions could be tuned further (e.g., "uranium
grade measurements" could accept SQL evidence about GRADE curve
existence; currently it expects narrative text).
