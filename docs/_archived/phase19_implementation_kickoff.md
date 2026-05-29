# Phase 19 Implementation Kickoff — Neo4j entity fixtures (R-P14-3.4)

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase18_handoff.md`,
`docs/phase17_golden_failure_audit.md`.

---

## 1. Theme

Phase 18 closed the assay + lithology fixture gap (R-P14-3.5).
Phase 19 picks up the remaining fixture-shaped carry-over:
**R-P14-3.4** — seed the Neo4j knowledge-graph entities the
agent's `traverse_knowledge_graph` + `query_graph_by_label` tools
expect to find for the test project.

Target unlocks (Class D from `phase17_golden_failure_audit.md`):

| Test | Expects | Needs |
|------|---------|-------|
| gq-011-graph-deposit | "Triple R" | `:Deposit {name:'Triple R'}` linked to project |
| gq-012-graph-qp | "Sarah Thompson" | `:QualifiedPerson {name:'Sarah Thompson'}` linked to a `:Report` |
| gq-013-graph-formations | "CGL", "GPT" | `:Formation {name:'CGL'}` + `:Formation {name:'GPT'}` linked to drill holes |
| gq-018-deposit-type | "unconformity" | `:Deposit.deposit_type = 'unconformity-related uranium'` |
| gq-025-qp-name (partial) | "Sarah Thompson" + NI43 citation | QP node + an NI 43-101 chunk (chunk-side deferred — only graph half this phase) |

---

## 2. Locked decisions

| ID | Item | Phase 19 status |
|----|------|---------------|
| **R-P14-3.4.A** | Reuse existing `src/fastapi/scripts/populate_neo4j.py` | **In scope (Step 2)** — already creates Project + DrillHole + Formation + Report + QP + Deposit + MineralOccurrence nodes from silver.* tables |
| **R-P14-3.4.B** | Seed `silver.reports.authors = {Sarah Thompson, ...}` on the test project's NI 43-101 row(s) | **In scope (Step 1)** — currently empty, blocks the QP node creation in populate_neo4j.py |
| **R-P14-3.4.C** | Seed `:Formation {name:'CGL'}` + `:Formation {name:'GPT'}` directly via migration | **In scope (Step 2 supplementary)** — these codes aren't in silver.lithology_logs, so populate_neo4j.py won't surface them |
| **R-P14-3.6** | Test assertion relaxations for gq-014/017 phrase-rendering | Defer — different concern; documented in Phase 18 handoff |
| **R-P14-3.7** | Warm-run agent refusal investigation | Defer — needs deep debug session |
| **NI43 chunk seed for gq-024/025/026** | Defer to Phase 20+ — document/PDF concern, not graph |

---

## 3. Done definition

- Step 1 verifier: `silver.reports` has ≥1 row under the test
  project's name with `authors` containing "Sarah Thompson".
- Step 2 verifier: Neo4j has a `:Deposit {name:'Triple R'}` with
  `deposit_type` containing "unconformity", a
  `:QualifiedPerson {name:'Sarah Thompson'}`, and `:Formation`
  nodes including `CGL` + `GPT` — all carrying the test
  project_id.
- Step 3 verifier: cold-run golden peak ≥ Phase 18's 16.
- Step 4 verifier: handoff + sweep green.
- All prior verifiers still green (within the known Phase 4 + 9
  flake/infra carry-overs from Phase 18 close).

---

## 4. Step-by-step

### Step 1 — Author seed on silver.reports

`database/raw/phase19/10-author-seed.sql`:
- UPDATE silver.reports SET authors = ARRAY['Sarah Thompson',
  'David Chen'] WHERE project_name = 'Patterson Lake South
  Property' AND (authors = '{}' OR authors IS NULL).
- Idempotent: only sets where authors is empty.

### Step 2 — Neo4j populate

`database/raw/phase19/20-neo4j-formations.cypher`:
- MERGE `:Formation {name:'CGL'}` + `:Formation {name:'GPT'}`
  with project_id + description; INTERSECTS to all
  `:DrillHole {project_id:$pid}`.

`scripts/phase19_populate_neo4j.sh`:
- Invoke `src/fastapi/scripts/populate_neo4j.py` inside the
  fastapi container.
- Then apply the formations cypher.

### Step 3 — Re-baseline

`docs/phase19_golden_baseline_v4.md`:
- Cold-run golden peak; record Class D unlocks delta from
  Phase 18 (16 → target 18-20).

### Step 4 — Handoff

`docs/phase19_handoff.md` + `scripts/phase19_master_sweep.sh`.

---

## 5. Files of record (preview)

```
database/raw/phase19/10-author-seed.sql                            (Step 1)
database/raw/phase19/20-neo4j-formations.cypher                    (Step 2)
docs/phase19_implementation_kickoff.md                             (this file)
docs/phase19_golden_baseline_v4.md                                 (Step 3)
docs/phase19_handoff.md                                             (Step 4)
scripts/phase19_master_sweep.sh                                    (Step 4)
scripts/phase19_populate_neo4j.sh                                  (Step 2)
scripts/phase19_step1_verify.sh                                    (Step 1)
scripts/phase19_step2_verify.sh                                    (Step 2)
scripts/phase19_step3_verify.sh                                    (Step 3)
scripts/phase19_step4_verify.sh                                    (Step 4)
```

End of Phase 19 kickoff.
