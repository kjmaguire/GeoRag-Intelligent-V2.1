# Phase 29 Implementation Kickoff — closing the autonomous run

**Document version:** 1.0 — DRAFT
**Status:** Active. Picked up after Phase 28's 30/31 cold-run peak.
**Predecessors:** `docs/phase28_handoff.md`,
`docs/retrospective_16_28.md`.

---

## 1. Theme

The golden-test pass-count goal that drove Phases 18-28 is
effectively exhausted at 30/31. The remaining gq-015 variance
slot is environment-shaped (vLLM cap + OOM under load), not a
new fixture/prompt gap. Phase 29's job is to wind down the
autonomous run cleanly: stabilise the remaining variance,
promote a key piece of observability that's been deferred for
five phases, and leave a clear pickup point for whoever drives
the next session.

---

## 2. Candidate scope

Three small, independent items. Pick all three if time permits;
pick just (a) + (b) if not.

### (a) R-P21-CACHE-TELEMETRY — promote cache hit/miss to INFO

`run_deterministic_rag: CACHE HIT key=… candidates=N` is currently
the only signal that cache behaviour is right. Phase 21 + 24 both
hinged on tracing this log to find their respective bugs, and both
took longer than necessary because the log line is at DEBUG.

Change: bump that log + the "skipping cache write" lines to INFO,
and surface `cache_hit_of_run_id` + `cache_skipped_reason` columns
on `answer_runs` (DB column already exists for the first; second
is new). Wire the orchestrator to populate them.

Estimated scope: 30-50 LOC + one Phase 5-shape DDL migration if
the column needs adding.

### (b) R-P28-VARIANCE — gq-015 stability

gq-015-lithology-narration was unlocked at Phase 18 and has been
in the passing set most runs since. It dropped out during Phase 28
verification. The query is "Summarise the lithology intersections
for drill hole PLS-20-01" — exercises `query_downhole_logs` which
returns a `DownholeLogsResult` with `intervals: list[LithologyInterval]`.

Hypothesis: under the Phase 25 dynamic vLLM cap, when the lithology
intervals + system prompt + project facts exceed the conservative
chars/2 estimate, `max_output` gets capped to 512 and the answer
truncates before emitting the "PLS-20-01" / "SST" / "PGN" tokens.

Investigation steps:
1. Run gq-015 alone, capture log line `vllm_output_cap: input~N tokens
   leaves M output budget`.
2. If M < 800, response truncation is the cause.
3. Mitigation options:
   - Cap the rendered intervals to top-N most-relevant
   - Truncate the `lithology_description` field per interval
   - Move project_preamble into a separate compact form

### (c) R-P19-POPULATE — populate_neo4j Report.title fix

Phase 19 discovered that `populate_neo4j.py` collides with the
`:Report.title` uniqueness constraint when silver.reports has 42
rows under one project all titled "NI 43-101 Technical Report".
Phase 19 sidestepped with a focused entity-seed Cypher. The
populate script is still broken.

Fix: change the MERGE key from `title` to `report_id` (which is
unique-by-construction). Re-run the populate script on a clean
Neo4j to validate.

Estimated scope: one MERGE statement change, one re-run.

---

## 3. Out of scope for Phase 29

- New fixtures, new entities, new tests — the pass count is at the
  natural ceiling for this fixture state.
- Frontend (R-P11-B) — bigger phase, separate session.
- Prompt migration (R-P15-1) — bigger phase, separate session.

---

## 4. Done definition

- (a) verifier: `CACHE HIT` log lines visible at INFO during a
  routine test run; `answer_runs.cache_hit_of_run_id` populated.
- (b) verifier: gq-015 passes on 3 consecutive cold runs.
- (c) verifier: `populate_neo4j.py` runs end-to-end without the
  uniqueness collision under the Phase 19 reports fixture.

Plus the usual handoff doc + master sweep.

---

## 5. Files of record (preview)

```
docs/phase29_implementation_kickoff.md   (this file)
docs/phase29_handoff.md                   (Step N)
src/fastapi/app/agent/orchestrator.py    (modified — telemetry promotion)
src/fastapi/scripts/populate_neo4j.py    (modified — Report.title→report_id)
scripts/phase29_master_sweep.sh
scripts/phase29_step1_verify.sh          (telemetry)
scripts/phase29_step2_verify.sh          (gq-015 stability)
scripts/phase29_step3_verify.sh          (populate_neo4j)
```

End of Phase 29 kickoff (draft — pending Phase 28 master sweep result).
