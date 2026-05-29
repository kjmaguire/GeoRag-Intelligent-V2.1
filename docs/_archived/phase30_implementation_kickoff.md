# Phase 30 Implementation Kickoff — closing observations + scope reset

**Document version:** 1.0 — DRAFT
**Status:** Active. Drafted during Phase 29 master sweep.
**Predecessors:** `docs/phase29_handoff.md`,
`docs/retrospective_16_28.md`.

---

## 1. Two carry-overs re-examined

Investigation during the Phase 29 sweep gap re-classified two
Phase 28 carry-overs:

### R-P21-CACHE-TELEMETRY — largely done

`silver.answer_runs.cache_hit_of_run_id` column **already exists**
(`uuid`, FK to answer_runs, indexed where NOT NULL). Live
inspection during the Phase 29 sweep shows it being populated
correctly:

```
SELECT count(*) FILTER (WHERE cache_hit_of_run_id IS NOT NULL) AS hits,
       count(*) FILTER (WHERE cache_hit_of_run_id IS NULL)     AS misses,
       count(*)                                                AS total
  FROM silver.answer_runs
 WHERE created_at > now() - interval '1 hour';

 hits | misses | total
------+--------+-------
   97 |    418 |   515
```

The CACHE HIT / CACHE MISS log lines are already at INFO
(verified line 3247 of orchestrator.py). The Phase 21 + Phase 24
work covered the bulk of this item.

**Remaining**: a small `cache_skipped_reason` column would let
operators distinguish "zero candidates" vs "partial failures"
vs "downhole bypass" without log archaeology. Low priority — the
log-based diagnosis path has worked fine through Phases 21-29.

### R-P28-FASTAPI-OOM — misdiagnosed

Phase 28's handoff noted "fastapi container OOMs when BGE+SPLADE+
vLLM all in-memory under back-to-back test load — restart loop
observed during Phase 28 verification". Live inspection:

```
mem=3.403GiB / 10GiB
memory_limit=10737418240
oom_killed_recently=false
restarts=0
```

The container has 10 GiB allocated and uses ~3.4 GiB steady-state.
No OOM-killer events. The "restart loop" during Phase 28 was the
test harness hitting fastapi during its 90-100 s warmup window
(BGE-small + SPLADE++ load on first run after `docker restart`),
not an out-of-memory kill.

Mitigation already in place: the verifiers use `sleep 90-100`
between `docker restart` and `pytest` invocations. The pattern
holds across Phase 29's verifier run (cold=warm=30 reproducibly).

**Closed as no-op.** Re-classified as "warmup timing, not memory".

---

## 2. Genuinely open work

| ID | Item | Priority | Scope estimate |
|----|------|----------|----------------|
| **R-P29-DOWNHOLE-CACHE** | Extend cache pipeline to include `DownholeLogsResult` properly (current Phase 29 fix bypasses cache for downhole queries) | Low — bypass is correct, this is the architecturally-clean version | 60-80 LOC: add `source_store: "downhole"`, serialise `LithologyInterval` payloads, rebuild on read |
| **R-P15-1** | Bundled orchestrator prompts migration — the 10 inline `_SYSTEM_PROMPT_*` constants in orchestrator.py need to move to `prompts/` per the canonical pattern Phase 13-15 established | Medium | 200-400 LOC + version registry entries |
| **R-P11-B** | Frontend Search/Query page — first user-facing RAG surface | Medium | Larger — Inertia page + chat UI + SSE plumbing |
| **R-P21-CACHE-SKIPPED-REASON** | New `silver.answer_runs.cache_skipped_reason` column + orchestrator wiring | Low | DDL migration + ~10 LOC |

---

## 3. Recommended Phase 30 scope

The fixture/agent/infra work is exhausted at 30/31 cold-run with
gq-014 the remaining variance edge (phrase-rendering, R-P14-3.6,
already documented as "tests should be source of truth, not target").

Two paths for Phase 30:

**(a) Polish + close out the autonomous run**: implement
R-P29-DOWNHOLE-CACHE properly (cache covers all 4 stores),
add R-P21-CACHE-SKIPPED-REASON, update retrospective. Stays in
the same shape as Phases 18-29.

**(b) Pivot to new ground**: start R-P15-1 (prompts migration)
or R-P11-B (frontend). Either is bigger than a single autonomous
phase. Better with explicit user direction.

Default recommendation: **(a)** — finishes the cycle started by
Phase 24's cache rehydration in a clean, testable way. Path (b)
deserves a fresh user-driven session.

---

## 4. Done definition (if Phase 30 takes path (a))

- Cache write captures `DownholeLogsResult` candidates with
  `source_store="downhole"` and full `LithologyInterval` payloads
- Cache read rebuilds `DownholeLogsResult` (including the
  `collar: CollarRecord` field) into `tool_results`
- The downhole-bypass guard in Phase 29 Step 2 is removed (or
  kept as defensive fallback)
- `silver.answer_runs.cache_skipped_reason` column added via
  Phase 5-style migration
- Cold = warm pass count holds at 30/31 with cache hits actively
  exercising downhole + reproducing identical answers
- All prior Phase 0-29 verifiers stay green on master sweep

---

## 5. Files of record (preview, path (a))

```
docs/phase30_implementation_kickoff.md             (this file)
docs/phase30_handoff.md                             (Step N)
src/fastapi/app/models/retrieval_cache.py          (modified — source_store enum)
src/fastapi/app/agent/orchestrator.py              (modified — cache write/read for downhole)
database/raw/phase30/10-cache-skipped-reason.sql   (new — DDL migration)
scripts/phase30_master_sweep.sh
scripts/phase30_step1_verify.sh
```

End of Phase 30 kickoff (draft).
