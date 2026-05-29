# Phase 17 Implementation Kickoff — Golden-test pass-rate investigation (R-P14-3)

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase14_r-p13-1_scoping.md`,
`docs/phase11_golden_baseline.md`, `docs/roadmap_phase16_onward.md`
(Path A.1).

---

## 1. Theme

Phase 15 closed the MV-refresh-drift root cause for R-P13-1.
Phase 17 picks up R-P14-3 — the "even with MV populated, pass
count fluctuates 2-13" problem.

Diagnostic run at Phase 17 open confirmed the actual story: the
agent **is working**. It returns correct answers grounded in the
fixture. The failures are deterministic mismatches between the
Phase 13 fixture content and the test's documented "ground truth":

| Failure class | Example | Root cause |
|---------------|---------|------------|
| Wrong-number | gq-001 expects "20", fixture yields "10" | Test assumes Milestone-2 XLS-24-* fixture too (10 + 10 = 20) |
| Wrong-avg | gq-007 expects "360.8", fixture yields "348.0" | PLS-* depths sum to 3480, not 3608 |
| Wrong-commodity | gq-022 expects "uranium", fixture says "silver" | Phase 13 picked "Phantom Lake Silver" placeholder |
| Wrong-region | gq-024 expects "Athabasca", fixture says "Athabasca Basin, Northern Saskatchewan" | Test substring matches; should pass actually |
| Missing-graph | gq-011 expects "Triple R" deposit | Neo4j entity not seeded |
| Missing-assay | gq-014 expects "52" U3O8 | Assay fixture not seeded |
| Missing-litho | gq-015 expects "SST", "PGN" | Lithology fixture not seeded |
| Confidence-low | gq-003 0.1 < 0.8 threshold | Agent uncertain; needs more grounding context |

Phase 17 unlocks the engineering-shaped wins (XLS-24-* fixture,
PLS-* depth recompute, project metadata) and defers the
SME/Neo4j/assay/lithology fixtures to follow-on phases.

---

## 2. Locked decisions

| ID | Item | Phase 17 status |
|----|------|---------------|
| **R-P14-3.1** | Add XLS-24-* Milestone-2 collar fixture | **In scope (Step 2)** |
| **R-P14-3.2** | Recompute PLS-* depths so 20-hole avg = 360.8m | **In scope (Step 2)** |
| **R-P14-3.3** | Update project metadata (uranium commodity, region trim) | **In scope (Step 2)** |
| **R-P14-3.4** | Neo4j entity fixtures (Triple R, Sarah Thompson, CGL/GPT) | Defer — needs investigation of Neo4j ingestion path |
| **R-P14-3.5** | Assay + lithology fixtures | Defer — schema bigger lift |
| **R-P14-3.6** | Test assertion relaxations | Defer — should not change source-of-truth tests without SME |

---

## 3. Done definition

- Step 1 verifier: failure-audit doc captures the 8 failure
  classes above + each test's class assignment.
- Step 2 verifier: `silver.collars` has 20 rows under TEST_PROJECT_ID
  (10 PLS-* + 10 XLS-24-*); MV `total_collars`=20, `avg_depth`=360.8;
  project `commodity`='uranium'.
- Step 3 verifier: live golden suite produces ≥10 passing tests
  (up from 1-2 floor). New floor locked in `phase11_golden_baseline.md`.
- All prior verifiers still green (411 → ~430+).

---

## 4. Step-by-step

### Step 1 — Failure-audit doc
Run the suite once, categorise every failure. Document in
`docs/phase17_golden_failure_audit.md`. No code change yet.

### Step 2 — Engineering-fixture unlocks
- New migration: `database/raw/phase17/10-golden-fixture-extensions.sql`
  - INSERT 10 XLS-24-01..XLS-24-10 collars (Milestone-2 fixture)
  - UPDATE PLS-* depths so 20-hole avg = 360.8m (3608m total)
  - UPDATE `silver.projects` SET commodity='uranium', region='Athabasca Basin'
  - REFRESH MATERIALIZED VIEW silver.mv_collar_summary
- Idempotent (ON CONFLICT DO NOTHING for collars; UPDATE for project).

### Step 3 — Re-baseline + new floor
- Run the suite, count passes.
- Update `docs/phase11_golden_baseline.md` with the new floor.
- Update `phase11_step2_verify.sh` + `phase11_step4_verify.sh` to
  the new floor IF the count is reliable (3 consecutive runs ≥ N).
- Output: `docs/phase17_golden_baseline_v2.md` summarising the
  Phase 13 → Phase 17 unlock path.

### Step 4 — Handoff

---

## 5. Engineering invariants

- No changes to test source — fixture catches up to the tests'
  source-of-truth expectations.
- New floor only locks if 3 consecutive runs hit it reliably.
- The Step 2 SQL is appended to the rollup builder's
  phase[0-9]* scan; phase4_step7 rollup verifier picks it up.

---

## 6. Files of record (preview)

```
database/raw/phase17/10-golden-fixture-extensions.sql              (Step 2)
docs/phase11_golden_baseline.md                                    (mod — Step 3)
docs/phase17_golden_baseline_v2.md                                 (Step 3)
docs/phase17_golden_failure_audit.md                               (Step 1)
docs/phase17_handoff.md                                             (Step 4)
docs/phase17_implementation_kickoff.md                             (this file)
scripts/phase17_master_sweep.sh                                    (Step 4)
scripts/phase17_step1_verify.sh                                    (Step 1)
scripts/phase17_step2_verify.sh                                    (Step 2)
scripts/phase17_step3_verify.sh                                    (Step 3)
```

---

End of Phase 17 kickoff.
