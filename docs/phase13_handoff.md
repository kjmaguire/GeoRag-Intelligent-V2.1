# Phase 13 Handoff — Golden fixture seeded

**Document version:** 1.0
**Status:** Phase 13 complete. Phase 14 inheriting.
**Predecessors:** `docs/phase12_handoff.md`, `docs/phase11_golden_baseline.md`,
`docs/phase13_golden_fixture_spec.md`.

---

## 1. What Phase 13 delivered

Phase 13 seeded the Milestone-1 golden-query fixture (10 PLS-*
drill collars + the parent `silver.projects` row) and proved
end-to-end that the RAG pipeline *can* produce real answers when
the underlying data exists — observed **13 / 35 passing** on the
first post-seed run, up from the Phase 11 baseline of 2 / 35.

Bundled with a second prompt migration (classifier_system),
keeping the Phase 11 Step 3 prompt-discipline pattern moving.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `_CLASSIFIER_SYSTEM_PROMPT` migrated from `llm_classifier.py` → `app/agent/prompts/classifier_system.py` + registry entry | `scripts/phase13_step1_verify.sh` (6/6) |
| 2 | `docs/phase13_golden_fixture_spec.md` — schema dependencies + expected row shape | `scripts/phase13_step2_verify.sh` (5/5) |
| 3 | `database/raw/phase13/10-golden-collars-fixture.sql` — silver.projects + 10 silver.collars rows under TEST_PROJECT_ID; idempotent | `scripts/phase13_step3_verify.sh` (8/8) |
| 4 | Baseline re-anchored — Phase 13 peak (13/35) documented; conservative floor stays at ≥2 due to LLM determinism; R-P13-1 flagged | `scripts/phase13_step4_verify.sh` (5/5) |
| 5 | This handoff | — |

**Phase 13 cumulative: 24 / 24 verifier checks** (6+5+8+5).
**Master sweep across Phase 0 → Phase 13 at close: 373 / 373 across
58 verifiers** (`scripts/phase13_master_sweep.sh`).

---

## 2. Notable finding — intermittent agent refusal

On the very first run after seeding, the golden suite passed 13/35
in ~48s (LLM round-trips dominant). Subsequent runs in the same
session passed only 2/35 in ~6s (LLM not called; agent's refusal
path fired). The fixture is intact — `silver.collars` still has 10
PLS-* rows — but the agent's orchestrator started short-circuiting
to "I don't have that data."

This is **R-P13-1** — a Phase 14 carry-over. The framework works;
the inconsistency is at the orchestrator's tool-dispatch /
classifier layer. Could be:
- Tool-result caching that goes stale
- Classifier non-determinism producing different bucket selections
  run to run
- vLLM response variability the deterministic orchestrator path
  doesn't tolerate

The 13-pass peak is documented in `phase11_golden_baseline.md` as
evidence the fixture *can* unlock these tests. The conservative
floor stays at ≥2 (metadata tests) until R-P13-1 is investigated.

---

## 3. Architectural state at end of Phase 13

### 3.1 RAG / agent posture (Phase 11 audit + Phase 13 additions)

- All 9 hallucination files unchanged.
- `app/agent/prompts/` registry now has 3 entries:
  example_system + rephrase_system + classifier_system.
- `app/agent/llm_classifier.py` + `app/agent/escalation.py` now
  import prompts from canonical paths. `orchestrator.py` still has
  inline prompts pending (Phase 14+).

### 3.2 Database posture

- `silver.projects` has one fixture row (`019d74a1-fba8-7165-9ae6-a5bf93eef97d`,
  Phantom Lake Silver, EPSG:32613, commodity=silver, region=Athabasca Basin).
- `silver.collars` has 10 PLS-* rows under that project — all
  Diamond, 9 Completed + 1 In Progress, depths 265–510m.
- Both `geom` (Point/32613) + `geom_4326` populated for spatial
  retrieval paths.

### 3.3 No changes elsewhere

Orchestration, auth, TLS, integration-edge unchanged from Phase 12.

---

## 4. Carry-overs for Phase 14

| ID | Item | Where | Priority |
|----|------|-------|----------|
| **R-P13-1** | Investigate intermittent agent refusal post-fixture-seed | orchestrator + classifier | **High** — blocks reliable golden suite |
| **R-P11-baseline-2** | Seed public-geoscience corpus fixture | golden-test infra | Medium — unlocks 3 pgeo tests |
| **R-P11-l4-fixture** | CI fixture for Layer 4 entity grounding | tests | Medium |
| **R-P11-B** | Frontend Search/Query page | `resources/js/Pages/` | Medium — first user surface |
| **R-P12-more-prompts** | Continue migrating `orchestrator.py` inline prompts | follow-on | Low — pattern proven |
| **R-P12-l6-overlap-hmac** | HMAC overlap-window rotation | controller | Low |
| **R-P12-l6-sme-review** | SME review of constraint bounds | doc | SME-gated |
| **R-P3-5**, **R-P3-6**, **R-P3-9** | Dual-write / Hatchet HA / vendor profiles | various | Deferred per prior phases |
| **R-P11-init-drift** | CLOSED at Phase 12 Step 1 |
| **R-P11-prompts-migrate** | CLOSED at Phase 12 Step 2 + Phase 13 Step 1 |
| **R-P11-l6-config** | CLOSED at Phase 12 Step 3 |
| **R-P10-1 + R-P10-2** | CLOSED at Phase 12 Step 4 |
| **R-P11-baseline-1** | CLOSED at Phase 13 Step 3 |

---

## 5. Files of record

**New in Phase 13:**

```
database/raw/phase13/10-golden-collars-fixture.sql                 (Step 3)
docs/phase11_golden_baseline.md                                    (mod — Step 4)
docs/phase13_implementation_kickoff.md                             (Step 0)
docs/phase13_golden_fixture_spec.md                                (Step 2)
docs/phase13_handoff.md                                             (this file)
scripts/phase13_master_sweep.sh                                    (Step 5)
scripts/phase13_step1_verify.sh                                    (Step 1)
scripts/phase13_step2_verify.sh                                    (Step 2)
scripts/phase13_step3_verify.sh                                    (Step 3)
scripts/phase13_step4_verify.sh                                    (Step 4)
src/fastapi/app/agent/llm_classifier.py                            (mod — Step 1)
src/fastapi/app/agent/prompts/_version_registry.py                 (mod — Step 1)
src/fastapi/app/agent/prompts/classifier_system.py                 (Step 1)
```

---

## 6. Re-running every Phase 13 verifier

```bash
bash scripts/phase13_step1_verify.sh   # classifier prompt migration  (6/6)
bash scripts/phase13_step2_verify.sh   # fixture spec doc             (5/5)
bash scripts/phase13_step3_verify.sh   # collar fixture seed          (8/8)
bash scripts/phase13_step4_verify.sh   # golden baseline re-anchor    (5/5)
```

Combined Phase 0 → Phase 13 sweep — **58 verifiers, 373 total checks**
(`scripts/phase13_master_sweep.sh`).

---

## 7. Phase 14 entry checklist

1. Read this handoff + `docs/phase11_golden_baseline.md` (the
   Phase 13 peak data) + `docs/phase11_section_04i_audit.md`.
2. Re-run `scripts/phase13_master_sweep.sh` — confirm 373/373.
3. Highest-leverage Phase 14 scope: **R-P13-1** investigation
   (why does the agent's refusal path fire intermittently?).
   Reproducing the 13-pass result on cold starts would unlock real
   regression testing.
4. Alternative scopes: R-P11-baseline-2 (pgeo fixture), R-P11-B
   (frontend Search page), continued prompt migrations.

End of Phase 13 handoff.
