# Phase 13 Implementation Kickoff — Golden fixture seeding + more prompt discipline

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase12_handoff.md`, `docs/phase11_golden_baseline.md`,
`docs/phase11_section_04i_audit.md`.

---

## 1. Theme

Phase 11 captured the golden-query baseline at 2 / 35 passing — the
agent correctly refuses on missing fixtures rather than hallucinating.
Phase 13 closes the highest-leverage carry-over from Phase 12's
handoff: **seed the `silver.collars` PLS-20-* fixture** so the 30
parameterised Milestone-1 golden tests have data to validate against.

Bundled with one more inline prompt migration (R-P12-more-prompts) to
keep the prompt-discipline arc moving — the canonical pattern has been
proven; the remaining migrations should be routine.

---

## 2. Locked decisions

| ID | Item | Phase 13 status |
|----|------|---------------|
| **R-P11-baseline-1** | Seed `silver.collars` PLS-20-* fixture | **In scope (Steps 2-4)** |
| **R-P12-more-prompts** | Migrate one more inline prompt | **In scope (Step 1)** |
| **R-P11-baseline-2** | Seed pgeo corpus fixture | Defer — more involved, separate phase |
| **R-P3-5**, **R-P3-6**, **R-P3-9** | Dual-write / Hatchet HA / vendor profiles | Defer per prior phases |
| **R-P11-B** | Frontend Search/Query page | Defer — Phase 14+ |
| **R-P12-l6-overlap-hmac** | HMAC overlap-window rotation | Defer — small follow-on |
| **R-P12-l6-sme-review** | SME review of constraint bounds | Defer — needs Kyle |

---

## 3. Done definition

Each step ships a verifier. Phase 13 passes when:

- Step 1 verifier proves a second inline prompt migrated from
  `llm_classifier.py` to `app/agent/prompts/classifier_system.py`
  with registry entry + round-trip equality.
- Step 2 verifier proves a schema-audit doc captures what the golden
  tests expect (project_id, collar count, hole IDs, status mix,
  drill years, depth/easting bounds).
- Step 3 verifier proves the `silver.collars` table contains
  exactly 10 rows under `project_id = '019d74a1-fba8-7165-9ae6-a5bf93eef97d'`
  with the expected hole IDs, depths, and types.
- Step 4 verifier proves the golden-query baseline pass count
  increases (≥3 passes — at minimum the SQL-only deterministic
  tests should now succeed). The new floor is written to
  `phase11_golden_baseline.md`.
- All prior phase verifiers still green (349 → ~370+).

---

## 4. Step-by-step

### Step 1 — Migrate `_CLASSIFIER_SYSTEM_PROMPT` (R-P12-more-prompts)
- Same pattern as Phase 12 Step 2's `rephrase_system` migration.
- Source: `app/agent/llm_classifier.py:_CLASSIFIER_SYSTEM_PROMPT`.
- Target: `app/agent/prompts/classifier_system.py`.
- Verifier: round-trip equality in-container + registry entry.

### Step 2 — Schema + fixture expectations audit
- Inspect `silver.collars` schema (columns, types, FKs).
- Read `test_golden_queries.py` to extract the exact expected data
  shape (project_id UUID, hole IDs, depths, statuses, drill years).
- Output: `docs/phase13_golden_fixture_spec.md` documenting what
  must be in the DB for the golden tests to retrieve correctly.

### Step 3 — Seed `silver.collars` fixture
- Migration: `database/raw/phase13/10-golden-collars-fixture.sql`.
- Idempotent: `INSERT ... ON CONFLICT DO NOTHING`.
- Wraps the seed in a check that the parent project row exists
  (or creates a minimal one if needed).
- Verifier: row count + key invariants match the test expectations.

### Step 4 — Re-baseline golden tests
- Run the golden suite post-fixture.
- Update `docs/phase11_golden_baseline.md` with the new pass count.
- The new pass count becomes the regression floor — any drop in
  Phase 14+ fails the master sweep.
- Verifier: pass count ≥ Phase 11 baseline + 1 (i.e., we MUST
  unlock at least one previously-failing test).

### Step 5 — Phase 13 → Phase 14 handoff
- Same shape as previous handoffs.

---

## 5. Engineering invariants

- `scripts/phase13_master_sweep.sh` extends the Phase 12 sweep.
  Target: 100% green.
- Fixture seed is reproducible — applying twice produces the same
  end state.
- Step 4's regression floor is INCREASED only by the new fixture's
  unlocked tests. Phase 14+ may NOT regress below this new floor.

---

## 6. Files of record (preview)

```
database/raw/phase13/10-golden-collars-fixture.sql                 (Step 3)
docs/phase11_golden_baseline.md                                    (mod — Step 4)
docs/phase13_implementation_kickoff.md                             (this file)
docs/phase13_golden_fixture_spec.md                                (Step 2)
docs/phase13_handoff.md                                             (Step 5)
scripts/phase13_master_sweep.sh                                    (Step 5)
scripts/phase13_step1_verify.sh                                    (Step 1)
scripts/phase13_step2_verify.sh                                    (Step 2)
scripts/phase13_step3_verify.sh                                    (Step 3)
scripts/phase13_step4_verify.sh                                    (Step 4)
src/fastapi/app/agent/llm_classifier.py                            (mod — Step 1; imports from prompts/)
src/fastapi/app/agent/prompts/_version_registry.py                 (mod — Step 1; classifier_system entry)
src/fastapi/app/agent/prompts/classifier_system.py                 (Step 1)
```

---

End of Phase 13 kickoff.
