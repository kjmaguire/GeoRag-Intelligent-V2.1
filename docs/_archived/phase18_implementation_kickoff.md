# Phase 18 Implementation Kickoff — Assay + lithology fixtures (R-P14-3.5)

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase17_handoff.md`,
`docs/phase17_golden_failure_audit.md`.

---

## 1. Theme

Phase 17 closed the engineering-shaped fixture gaps (PLS-* +
XLS-24-* + project commodity). Phase 18 picks up the next
fixture-shaped carry-over: **R-P14-3.5** — seed assay + lithology
data so the agent's `query_assay_data` + `query_downhole_logs`
tools can actually retrieve.

Target unlocks (from `phase17_golden_failure_audit.md` Class E):
- gq-014-assay-u3o8 — expects "U3O8" + "52" in response
- gq-015-lithology-narration — expects "PLS-20-01" + "SST" + "PGN"
- gq-017-assay-gold — expects "Au"

Bundle that includes the schema audit + the seed migration + the
re-baseline + handoff. No agent code changes; fixture-only.

---

## 2. Locked decisions

| ID | Item | Phase 18 status |
|----|------|---------------|
| **R-P14-3.5.A** | Assay fixture (U3O8 + Au samples on PLS-22-08) | **In scope (Step 2)** |
| **R-P14-3.5.B** | Lithology fixture (OVB/SST/PGN on PLS-20-01) | **In scope (Step 3)** |
| **R-P14-3.5.C** | Set default workspace_id on test project (samples FK) | **In scope (Step 2 — same migration)** |
| **R-P14-3.4** | Neo4j entity fixtures | Defer — different schema |
| **R-P14-3.7** | Warm-run agent refusal investigation | Defer — needs deep debug session |
| **R-P11-baseline-2** | Public-geoscience fixture | Defer |
| **R-P15-1** | Bundled orchestrator prompts migration | Defer |

---

## 3. Done definition

- Step 1 verifier: schema-audit doc captures the columns +
  workspace_id requirement + value shape for `commodity_assays`
  JSON.
- Step 2 verifier: at least 4 samples present under the test
  project's PLS-22-08 with U3O8_ppm peaking at ≥52000; at least
  2 samples carry an Au_ppb key.
- Step 3 verifier: at least 3 lithology log intervals present
  on PLS-20-01 with lithology_codes "OVB" / "SST" / "PGN".
- Cold-run golden pass count peak ≥ Phase 17's 15.
- All prior verifiers still green (428 → ~450+).

---

## 4. Step-by-step

### Step 1 — Schema audit doc
- `docs/phase18_assay_litho_schema_audit.md` — columns + FKs +
  workspace_id requirement + JSON value shape for
  `commodity_assays`.

### Step 2 — Assay fixture
- `database/raw/phase18/10-assay-litho-fixture.sql` — bundles
  workspace_id update, 4 PLS-22-08 samples with U3O8/Au, and
  3 PLS-20-01 lithology intervals.
- Idempotent.

### Step 3 — (folded into Step 2 SQL; separate verifier for litho)
- Step 3 verifier just checks the lithology side of the same
  migration.

### Step 4 — Re-baseline + handoff
- Run golden suite; check if cold-run peak rises above Phase 17's 15.
- Update Phase 17 baseline-v2 doc with Phase 18 results.
- Master sweep + handoff.

---

## 5. Files of record (preview)

```
database/raw/phase18/10-assay-litho-fixture.sql                    (Step 2)
docs/phase17_golden_baseline_v2.md                                 (mod — Step 4 baseline update)
docs/phase18_assay_litho_schema_audit.md                           (Step 1)
docs/phase18_handoff.md                                             (Step 4)
docs/phase18_implementation_kickoff.md                             (this file)
scripts/phase18_master_sweep.sh                                    (Step 4)
scripts/phase18_step1_verify.sh                                    (Step 1)
scripts/phase18_step2_verify.sh                                    (Step 2 — assays)
scripts/phase18_step3_verify.sh                                    (Step 3 — lithology)
```

End of Phase 18 kickoff.
