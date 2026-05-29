# Phase 31 Handoff — gq-006 stale-assertion fix (R-P31-STALE-AUDIT)

**Document version:** 1.0
**Status:** Phase 31 complete. Test-staleness audit closed.
**Predecessors:** `docs/phase30_handoff.md`,
`docs/phase31_test_staleness_audit.md`.

---

## 1. What Phase 31 delivered

Single small fix from the Phase 31 audit doc drafted during the
Phase 29 sweep gap. gq-006 was identified as the last stale-
fixture-assertion case in the audit; this phase applied the
1-line fix.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `src/fastapi/tests/test_golden_queries.py` — gq-006-completed-holes `expected_answer_contains` updated from `["9"]` to `["19"]`, `must_not_contain` updated from `["10 completed", "all completed"]` to `["20 completed", "all completed"]`. | `scripts/phase31_step1_verify.sh` (5/5) |
| 2 | This handoff + master sweep | — |

---

## 2. The fix in one paragraph

Pre-Phase-17 the fixture had 10 collars: 9 Completed + 1 In
Progress. The test was authored against that state with
`expected_answer_contains: ["9"]`. Phase 17 added 10 XLS-24-*
collars, all Completed, raising the count to 19. The test stayed
in the passing set because the agent's answer "19 holes are
completed" contains the digit "9" as a substring — same shape as
Phase 26's gq-005 false-positive on "10" in "510 m TD".

After the fix:

```python
"expected_answer_contains": ["19"],
"must_not_contain": ["20 completed", "all completed"],
```

`silver.collars` confirmation:

```
   status    | count
-------------+-------
 Completed   |    19
 In Progress |     1
```

gq-006 now passes against the intended assertion (19 ↔ 19) rather
than the substring incidental ("9" ↔ "19"). No agent or
orchestrator code changed.

---

## 3. About the variance edge

The full cold-run after this change shows 30/31 with gq-017
(assay-gold) failing on confidence 0.100 < 0.400. gq-017's
phrase-rendering variance has been documented since Phase 22 —
when the agent volunteers a caveat like "based on only two
assay samples, this may be insufficient to characterize…",
the word **"insufficient"** matches `_REFUSAL_PHRASES` in
`response_assembler.py` and triggers Layer A's confidence=0.1
override. The response text is geologically correct; the
heuristic refusal classifier over-flags.

Across this session's autonomous run, gq-017 has toggled in and
out of the passing set. The 31/31 result observed at Phase 30
close was a lucky variance run; **typical cold-run sits at
30/31** with gq-017 as the toggle. This is the natural ceiling
of the suite against its current fixture state + agent prompt
+ refusal-phrase list.

Possible future fix (out of scope here): refine
`_REFUSAL_PHRASES` so "insufficient" only counts when it modifies
"data" / "evidence" / etc. — i.e. distinguish "insufficient data"
(true refusal) from "two samples may be insufficient to
characterize a population" (legitimate scientific caveat).

---

## 4. Cumulative session trajectory

| Phase | Cold-run typical | Notes |
|-------|-----------------:|-------|
| 13 (baseline) | 13 | initial |
| ... | ... | ... |
| 30 | 30 (peak 31) | full cache pipeline |
| **31** | **30 (peak 31)** | **gq-006 fix lands; gq-017 phrase-variance documented** |

Net pass count: unchanged from Phase 30's typical 30/31, but
**gq-006 now passes intentionally** rather than incidentally.

---

## 5. Carry-overs for Phase 32+

The original goal-list driving Phases 18–30 is exhausted. The
audit doc named gq-006 as the last test-staleness case; that's
now closed. Remaining items are all out-of-scope for the
fixture/agent/cache shape this autonomous run targeted:

| ID | Item | Priority |
|----|------|----------|
| R-P32-REFUSAL-CONTEXT | Refine `_REFUSAL_PHRASES` so "insufficient" requires data/evidence context (would stabilise gq-017 at 31/31) | Medium |
| R-P15-1 | Bundled orchestrator prompts migration | Medium |
| R-P11-B | Frontend Search/Query page | Medium |
| R-P21-CACHE-TELEMETRY-DASHBOARD | Surface `cache_skipped_reason` in operator dashboard | Low |

---

## 6. Files of record

```
src/fastapi/tests/test_golden_queries.py   (Step 1 — gq-006 stale assertion fixed)
docs/phase31_handoff.md                     (this file)
scripts/phase31_master_sweep.sh
scripts/phase31_step1_verify.sh
```

---

## 7. Re-running

```bash
bash scripts/phase31_step1_verify.sh   # gq-006 fix + cold run
bash scripts/phase31_master_sweep.sh   # Phase 0 → 31 sweep
```

End of Phase 31 handoff.
