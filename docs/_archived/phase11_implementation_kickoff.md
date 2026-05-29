# Phase 11 Implementation Kickoff — RAG validation + discipline

**Document version:** 1.0
**Status:** Active.
**Predecessors:** `docs/phase10_handoff.md`, `docs/phase11_scoping.md`.

---

## 1. Theme

The Phase 10 scoping inventory surfaced that the RAG framework is
**already implemented** — 30 agent files, all six Section 04i
hallucination layers, golden-query test files. Phase 11 doesn't build
RAG; it validates the framework that's there and lays operational
discipline for prompt-version bookkeeping.

Combines **Path A + Path C** from `phase11_scoping.md`:
- Path A — Section 04i audit + golden-query smoke wired into the
  master sweep so RAG quality is on the regression gate.
- Path C — `src/fastapi/app/agent/prompts/` subdirectory bootstrap so
  the Phase 5 Step 3 pre-commit hook (`system-prompt-version-bump`)
  has a real target.

Path B (frontend Search/Query page) stays deferred to Phase 12.

---

## 2. Locked decisions

| ID | Item | Phase 11 status |
|----|------|---------------|
| **R-P11-A** | Section 04i audit + golden-query smoke | **In scope (Steps 1, 2, 4)** |
| **R-P11-C** | `prompts/` subdirectory canonicalisation | **In scope (Steps 3, 5)** |
| **R-P11-B** | Frontend Search/Query page | Defer — Phase 12 |
| **R-P3-5** | Generalised dual-write harness | Defer |
| **R-P3-6** | Hatchet HA | Defer |
| **R-P3-9** | Vendor profiles | Defer (SME-gated) |
| **R-P10-1** | Sender HMAC rotate button | Defer — Phase 12 candidate |
| **R-P10-2** | Rotation history panel | Defer — Phase 12 candidate |

---

## 3. Done definition

Each step ships a verifier. Phase 11 passes when:

- Step 1 verifier proves `docs/phase11_section_04i_audit.md` exists,
  reads each of the six layer files, summarises what each enforces,
  and lists at least one concrete gap or coverage observation per
  layer.
- Step 2 verifier proves the existing golden-query tests (`test_golden_queries.py`
  + `test_public_geoscience_golden.py`) execute end-to-end in CI mode
  (collected + summarised pass/fail). A baseline doc captures the
  results so Phase 12+ can detect regression.
- Step 3 verifier proves `src/fastapi/app/agent/prompts/` exists,
  contains an `__init__.py` + a `_VERSION_REGISTRY.py` module + at
  least one example prompt file, and the import works inside the
  fastapi container.
- Step 4 verifier runs the golden-query suite as part of the master
  sweep and asserts the pass count is ≥ the Step 2 baseline.
- Step 5 verifier proves `pre-commit install` succeeded once, the
  hook is now in `.git/hooks/pre-commit`, and touching a file under
  `src/fastapi/app/agent/prompts/` triggers the
  `system-prompt-version-bump` hook to run (and reject without a
  version bump).
- All prior phase verifiers still green (296 → ~325+).

---

## 4. Step-by-step

### Step 1 — Section 04i hallucination layer audit
- Read each `src/fastapi/app/agent/layer_*.py` (or whatever the
  naming convention turns out to be — Phase 10's inventory said
  "ten files covering layers 1-6 + completeness + validators +
  qualitative_detector").
- For each layer, write a one-paragraph summary: what input shape
  it takes, what it rejects/accepts, what tests exercise it.
- Cross-reference against the CLAUDE.md hard rules (citations
  mandatory, Section 04i six layers).
- Output: `docs/phase11_section_04i_audit.md` with one section per
  layer + a gaps list.

### Step 2 — Golden-test baseline
- Run the existing golden suites inside the fastapi container:
    - `test_golden_queries.py`
    - `test_public_geoscience_golden.py`
- Capture pass/fail counts + total elapsed time.
- Output: `docs/phase11_golden_baseline.md` recording the pass
  counts that Phase 12+ should not regress below.

### Step 3 — `prompts/` subdirectory bootstrap
- Create `src/fastapi/app/agent/prompts/` with:
    - `__init__.py` exporting a `PROMPT_REGISTRY` mapping
    - `_version_registry.py` with a `_SYSTEM_PROMPT_VERSION` constant
    - One example file (e.g. `example_system.py`) demonstrating the
      pattern + a docstring explaining how to migrate inline prompts
- Do NOT migrate existing inline prompts — that's deeper engineering
  work for a follow-up phase.
- Verifier: directory + files exist, `from app.agent.prompts import
  PROMPT_REGISTRY` works inside the fastapi container.

### Step 4 — Golden-query smoke in master sweep
- Add `scripts/phase11_step4_verify.sh` that runs the golden suite
  via `docker exec georag-fastapi pytest ...` and asserts the pass
  count meets the Step 2 baseline.
- Update `phase11_master_sweep.sh` accordingly.
- Cap test runtime at 5 minutes; surface the elapsed time in the
  verifier output.

### Step 5 — Pre-commit hook end-to-end activation
- Run `pre-commit install` in the project root (creates / updates
  `.git/hooks/pre-commit`).
- Touch `src/fastapi/app/agent/prompts/example_system.py`, stage it,
  attempt `git commit --dry-run`.
- Confirm the `system-prompt-version-bump` hook fires + rejects (or
  passes, depending on whether `_SYSTEM_PROMPT_VERSION` was also
  bumped).
- Verifier: hook installed, hook entry script executable, the
  prompt-path-match regex still includes the new prompts/ tree.

### Step 6 — Phase 11 → Phase 12 handoff
- Same shape as previous handoffs.

---

## 5. Engineering invariants

- `scripts/phase11_master_sweep.sh` extends the Phase 10 sweep with
  the five new verifiers. Target: 100% green.
- No new database migrations. Phase 11 is doc + code + scripts only.
- Step 4's golden smoke must NOT mutate persistent state outside
  the test container's own fixtures (RAG corpora, qdrant indexes
  etc. stay untouched in the run).
- Step 1's audit doc is a snapshot, not a moving target — it
  records what's in tree TODAY. Phase 12+ updates it if layers
  change.

---

## 6. Files of record (preview)

```
docs/phase11_implementation_kickoff.md                             (this file)
docs/phase11_section_04i_audit.md                                  (Step 1)
docs/phase11_golden_baseline.md                                    (Step 2)
docs/phase11_handoff.md                                             (Step 6)
scripts/phase11_master_sweep.sh                                    (Step 6)
scripts/phase11_step1_verify.sh                                    (Step 1)
scripts/phase11_step2_verify.sh                                    (Step 2)
scripts/phase11_step3_verify.sh                                    (Step 3)
scripts/phase11_step4_verify.sh                                    (Step 4)
scripts/phase11_step5_verify.sh                                    (Step 5)
src/fastapi/app/agent/prompts/__init__.py                         (Step 3)
src/fastapi/app/agent/prompts/_version_registry.py                 (Step 3)
src/fastapi/app/agent/prompts/example_system.py                    (Step 3)
```

---

End of Phase 11 kickoff.
