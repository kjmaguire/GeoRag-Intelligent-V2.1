# Phase 17 Handoff — Golden-test pass-rate investigation (R-P14-3 partial)

**Document version:** 1.0
**Status:** Phase 17 complete. Phase 18 inheriting.
**Predecessors:** `docs/phase16_handoff.md`,
`docs/phase17_golden_failure_audit.md`,
`docs/phase17_golden_baseline_v2.md`.

---

## 1. What Phase 17 delivered

Phase 17 was the **R-P14-3 investigation** the roadmap flagged
as the highest-leverage next phase. The investigation found
that the agent is producing correct answers from the fixture
data, but:

1. The Phase 13 fixture didn't match the test's documented
   ground-truth (10 vs 20 holes; 348m vs 360.8m avg; silver
   vs uranium commodity).
2. There's an unresolved warm-run agent state issue that drops
   pass count from cold-run peak to refusal-path floor across
   subsequent runs.

Phase 17 fixes #1 fully (engineering-shaped work). #2 is
deferred as R-P14-3.7 with documented Phase 18+ investigation
recommendations.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `docs/phase17_golden_failure_audit.md` — categorised all 30 milestone-1 failures into 6 classes; separated Phase 17 unlocks from Phase 18+ deferred work | `scripts/phase17_step1_verify.sh` (5/5) |
| 2 | `database/raw/phase17/10-golden-fixture-extensions.sql` — 10 XLS-24-* collars added; project commodity → uranium, region → "Athabasca Basin"; 20-hole avg_depth = 360.8m exact | `scripts/phase17_step2_verify.sh` (7/7) |
| 3 | `docs/phase17_golden_baseline_v2.md` — cold-run peak 15/31 documented (+2 vs Phase 13 peak); conservative floor unchanged at ≥2 | `scripts/phase17_step3_verify.sh` (5/5) |
| 4 | This handoff | — |

**Phase 17 cumulative: 17 / 17 verifier checks** (5+7+5).
**Master sweep across Phase 0 → Phase 17 at close: 428 / 428 across
68 verifiers** (`scripts/phase17_master_sweep.sh`).

---

## 2. Cold-run pass count progression

| Phase | Total | Pass | Notes |
|-------|------:|-----:|-------|
| 11 | 35 | 2 | No fixture; agent refused on missing data |
| 13 | 35 | 13 | Phase 13 fixture seeded (10 PLS-* collars) — cold-run peak |
| 14 | 35 | 12 | MV refresh added to fixture migration |
| **17** | **31** | **15** | **20-hole fixture; uranium commodity; avg=360.8m** |

Phase 17's +2 vs Phase 13 peak comes from gq-001 (count "20"
now matches) + gq-007 (avg "360.8" now matches) + gq-022
(commodity "uranium" now matches), minus one test that was
phrase-fragile and slipped between runs.

---

## 3. Two upstream verifiers updated

Phase 17 Step 2 raised the fixture to 20 collars. Two prior
verifiers hardcoded `=10`:

- `scripts/phase14_step3_verify.sh` (check #6)
- `scripts/phase15_step1_verify.sh` (check #6)

Both relaxed to `≥10` so they pass both pre-Phase-17 (10 collars)
and post-Phase-17 (20 collars) DB states. No behavioural
change — just makes the assertions forward-compatible.

---

## 4. Carry-overs for Phase 18

| ID | Item | Where | Priority |
|----|------|-------|----------|
| **R-P14-3.7** | Warm-run agent refusal investigation | orchestrator + LLM client cache | **High** — cold-run peak 15 / warm-run floor 1 split is the central remaining mystery |
| **R-P14-3.4** | Neo4j entity fixtures (Triple R, Sarah Thompson, CGL, GPT, unconformity) | Neo4j ingestion path | Medium-high — unlocks 4-5 graph tests |
| **R-P14-3.5** | Assay + lithology fixtures (U3O8, Au, PGN, SST) | `silver.samples`, `silver.lithology_logs` | Medium — unlocks 3-4 tests |
| **R-P11-baseline-2** | Public-geoscience fixture | `public_geoscience.*` | Medium — unlocks 3 pgeo tests |
| **R-P15-1** | Bundled orchestrator prompts migration | `orchestrator.py` | Medium |
| **R-P11-B** | Frontend Search/Query page | `resources/js/Pages/` | Medium |
| **R-P14-3.1, .2, .3** | Engineering unlocks (XLS-24-*, depths, commodity) | CLOSED at Phase 17 Step 2 |
| **R-P14-3.6** | Test assertion relaxations | Defer — should be source-of-truth, not target |
| **R-P3-5**, **R-P3-6**, **R-P3-9** | Deferred per prior phases |

---

## 5. Files of record

**New in Phase 17:**

```
database/raw/phase17/10-golden-fixture-extensions.sql              (Step 2)
docs/phase17_implementation_kickoff.md                             (Step 0)
docs/phase17_golden_failure_audit.md                               (Step 1)
docs/phase17_golden_baseline_v2.md                                 (Step 3)
docs/phase17_handoff.md                                             (this file)
scripts/_p17_cold_run.sh                                            (Step 3 helper)
scripts/phase14_step3_verify.sh                                    (mod — ≥10 relaxation)
scripts/phase15_step1_verify.sh                                    (mod — ≥10 relaxation)
scripts/phase17_master_sweep.sh                                    (Step 4)
scripts/phase17_step1_verify.sh                                    (Step 1)
scripts/phase17_step2_verify.sh                                    (Step 2)
scripts/phase17_step3_verify.sh                                    (Step 3)
```

---

## 6. Re-running every Phase 17 verifier

```bash
bash scripts/phase17_step1_verify.sh   # failure audit          (5/5)
bash scripts/phase17_step2_verify.sh   # fixture extensions     (7/7)
bash scripts/phase17_step3_verify.sh   # baseline v2 doc        (5/5)
```

Combined Phase 0 → Phase 17 sweep — **68 verifiers, 428 total checks**
(`scripts/phase17_master_sweep.sh`). Green first run after
relaxing the two `=10` hardcodes.

---

## 7. Phase 18 entry checklist

1. Read this handoff + `docs/phase17_golden_failure_audit.md` +
   `docs/phase17_golden_baseline_v2.md`.
2. Re-run `scripts/phase17_master_sweep.sh` — confirm 428/428.
3. Highest-leverage Phase 18 scope candidates:
   - **R-P14-3.7** — investigate warm-run agent state. Specific
     starting point: trace one warm-run failing query end-to-end
     with debug logging to find where the agent decides to
     refuse. The MV is populated; something downstream of the
     fact-block construction is short-circuiting.
   - **R-P14-3.5** — seed `silver.samples` U3O8 + Au assays for
     PLS-22-08 + PLS-22-12 (test data documented in gq-014 +
     gq-017). 3-4 unlocks.
   - **R-P14-3.4** — seed Neo4j entities (Triple R deposit, Sarah
     Thompson QP, CGL/GPT formations). 4-5 unlocks. Needs
     understanding of Neo4j ingestion path.

End of Phase 17 handoff.
