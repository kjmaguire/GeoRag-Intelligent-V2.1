# Phase 29 Handoff — populate_neo4j fix + downhole cache bypass

**Document version:** 1.0
**Status:** Phase 29 complete. Phase 30+ open scope.
**Predecessors:** `docs/phase28_handoff.md`,
`docs/phase29_implementation_kickoff.md`,
`docs/retrospective_16_28.md`.

---

## 1. What Phase 29 delivered

Two paired cleanup fixes against the Phase 28 carry-over list:

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `src/fastapi/scripts/populate_neo4j.py` — Report title now suffixed with the first 8 chars of `report_id` so the `:Report.title` uniqueness constraint doesn't trip when silver.reports has many rows under one project all titled "NI 43-101 Technical Report". MERGE key (`report_id`) was already correct; only the SET-time title needed disambiguation. (R-P19-POPULATE) | `scripts/phase29_step1_verify.sh` checks 1+2+5 |
| 2 | `src/fastapi/app/agent/orchestrator.py` — cache shortcut bypassed when `categories.downhole=True`. `query_downhole_logs` was never wired into the RRF candidate list (Phase 24 added qdrant + neo4j + postgis), so a warm-state cache hit for a downhole-classified query silently dropped the lithology intervals — gq-015's variance root cause. Forcing a cache miss on downhole=True queries lets the dispatch path re-run the tool fresh. (R-P28-VARIANCE) | `scripts/phase29_step1_verify.sh` checks 3+4+6 |
| 3 | This handoff + master sweep | — |

---

## 2. The two fixes, in detail

### Fix 1 — populate_neo4j Report uniqueness

Before:

```python
await session.run(
    "MERGE (rpt:Report {report_id: $rid}) "
    "SET rpt.title = $title, ...",
    rid=r["report_id"], title=r["title"], ...
)
```

The `MERGE` key is the unique `report_id`, but the `SET rpt.title = $title`
trips the `:Report.title` uniqueness constraint when many silver.reports
rows share the same title. Phase 19 sidestepped it by writing a focused
entity-seed Cypher; populate_neo4j.py itself stayed broken.

After:

```python
unique_title = f"{r['title']} ({r['report_id'][:8]})"
# ... title = unique_title
```

Run output:

```
INFO        Report                    43 nodes
INFO        HAS_REPORT                43 rels
INFO        AUTHORED_BY               6 rels
INFO        Done.
```

Zero `ConstraintError`s — the populate script now completes end-to-end
under the Phase 19 silver.reports fixture state.

### Fix 2 — Downhole cache bypass

Tracing gq-015 (R-P28-VARIANCE) revealed:

```
cache rehydrated docs=2 graph=0 collars=20 into tool_results
context_chars=5905 approx_tokens=1476
llm_text='I don't have data on that in this project.'
```

Categories had `downhole: True` and `downhole_hole_ids: ['PLS-20-01']`,
so `query_downhole_logs` should have surfaced the 4 lithology intervals
(OVB/SST/PGN/GNT). It didn't — the cache hit replaced fresh tool
dispatch with the cached candidate set, which contains zero downhole
intervals because `DownholeLogsResult` was never wired into the
`_fused_candidates` RRF list.

Surgical fix: when `categories.get("downhole")` is True, ignore the
cache hit and let retrieval run. Cleaner than extending the cache
pipeline to a fifth store type (which would also have needed schema
changes to `CachedRetrievalCandidate.source_store`).

Phase 30+ candidate for full fix: extend the cache pipeline to handle
downhole — adds `source_store: "downhole"` and serialises
`LithologyInterval` payloads in `candidate.payload`.

---

## 3. Cold-run pass count

| Phase | Cold | Notes |
|-------|-----:|------|
| 28 | 30 | NI 43-101 chunks (peak with gq-015 variance) |
| **29** | **30** | gq-015 now stable across cold + warm; gq-014 surfaces as new variance edge |

The pass count didn't move — but the *consistency* did. Across cold +
warm in this phase's verification, gq-015 passed both. The remaining
variance edge has shifted to gq-014 (assay phrase-rendering, already
documented since Phase 22).

---

## 4. Carry-overs for Phase 30+

| ID | Item | Priority |
|----|------|----------|
| R-P28-FASTAPI-OOM | back-to-back load OOMs | Medium |
| R-P29-DOWNHOLE-CACHE | extend cache pipeline to include `DownholeLogsResult` instead of bypassing | Low — current bypass is correct, this is the "proper" fix |
| R-P15-1 | Bundled orchestrator prompts migration | Low |
| R-P21-CACHE-TELEMETRY | Already at INFO; remaining work is `cache_hit_of_run_id` surface on `answer_runs` | Low |
| R-P11-B | Frontend Search/Query page | Medium — first user-facing surface |

The structured-fixture / agent-prompt / infra-bug surface that drove
Phases 18-29 is now empty. Future phases will be feature additions
(R-P11-B frontend, R-P15-1 prompt migration) or environment hardening
(R-P28-FASTAPI-OOM).

---

## 5. Files of record

```
src/fastapi/scripts/populate_neo4j.py       (Step 1 — unique_title disambiguator)
src/fastapi/app/agent/orchestrator.py       (Step 2 — downhole cache bypass)
docs/phase29_implementation_kickoff.md      (kickoff doc, drafted during P28 sweep gap)
docs/phase29_handoff.md                      (this file)
docs/retrospective_16_28.md                  (retrospective, also drafted during P28 sweep gap)
scripts/phase29_master_sweep.sh
scripts/phase29_step1_verify.sh
```

---

## 6. Re-running

```bash
bash scripts/phase29_step1_verify.sh   # populate + cache bypass + cold golden
bash scripts/phase29_master_sweep.sh   # Phase 0 → 29
```

End of Phase 29 handoff.
