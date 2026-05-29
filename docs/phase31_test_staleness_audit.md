# Phase 31 Test-Staleness Audit (read-only scoping)

**Document version:** 1.0 — DRAFT
**Status:** Audit only, drafted during Phase 29 sweep window.
**Predecessors:** `docs/phase26_handoff.md` (which corrected gq-005 + gq-020).

A scan of `src/fastapi/tests/test_golden_queries.py` for any other
test cases that share the same shape as gq-005 and gq-020 — where
the test assertion was authored against an older fixture state and
is now incidentally passing on substring matches rather than the
intended condition. These are R-P14-3.6 candidates ("test fixes
that are corrections, not relaxations").

---

## 1. gq-006-completed-holes — **STALE**

```python
"expected_answer_contains": ["9"],
"must_not_contain": ["10 completed", "all completed"],
```

**Reality (verified against silver.collars 2026-05-12)**:

```
   status    | count
-------------+-------
 Completed   |    19
 In Progress |     1
```

Pre-Phase-17 the fixture had 10 collars total, 9 of which were
Completed and 1 In Progress — `"9"` was correct then. Phase 17
added 10 XLS-24-* collars, all Completed, raising the Completed
count to 19.

The test is *currently passing* because "19" contains the digit
"9" as a substring. The matched answer is "19", not the intended
"9". Same shape as gq-005's pre-Phase-26 false-positive on "10"
in "510 m TD".

**Recommended fix** (Phase 31 Step 1, ~3 LOC):

```python
"expected_answer_contains": ["19"],   # was "9" before Phase 17 raised the count
"must_not_contain": ["20 completed", "all completed"],
```

Plus a comment naming the Phase 17 fixture change for future
re-scoping. The `"10 completed"` must_not_contain becomes
`"20 completed"` to guard against the agent mistakenly
counting the in-progress hole as Completed.

---

## 2. gq-022-primary-commodity — **BRITTLE** (not yet failing)

```python
"expected_answer_contains": ["uranium"],
"must_not_contain": ["gold", "copper"],
```

The query is about primary commodity — uranium is correct.
But Phase 18 seeded gold assays (`Au_ppb`) on PLS-22-08 to
unlock gq-017. The agent might now legitimately mention "gold"
in the context of secondary commodities or assay samples
("Uranium is the primary commodity, with secondary gold assays
in PLS-22-08"). That's a correct geological answer that would
fail this must_not_contain.

The test isn't currently failing — the agent's narration of
"primary commodity" stays tight. But the assertion is one
prompt tweak away from a false negative.

**Recommended treatment**: leave it for now. If it surfaces as
a regression in a future phase, swap to a more specific
`must_not_contain` like `"primary commodity is gold"` /
`"primary commodity is copper"`. Document the brittleness in
the test comment.

---

## 3. gq-009-holes-in-2022 — **WORKING** but adjacent

```python
"expected_answer_contains": ["3"],
"must_not_contain": ["4 holes", "2 holes", "two holes"],
```

Reality: 2022 drill year has 3 holes (PLS-22-08, -09, -10). ✓

The substring `"4 holes"` is fine in isolation, but if the agent
narrates "10 holes drilled in 2024" or "4 holes in 2020", the
must_not_contain might trip on those neighbouring counts. The
proactive-insights gate (Phase 26) protects against most of this,
but the assertion is fragile.

**Recommended treatment**: keep as-is. The narrative phrasing
hasn't tripped it through 12+ phases. Watch for regressions.

---

## 4. No other obvious staleness

Scanned the remaining tests (gq-001 through gq-030). All other
specs that depend on collar/sample counts use the post-Phase-17
fixture values correctly (20 holes, 360.8 avg depth, PLS-22-08
as deepest, PLS-21-06 as shallowest, etc.).

---

## 5. Proposed Phase 31 scope

If/when Phase 30 (cache pipeline + cache_skipped_reason) lands,
Phase 31 picks up this audit as a one-step phase:

- Apply the gq-006 fix from Section 1
- Add an inline comment to gq-022 noting the brittleness
- Verifier confirms `expected_answer_contains` for gq-006 reads
  `"19"` and run a single cold pass to confirm no regression

Estimated scope: ~10 LOC, one verifier, one handoff. Smaller
than any other phase in the autonomous run.

End of staleness audit.
