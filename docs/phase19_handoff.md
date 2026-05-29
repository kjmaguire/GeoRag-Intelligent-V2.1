# Phase 19 Handoff — Neo4j entity fixtures (R-P14-3.4)

**Document version:** 1.0
**Status:** Phase 19 complete. Phase 20 inheriting.
**Predecessors:** `docs/phase18_handoff.md`,
`docs/phase19_implementation_kickoff.md`,
`docs/phase19_golden_baseline_v4.md`.

---

## 1. What Phase 19 delivered

Phase 19 closed `R-P14-3.4` — the Neo4j knowledge-graph fixture
gap. Pure fixture phase; no agent or app code changes.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `database/raw/phase19/10-author-seed.sql` — adds Sarah Thompson + David Chen to one PLS NI 43-101 report; gates further updates so re-runs are no-ops | `scripts/phase19_step1_verify.sh` (3/3) |
| 2 | `database/raw/phase19/20-neo4j-entities.cypher` — focused entity seed: `:Project`, `:Deposit Triple R`, `:Report`, `:QualifiedPerson Sarah Thompson`, `:Formation CGL/GPT`, `:MineralOccurrence`, plus all binding edges | `scripts/phase19_step2_verify.sh` (9/9) |
| 3 | `docs/phase19_golden_baseline_v4.md` — cold-run peak 19/31 documented + Phase 19 unlocks (gq-011 + gq-012) + Class D shrink from 5→2 + new Class A3 (graph property surface) for gq-013/018 | `scripts/phase19_step3_verify.sh` (5/5) |
| 4 | This handoff + `scripts/phase19_master_sweep.sh` | `scripts/phase19_step4_verify.sh` |

**Phase 19 cumulative: 3 + 9 + 5 + ? = 17+ verifier checks** across
4 verifiers.

---

## 2. Cold-run pass count progression

| Phase | Total | Pass | Notes |
|-------|------:|-----:|-------|
| 13 | 35 | 13 | Phase 13 cold-run peak |
| 17 | 31 | 15 | +2 (20-hole + uranium fixture) |
| 18 | 31 | 16 | +1 (gq-015 lithology unlock) |
| **19** | **31** | **19** | **+3 (gq-011 + gq-012 graph unlocks)** |

---

## 3. Why the focused cypher and not `populate_neo4j.py`?

`src/fastapi/scripts/populate_neo4j.py` does a broader ingest pass
that hits the `:Report.title` uniqueness constraint when
`silver.reports` has 42 rows under one project all with the same
title `"NI 43-101 Technical Report"` (the partial seed from earlier
phases). It successfully seeds projects, drillholes, formations,
and the first report before the constraint blows up.

Phase 19's `20-neo4j-entities.cypher` is a narrow, idempotent
top-up: it adds the exact entities + edges the golden tests
inspect, with a project-specific report title to dodge the
uniqueness conflict. It also relies on `populate_neo4j.py`'s
already-created DrillHole + Formation nodes when seeding the
INTERSECTS / HAS_FORMATION edges, but degrades to no-op edges
if those don't yet exist.

A future Phase 20+ task: fix `populate_neo4j.py` to MERGE on
`report_id` not `title`, or relax the constraint to
`(title, project_id)`. Out of scope for fixture-only Phase 19.

---

## 4. Carry-overs for Phase 20+

| ID | Item | Where | Priority |
|----|------|-------|----------|
| **R-P14-3.7** | Warm-run agent refusal investigation | orchestrator + agent state | **High** — central mystery, gates whether cold-run peak ever holds |
| **R-P19-A3** | Graph property surface — gq-013 / gq-018 | agent prompt or tool response shape (include `start` node props in traverse return) | **Medium-high** — 2-3 unlocks |
| **R-P19-DOC** | NI 43-101 PDF stub for gq-024/025/026 | `gold.documents` + chunk pipeline | **Medium-high** — 3 unlocks |
| **R-P19-POPULATE** | Fix `populate_neo4j.py` to handle duplicate report titles | `src/fastapi/scripts/populate_neo4j.py` | Medium — cleanup, not a test unlock |
| **R-P14-3.6** | Test assertion relaxations for phrase-fragile tests | `tests/test_golden_queries.py` | Medium |
| **R-P11-baseline-2** | Public-geoscience fixture | `public_geoscience.*` | Medium |
| **R-P15-1** | Bundled orchestrator prompts migration | `orchestrator.py` | Medium |
| **R-P11-B** | Frontend Search/Query page | `resources/js/Pages/` | Medium |

---

## 5. Files of record

**New in Phase 19:**

```
database/raw/phase19/10-author-seed.sql                            (Step 1)
database/raw/phase19/20-neo4j-entities.cypher                      (Step 2)
docs/phase19_implementation_kickoff.md                             (Step 0)
docs/phase19_golden_baseline_v4.md                                 (Step 3)
docs/phase19_handoff.md                                             (this file)
scripts/phase19_master_sweep.sh                                    (Step 4)
scripts/phase19_step1_verify.sh                                    (Step 1)
scripts/phase19_step2_verify.sh                                    (Step 2)
scripts/phase19_step3_verify.sh                                    (Step 3)
scripts/phase19_step4_verify.sh                                    (Step 4)
```

---

## 6. Re-running every Phase 19 verifier

```bash
bash scripts/phase19_step1_verify.sh   # author seed              (3/3)
bash scripts/phase19_step2_verify.sh   # Neo4j entity seed        (9/9)
bash scripts/phase19_step3_verify.sh   # baseline v4              (5/5)
bash scripts/phase19_step4_verify.sh   # handoff + sweep
```

Combined Phase 0 → Phase 19 sweep: `scripts/phase19_master_sweep.sh`
adds the four Phase 19 verifiers on top of Phase 18's 60-verifier
list. Expected: 64 verifiers green, modulo the two pre-existing
non-greens (`phase4_step7` sweep-flake, `phase9_step1` docker
network-name mismatch).

---

## 7. Phase 20 entry checklist

1. Read this handoff + `docs/phase19_golden_baseline_v4.md`.
2. Re-run `scripts/phase19_master_sweep.sh` — confirm green
   (within the documented carry-over non-greens).
3. Highest-leverage Phase 20 scope candidates:
   - **R-P19-A3** — graph property surface for `traverse_knowledge_graph`.
     Quick win: include `start` node props in the returned entity
     list so `deposit_type` reaches the LLM context. 2-3 unlocks.
   - **R-P19-DOC** — NI 43-101 chunk seed for gq-024/025/026.
     Need a `gold.documents` row + matching chunk in
     pgvector / Qdrant with `citation_type='NI43'`. 3 unlocks.
   - **R-P14-3.7** — warm-run agent state investigation. Highest
     mystery value but deepest debugging.

End of Phase 19 handoff.
