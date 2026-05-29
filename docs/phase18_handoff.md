# Phase 18 Handoff — Assay + lithology fixtures (R-P14-3.5)

**Document version:** 1.0
**Status:** Phase 18 complete. Phase 19 inheriting.
**Predecessors:** `docs/phase17_handoff.md`,
`docs/phase18_implementation_kickoff.md`,
`docs/phase18_assay_litho_schema_audit.md`,
`docs/phase18_golden_baseline_v3.md`.

---

## 1. What Phase 18 delivered

Phase 18 closed `R-P14-3.5` — the fixture-shaped carry-over after
Phase 17 wrapped up the engineering-shaped collar/project work.
Pure SQL fixture phase; no agent or app code changes.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `docs/phase18_assay_litho_schema_audit.md` — column + FK + workspace-id + JSON-shape audit for `silver.samples` + `silver.lithology_logs` | `scripts/phase18_step1_verify.sh` (5/5) |
| 2 | `database/raw/phase18/10-assay-litho-fixture.sql` — workspace link + 4 PLS-22-08 U3O8/Au samples + 4 PLS-20-01 lithology intervals + MV refresh; idempotent | `scripts/phase18_step2_verify.sh` (7/7) |
| 3 | (folded into Step 2 SQL) — lithology side of the same migration | `scripts/phase18_step3_verify.sh` (6/6) |
| 4 | `docs/phase18_golden_baseline_v3.md` — cold-run peak 16/31 documented + Phase 18 unlock (gq-015) named + non-unlocks (gq-014/017) reclassified as phrase-rendering carry-overs | `scripts/phase18_step4_verify.sh` (8/8) |
| 5 | `database/raw/phase18/15-fix-mv-collar-summary.sql` — fixes a pre-existing cartesian-join bug in `silver.mv_collar_summary` that Phase 18's first-ever non-empty samples + lithology fixtures surfaced. The MV's LEFT JOINs to samples + lithology multiplied the collar rows; `count(c.collar_id)` and `avg(c.total_depth)` then over-counted. Rebuilt with `count(DISTINCT c.collar_id)` + scalar subqueries for downhole counts. | `scripts/phase18_step5_verify.sh` (7/7) |

**Phase 18 cumulative: 33 verifier checks across 5 verifiers**
(5 + 7 + 6 + 8 + 7 = 33).

---

## 2. Cold-run pass count progression

| Phase | Total | Pass | Notes |
|-------|------:|-----:|-------|
| 13 | 35 | 13 | Phase 13 fixture (10 PLS-*) cold-run peak |
| 14 | 35 | 12 | MV refresh added; one phrase-fragile slipped |
| 17 | 31 | 15 | 20-hole fixture; uranium commodity; avg=360.8m |
| **18** | **31** | **16** | **+1 from gq-015 lithology unlock** |

---

## 3. The Phase 18 unlock + the non-unlocks

- **gq-015-lithology-narration** — now passing. Expected
  `PLS-20-01` + `SST` + `PGN`; the lithology rows produce all
  three substrings via `query_downhole_logs`.

- **gq-014-assay-u3o8** — still failing. Fixture data is correct
  (U3O8_ppm peak = 52000 across 4 rows) but the agent's narration
  doesn't render the exact `U3O8` + `52` substrings the test
  expects. **Reclassified from Class E (data-missing) → Class A2
  (phrase-rendering).**

- **gq-017-assay-gold** — same story. 2 samples carry `Au_ppb` but
  the agent's response copy doesn't include a bare `Au` substring.
  Also Class A2.

Phase 19 candidate scope: either an agent-prompt tweak that
preserves chemical symbols + integer-fragment matches in the
response, OR test-side assertion relaxation per the deferred
R-P14-3.6 carry-over.

---

## 4. Carry-overs for Phase 19+

| ID | Item | Where | Priority |
|----|------|-------|----------|
| **R-P14-3.7** | Warm-run agent refusal investigation | orchestrator + agent state | **High** — central mystery of cold/warm split |
| **R-P14-3.6** | Test assertion relaxations (case + synonym) | `tests/test_golden_queries.py` | **Medium-high now** — would unlock gq-014/017 directly without changing agent |
| **R-P14-3.4** | Neo4j entity fixtures | Neo4j ingestion | Medium-high — 5 unlocks (Class D) |
| **R-P11-baseline-2** | Public-geoscience fixture | `public_geoscience.*` | Medium |
| **R-P15-1** | Bundled orchestrator prompts migration | `orchestrator.py` | Medium |
| **R-P11-B** | Frontend Search/Query page | `resources/js/Pages/` | Medium |

---

## 5. Files of record

**New in Phase 18:**

```
database/raw/phase18/10-assay-litho-fixture.sql                    (Step 2)
database/raw/phase18/15-fix-mv-collar-summary.sql                  (Step 5)
docs/phase18_implementation_kickoff.md                             (Step 0)
docs/phase18_assay_litho_schema_audit.md                           (Step 1)
docs/phase18_golden_baseline_v3.md                                 (Step 4)
docs/phase18_handoff.md                                             (this file)
scripts/phase18_master_sweep.sh                                    (Step 4)
scripts/phase18_step1_verify.sh                                    (Step 1)
scripts/phase18_step2_verify.sh                                    (Step 2)
scripts/phase18_step3_verify.sh                                    (Step 3)
scripts/phase18_step4_verify.sh                                    (Step 4)
scripts/phase18_step5_verify.sh                                    (Step 5)
```

---

## 6. Re-running every Phase 18 verifier

```bash
bash scripts/phase18_step1_verify.sh   # schema audit             (5/5)
bash scripts/phase18_step2_verify.sh   # assay fixture            (7/7)
bash scripts/phase18_step3_verify.sh   # lithology fixture        (6/6)
bash scripts/phase18_step4_verify.sh   # baseline + handoff       (8/8)
bash scripts/phase18_step5_verify.sh   # MV cartesian-join fix    (7/7)
```

Combined Phase 0 → Phase 18 sweep: `scripts/phase18_master_sweep.sh`
runs 60 verifiers; **58 / 60 green, 367 / 371 cumulative checks**
at close. The two non-greens are pre-existing and not Phase 18
regressions:

- `phase4_step7_verify.sh` — known sweep-only flake (passes
  standalone 5/5). Documented since Phase 12 / 15 / 16 closes.
- `phase9_step1_verify.sh` — Dagster/Tempo e2e verifier expects
  a docker network named `georag_georag`; the actual network is
  `georag`. Infrastructure-shaped, predates Phase 18.

After fix re-runs the Phase 17 Step 2 verifier (which broke when
Phase 18 surfaced the MV cartesian-join bug) is back to green (7/7).

---

## 7. Phase 19 entry checklist

1. Read this handoff + `docs/phase18_golden_baseline_v3.md`.
2. Re-run `scripts/phase18_master_sweep.sh` — confirm green.
3. Highest-leverage Phase 19 scope candidates:
   - **R-P14-3.6** — test assertion relaxations. Direct path
     to unlocking gq-014 + gq-017 + possibly 2-3 more
     Class A/A2 tests. Source-of-truth concern: should the
     tests be permissive, or should the agent be tightened?
     Phase 17 deferred this with the framing
     "tests should be source-of-truth, not target". Phase 19
     should revisit with the Class A2 evidence from Phase 18.
   - **R-P14-3.4** — Neo4j entity fixtures (Triple R, Sarah
     Thompson, CGL/GPT formations). Unlocks 4-5 tests.
   - **R-P14-3.7** — warm-run agent state investigation. Highest
     mystery-value but deepest debugging.

End of Phase 18 handoff.
