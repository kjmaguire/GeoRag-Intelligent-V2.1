# Phase 29 Retrospective Addendum

**Document version:** 1.0
**Status:** Snapshot at Phase 29 close.
**Predecessors:** `docs/retrospective_16_28.md`, `docs/phase29_handoff.md`.

A short append-only update to the Phase 16-28 retrospective. Phase 29
closed with the same cold-run peak (30/31) but **stabilised the
remaining variance edge** (gq-015) and **fixed a long-broken cleanup
script** (populate_neo4j.py). It also reclassified two carry-overs
from the Phase 28 handoff after live inspection.

---

## 1. New row on the cumulative trajectory

| Phase close | Verifiers | Cold-run golden | Notes |
|------------:|----------:|----------------:|-------|
| 28 | 86 | 30 | NI 43-101 chunks |
| **29** | **87** | **30** | **gq-015 variance closed; populate_neo4j fixed** |

Cold/warm parity at 30/31 with the Phase 29 verifier reproducibly hitting
cold=30 + warm=30 (where Phase 28 saw warm-side gq-015 variance).

---

## 2. What Phase 29 delivered

Two small fixes against the Phase 28 carry-over list:

- **R-P19-POPULATE** — `src/fastapi/scripts/populate_neo4j.py` now appends
  the first 8 chars of `report_id` to the Neo4j `:Report.title` field
  so the uniqueness constraint doesn't trip when silver.reports has many
  rows under one project sharing the same title. Verified end-to-end:
  43 Report nodes + 6 AUTHORED_BY rels written without `ConstraintError`.

- **R-P28-VARIANCE** — traced gq-015's intermittent failures to a real
  bug. The cache pipeline that Phase 24 built covers qdrant + neo4j +
  postgis but `query_downhole_logs` was never wired in. Any warm-state
  cache hit on a downhole-classified query silently dropped the
  lithology intervals — and gq-015 ("Summarise the lithology
  intersections for drill hole PLS-20-01") is exactly that kind of
  query. Surgical fix in `orchestrator.py`: bypass the cache shortcut
  when `categories.downhole=True`. The dispatch path then re-runs the
  downhole tool fresh.

---

## 3. Two carry-overs reclassified

During the Phase 29 sweep gap, live inspection found:

- **R-P21-CACHE-TELEMETRY** — largely done already. The
  `silver.answer_runs.cache_hit_of_run_id` column exists and is being
  populated (97 hits / 418 misses in the last hour). CACHE HIT/MISS log
  lines are at INFO. The remaining work — a `cache_skipped_reason`
  column — is a small observability nicety scoped into Phase 30.

- **R-P28-FASTAPI-OOM** — **misdiagnosed**. fastapi has 10 GiB allocated
  and uses ~3.4 GiB steady-state. `OOMKilled=false` across the
  container's lifetime. The "restart loop" observed during Phase 28
  verification was the test harness racing the BGE-small + SPLADE++
  load window after `docker restart`, not an out-of-memory kill. The
  90–100s `sleep` in every verifier mitigates it. **Closed as no-op.**

---

## 4. What remains

The fixture/agent/infra goal-list driving Phases 18–29 is essentially
exhausted at 30/31 cold-run pass rate. Remaining items are observability
+ architectural cleanup + new features (Phase 30 scope drafted in
`docs/phase30_implementation_kickoff.md`):

| ID | Item | Priority |
|----|------|----------|
| R-P29-DOWNHOLE-CACHE | Extend cache pipeline to include `DownholeLogsResult` properly (replaces Phase 29's surgical bypass) | Low — bypass is correct, this is the architecturally clean version |
| R-P21-CACHE-SKIPPED-REASON | New `silver.answer_runs.cache_skipped_reason` column + orchestrator wire | Low — small DDL + ~10 LOC |
| R-P15-1 | Bundled orchestrator prompts migration | Medium |
| R-P11-B | Frontend Search/Query page | Medium — first user-facing surface |

The Phase 30 kickoff doc + diff note pre-stage R-P29-DOWNHOLE-CACHE and
R-P21-CACHE-SKIPPED-REASON as ready-to-apply work.

---

## 5. Session-level totals at Phase 29 close

- **12 phases delivered** (18 → 29) across one continuous autonomous
  run.
- **Cold-run golden trajectory: 13 → 30** (+17 absolute) on a 31-test
  suite.
- **Three central infrastructure root causes solved**:
  R-P14-3.7 (warm-state cache poison, Phase 21),
  R-P23-CACHE-REHYDRATE (broken cache rehydration, Phase 24),
  R-P24-VLLM-PAYLOAD-CAP (vLLM context cliff, Phase 25).
- **All 87 step verifiers** introduced this run pass 100%; only the
  pre-existing `phase9_step1` docker-network-name mismatch fails on
  the master sweep (documented carry-over from before Phase 18).

End of Phase 29 addendum.
