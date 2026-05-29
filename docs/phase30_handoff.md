# Phase 30 Handoff — Full cache pipeline coverage + skipped-reason telemetry

**Document version:** 1.0
**Status:** Phase 30 complete. The golden test suite passes 31/31.
**Predecessors:** `docs/phase30_implementation_kickoff.md`,
`docs/phase30_implementation_diff_note.md`,
`docs/phase29_handoff.md`.

---

## 1. Result

**31 / 31 cold-run pass.** First time the golden suite has hit a
clean 100% pass since it was introduced.

```
$ docker exec georag-fastapi pytest --tb=no -q /app/tests/test_golden_queries.py
============= 31 passed, 1 warning in 106.07s (0:01:46) ===========
```

Cumulative trajectory from session start: **13 → 31** (+18
absolute) across Phases 18–30.

---

## 2. What Phase 30 delivered

The pre-staged diff stack from `phase30_implementation_diff_note.md`
landed in 7 blocks, plus one diagnostic catch:

| Block | Output |
|------|--------|
| A | `_call_openai_compatible_llm`'s RRF list-builder gets a 4th branch for `query_downhole_logs` — each `LithologyInterval` becomes a `Candidate(store="downhole")`. |
| B | Cache-write candidate loop gets a 4th `elif _cand.store == "downhole":` branch that captures the full `LithologyInterval` dataclass via `dataclasses.asdict`. |
| C | Cache-read rehydration gets a 4th bucket (`_lithology_intervals`) + a `DownholeLogsResult` rebuild appending into `tool_results`. Falls back to a tool-rerun if any cached candidate lacks its payload. |
| D | `LithologyInterval` added to the `from app.agent.tools import (…)` block. |
| E | Phase 29's surgical bypass (`if categories.get("downhole"):`) removed — no longer needed now that downhole is a first-class store in the cache. |
| F | `silver.answer_runs.cache_skipped_reason` column added with a CHECK-constrained enum (`zero_candidates`, `partial_failures`, `downhole_bypass_legacy`, `schema_validation_failed`), plus orchestrator wiring + `AnswerRunCreate.cache_skipped_reason` field + `answer_run_store.insert_answer_run` INSERT update. |
| G | Analytics parity at `silver.answer_retrieval_items` insert site — adds a 4th `elif _cand.store == "downhole":` so dashboard queries can break down retrieval by all 4 stores. |
| (bonus) | Diagnostic fix: `DownholeLogsResult.__init__` requires `count` + `data_source`. First-cut rehydration omitted them and the rebuild crashed with `TypeError`, surfaced as "cache rehydration crashed — falling back to tool rerun" in the log. Fix added both fields to the cache-hit constructor with `data_source="PostGIS silver.lithology_logs (cache hit)"`. |

---

## 3. The diagnostic moment

The first run after applying Blocks A-G had this in the logs:

```
ERROR run_deterministic_rag: cache rehydration crashed — falling
back to tool rerun
Traceback: ...
TypeError: DownholeLogsResult.__init__() missing 2 required
positional arguments: 'count' and 'data_source'
```

The Phase 24 rehydration's `_abort_rehydrate` + try/except path
caught the crash and degraded gracefully (tool rerun, identical to
Phase 29's behaviour), so nothing user-visible broke — but gq-015
kept silently re-running tools on every cache hit. Adding the two
missing fields to the `DownholeLogsResult(...)` constructor flipped
the rehydration from "crash → retry tools" to "rehydrate cleanly →
serve from cache".

Phase 24 paid for this resilience: the rehydration wrapper at
line 4237 explicitly catches `Exception`, sets `_cache_hit = False`,
clears `tool_results`, and lets the standard tool-execution path
run. That's why my missing-field bug didn't surface as user-facing
breakage — only as a silent cache miss that the verifier's
"gq-015 standalone" check caught.

---

## 4. Cumulative session trajectory

| Phase | Cold-run | Δ | Theme |
|-------|---------:|--:|-------|
| 13 (baseline) | 13 |  — | Phase 13 fixture seeded |
| 17 | 15 | +2 | 20-hole fixture + uranium commodity |
| 18 | 16 | +1 | assay + lithology + MV cartesian-join |
| 19 | 19 | +3 | Neo4j entity seed |
| 20 | 19 |  0 | SELF-row property surface (structural) |
| 21 | 20 | +1 | **warm-state cache poison fix** |
| 22 | 24 | +4 | prompt coaching + confidence calc |
| 23 | 22 | -2 | investigation only (no code) |
| 24 | 23 | +1 | vLLM resilience + cache rehydration paired fix |
| 25 | 25 | +2 | **vLLM context cliff fix** |
| 26 | 27 | +2 | factoid insights gate + stale-test corrections |
| 27 | 28 | +1 | collar azimuth surface + off-topic refusal |
| 28 | 30 | +2 | **NI 43-101 chunk seed + doc classifier** |
| 29 | 30 |  0 | populate_neo4j fix + downhole cache bypass (stability) |
| **30** | **31** | **+1** | **full cache pipeline coverage — last test (gq-015) unlocks via cache rehydration** |

Cumulative: **13 → 31 across 13 phases**. The 31/31 result is the
natural ceiling — every test in the suite passes.

---

## 5. What's now in place

The orchestrator's retrieval cache is feature-complete:

- **All 4 stores** (qdrant, neo4j, postgis, downhole) round-trip
  through the cache pipeline. Each carries its full dataclass
  payload via `dataclasses.asdict`.
- **Phase 21 poison guard** still gates the write — empty results
  + partial failures don't poison the cache.
- **Phase 24 rehydration** rebuilds typed `tool_results` entries
  on hit, with a graceful fallback when a cached entry is
  pre-Phase-30 (no downhole payload) or has a schema mismatch.
- **Phase 30 observability** records the skip reason in
  `silver.answer_runs.cache_skipped_reason` so dashboard queries
  can break down cache health without log archaeology.

Three central infrastructure root causes solved this session:
R-P14-3.7 (warm-state cache poison, Phase 21), R-P23-CACHE-REHYDRATE
(broken cache rehydration, Phase 24), R-P24-VLLM-PAYLOAD-CAP (vLLM
context cliff, Phase 25). Plus all 31 fixture/agent/test surfaces
needed for the full golden suite.

---

## 6. Carry-overs

The fixture / agent / cache work that drove Phases 18–30 is
exhausted. Remaining items are all out-of-scope for this autonomous
run shape:

| ID | Item | Priority |
|----|------|----------|
| R-P31-STALE-AUDIT | Apply the `phase31_test_staleness_audit.md` fix: gq-006 expects "9" but reality is 19 (currently passing on substring) | Low — gq-006 currently passing; correction is a cleanup |
| R-P15-1 | Bundled orchestrator prompts migration | Medium |
| R-P11-B | Frontend Search/Query page | Medium — first user-facing surface |
| R-P21-CACHE-TELEMETRY-DASHBOARD | Wire `cache_skipped_reason` into the operator dashboard | Low |

The Phase 31 audit doc + the Phase 30 cache_skipped_reason
groundwork pre-stage R-P31-STALE-AUDIT as a tiny next phase if
desired.

---

## 7. Files of record

```
database/raw/phase30/10-cache-skipped-reason.sql              (Step 1F — DDL)
src/fastapi/app/agent/orchestrator.py                          (Blocks A, B, C, D, E, F, G)
src/fastapi/app/services/answer_run_store.py                   (Step 1F — INSERT column wiring)
src/fastapi/app/models/answer_run.py                           (Step 1F — Pydantic field)
docs/phase30_implementation_kickoff.md                         (Step 0)
docs/phase30_implementation_diff_note.md                       (Step 0 — exact diffs)
docs/phase30_handoff.md                                         (this file)
scripts/phase30_master_sweep.sh                                (Step 3)
scripts/phase30_step1_verify.sh                                (Step 2)
```

---

## 8. Re-running

```bash
bash scripts/phase30_step1_verify.sh   # 9/9 — incl. full cold-run + gq-015 standalone
bash scripts/phase30_master_sweep.sh   # Phase 0 → 30 sweep
```

End of Phase 30 handoff.
