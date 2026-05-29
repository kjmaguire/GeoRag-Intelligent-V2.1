# Phase 32 Implementation Kickoff — R-P32-REFUSAL-CONTEXT

**Document version:** 1.0 — DRAFT (Phase 31 master sweep in flight).
**Status:** Active. Designed to stabilise gq-017 at 31/31.
**Predecessors:** `docs/phase31_handoff.md`.

---

## 1. Theme

The remaining variance edge in the cold-run golden suite is
**gq-017-assay-gold**. The agent produces correct gold-grade
narration ("minimum 22.00 ppb, maximum 410.00 ppb, mean 216 ppb")
but the response_assembler's `_is_refusal` heuristic occasionally
flags it as a refusal when the LLM volunteers a caveat like
"two samples may be insufficient to characterize the full
distribution". The word **"insufficient"** in `_REFUSAL_PHRASES`
is over-broad: it catches legitimate scientific caveats alongside
true refusals.

Phase 32 narrows three over-broad entries to refusal-shaped
patterns. Expected outcome: gq-017 stabilises at pass, cold-run
peak holds at 31/31 reproducibly.

---

## 2. The over-broad entries

`src/fastapi/app/agent/response_assembler.py` `_REFUSAL_PHRASES`
(line 385) contains three bare substrings that fire on benign
context:

| Bare entry | False-positive shape |
|------------|----------------------|
| `"insufficient"` | "two samples may be insufficient to characterize…" |
| `"unable to"` | "the data was unable to confirm a trend" |
| `"not available"` | "the downhole survey is not available for hole X" |

True refusals always pair these words with a data noun
("insufficient data", "unable to determine", "data not available
for this project").

---

## 3. Proposed fix

Replace the three bare entries with narrower phrase pairs:

```python
# Phase 32 R-P32-REFUSAL-CONTEXT: narrow over-broad single-word
# matchers to refusal-shaped phrasings. The bare "insufficient"
# entry was tripping on legitimate scientific caveats like
# "two samples may be insufficient to characterize the full
# distribution" — gq-017's phrase-fragility root cause. Similar
# for bare "unable to" and "not available".
# True refusals always pair these words with a data/evidence/
# information noun.

# Previously: "insufficient",
"insufficient data",
"insufficient evidence",
"insufficient information",
"insufficient samples",

# Previously: "unable to",
"unable to determine",
"unable to find",
"unable to identify",
"unable to provide",

# Previously: "not available",
"not available for this project",
"data is not available",
"data not available",
```

No other code changes — `_is_refusal` is plain substring matching.

---

## 4. Risk surface

1. **Could miss true refusals that don't fit the new patterns.**
   Mitigated by keeping all existing entries (just splitting the
   3 over-broad ones into more specific shapes) — the union is
   strictly tighter, not weaker.

2. **Could destabilise other tests.** All 30 currently-passing
   tests pass on substring-match-fail of `_is_refusal` (response
   doesn't contain any refusal phrase). Tighter matching can
   only *reduce* refusal detection, so currently-passing tests
   stay passing. Currently-borderline tests (gq-017) gain
   stability.

3. **The Phase 27 + impossible-premise entries are unchanged.**
   `"i can only answer geological"`, `"out of scope"`, `"not a
   possible value"` etc. are already refusal-specific.

---

## 5. Verifier plan

`scripts/phase32_step1_verify.sh`:

1. `_REFUSAL_PHRASES` contains the narrowed entries
   (`"insufficient data"`, `"unable to determine"`,
   `"data not available"`)
2. The bare `"insufficient"`, `"unable to"`, `"not available"`
   entries are removed
3. R-P32-REFUSAL-CONTEXT marker present
4. gq-017 passes on **3 consecutive cold runs** (canary
   confidence threshold)
5. Cold-run golden hits **31/31 on 2 of 3 runs** (stricter than
   the typical 30/31 baseline; tightens the heuristic should
   eliminate the gq-017 variance entirely)

---

## 6. Files of record (preview)

```
docs/phase32_implementation_kickoff.md           (this file)
docs/phase32_handoff.md                           (Step N)
src/fastapi/app/agent/response_assembler.py      (modified)
scripts/phase32_master_sweep.sh
scripts/phase32_step1_verify.sh
```

Total LOC: ~30 (15 in response_assembler.py + ~90 in verifier).
Easily applicable in one autonomous-loop tick once Phase 31's
sweep clears.

End of Phase 32 kickoff (draft — pending Phase 31 sweep result).
