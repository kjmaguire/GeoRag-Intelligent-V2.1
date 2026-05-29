# Phase 14 Step 3 — R-P13-1 scoping (intermittent agent refusal)

**Document version:** 1.0
**Status:** Root cause identified + fix proven during Phase 14 Step 3.
**Predecessors:** `docs/phase13_handoff.md`,
`docs/phase11_golden_baseline.md`.

---

## 1. The observation

Phase 13 Step 4 baseline run:

| Run | Elapsed | Passed | Failed |
|-----|---------|-------:|-------:|
| Cold (post-fixture-seed) | ~48 s | **13** | 22 |
| Warm (same session, no schema change) | ~6 s | 2 | 33 |
| Warm (next attempt) | ~7 s | 2 | 33 |

The cold run did 35 LLM round-trips (~1.4 s/test). The warm runs
skipped the LLM entirely — agent short-circuited to refusal text.

Refusal-text sample from `gq-001-count-holes`:
> "I don't have that number in this project [DATA-1]."

The framework + fixture are fine. Something between the cold run
and the warm runs flipped a switch that hid the data from the
agent's prompt-building path.

---

## 2. Root cause — found

`src/fastapi/app/agent/orchestrator.py:1244` defines
`_build_project_facts(project_id, pg_pool)`. It reads from the
`silver.mv_collar_summary` materialized view to populate the
**HIGH-CONFIDENCE SUMMARIES** block in the system prompt context.

The agent's NUMERIC system-prompt variant (orchestrator.py:843+)
says:
> If the summaries block is absent, say
> "I don't have that number in this project."

`silver.mv_collar_summary` is **not auto-refreshed** when
`silver.collars` is INSERTed. The Phase 13 fixture seed wrote 10
collars to the table but the MV's project_id row stays empty
(`WHERE project_id = '019d74a1...' returns 0 rows`) until something
explicitly `REFRESH MATERIALIZED VIEW`s it.

So between Phase 13's cold run (when the MV happened to be empty
of OTHER projects too) and the warm runs (state unchanged), the
agent looked up the MV, found no row for our test project, omitted
the summaries block, and the LLM dutifully said "I don't have that."

**Proven during Phase 14 Step 3:** running
`REFRESH MATERIALIZED VIEW silver.mv_collar_summary;` immediately
flipped the warm run from 2/31 passing back to **12/31 passing**
(48 s elapsed → real LLM round-trips).

---

## 3. Why the cold-run peak was 13 and not 12

The cold-run peak counted `test_pgeo_*` metadata too; warm runs
only ran the milestone-1 file. Aligned, both are 12 parameterised
agent-tests passing under the populated MV.

---

## 4. The fix

Add `REFRESH MATERIALIZED VIEW silver.mv_collar_summary;` to the
Phase 13 fixture seed migration. Applied at the end of
`database/raw/phase13/10-golden-collars-fixture.sql`, it ensures
that any rerun of the fixture path leaves the MV in a state the
agent can find.

A more robust solution (out of scope for this Phase 14 doc):
- A nightly Hatchet workflow that refreshes the MV — pairs with
  the existing audit-ledger / flow-key-reaper crons.
- An after-insert trigger on `silver.collars` that schedules a
  background refresh — less safe under heavy ingestion churn
  (concurrent refresh trade-offs).

---

## 5. Hypotheses to investigate further (Phase 15+)

Even with the MV refresh fix, two unexplained behaviours remain:

1. **22 of 35 still fail** even with the MV populated. Most of
   these are LLM-determinism mismatches (e.g. average response
   says "approximately 350m" but the test expects "364"). Improving
   that pass rate is RAG-quality work, not infrastructure.

2. **The MV is empty at random times.** It populates fresh on the
   first run after a REFRESH but the silver pipeline doesn't have
   a documented refresh cadence. Dagster's ingestion pipeline is
   expected to refresh the MV after every ingestion batch, but
   neither Dagster nor a refresh cron is observably running in
   the dev environment.

---

## 6. Recommended Phase 15+ scope

- **R-P14-1** — add the MV refresh to the Phase 13 fixture migration
  (closed in this Phase 14 Step 3 itself).
- **R-P14-2** — add a nightly Hatchet workflow `mv_refresh_silver`
  that refreshes `silver.mv_collar_summary` + any other agent-prompt
  MVs that exist. Pattern matches `flow_jwt_key_reaper` from Phase 7
  Step 2.
- **R-P14-3** — golden-test pass-rate improvement: investigate the
  remaining 19-23 failures with the MV populated. Likely a mix of
  LLM phrasing (loosen assertions) and missing fixtures (lithology
  intervals, assay samples) that the existing tests retrieve.

---

End of R-P13-1 scoping.
