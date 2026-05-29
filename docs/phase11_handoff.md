# Phase 11 Handoff — RAG validation + discipline

**Document version:** 1.0
**Status:** Phase 11 complete. Phase 12 inheriting.
**Predecessors:** `docs/phase10_handoff.md`,
`docs/phase11_implementation_kickoff.md`, `docs/phase11_scoping.md`.

---

## 1. What Phase 11 delivered

Phase 11 was the long-deferred pivot from "build infrastructure" to
"validate the product surface." No new feature code; instead, an
audit of the Section 04i hallucination-defence framework already in
tree, a baseline of the golden-query test suite, and a prompts/
subdirectory bootstrap so the Phase 5 Step 3 pre-commit hook is now
genuinely live.

The most consequential finding: **the RAG framework is more complete
than the Phase 10 inventory suggested.** All six §04i layers have
implementations (not "5 of 6" — Layer 5 provenance exists at 157
lines despite the `__init__.py` saying otherwise). The framework's
gate-and-refuse behaviour is correct — golden tests fail today
because the agent properly refuses on missing fixtures, not because
it hallucinates.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `docs/phase11_section_04i_audit.md` — layer-by-layer audit, 4-guard mapping, 5 gap observations, 9-row implementation summary | `scripts/phase11_step1_verify.sh` (6/6) |
| 2 | `docs/phase11_golden_baseline.md` — 35 collected tests, 2 pass / 33 fail floor recorded for Phase 12+ regression detection | `scripts/phase11_step2_verify.sh` (5/5) |
| 3 | `src/fastapi/app/agent/prompts/` — canonical landing zone with `__init__.py` + `_version_registry.py` + `example_system.py`. Existing inline prompts NOT migrated (Phase 12+) | `scripts/phase11_step3_verify.sh` (6/6) |
| 4 | Golden suite wired into master sweep with ≥2 baseline floor; <10s elapsed | `scripts/phase11_step4_verify.sh` (5/5) |
| 5 | Pre-commit hooks proven live end-to-end (uv-managed `pre-commit 4.6.0`, both hooks fire + accept) | `scripts/phase11_step5_verify.sh` (6/6) |
| 6 | This handoff | — |

**Phase 11 cumulative: 28 / 28 verifier checks** (6+5+6+5+6).
**Master sweep across Phase 0 → Phase 11 at close: 324 / 324 across
50 verifiers** (`scripts/phase11_master_sweep.sh`). Green first
run.

---

## 2. Architectural state at end of Phase 11

### 2.1 Orchestration ownership (unchanged from Phase 10)

No changes.

### 2.2 New surfaces

| Surface | Purpose | Phase 12 work |
|---------|---------|---------------|
| `docs/phase11_section_04i_audit.md` | Snapshot of all 9 hallucination files + per-layer enforcement | Update if layer code moves; fix the §04i `__init__.py` docstring drift |
| `docs/phase11_golden_baseline.md` | Phase 12+ regression floor for golden-query pass count | Seed `silver.collars` PLS-20-* fixtures so the 30 parametrised tests can actually pass |
| `src/fastapi/app/agent/prompts/` | Canonical home for prompt strings; bootstrap pattern | Migrate one or two inline prompts from `orchestrator.py` to prove the pattern |
| `scripts/phase11_step4_verify.sh` | Golden smoke in the regression gate | None — fully wired |

### 2.3 RAG framework state (newly confirmed)

| Layer | File | Lines | Status |
|-------|------|------:|--------|
| 1 retrieval | `layer1_retrieval.py` | 125 | implemented |
| 2 typed output | `layer2_typed_output.py` | 128 | implemented |
| 3 numerical | `layer3_numerical.py` | 301 | implemented |
| 4 entity | `layer4_entity.py` | 362 | implemented |
| 5 provenance | `layer5_provenance.py` | 157 | implemented (init.py docstring stale) |
| 6 constraints | `layer6_constraints.py` | 337 | implemented (constraints hard-coded — gap) |
| completeness | `layer_completeness.py` | 434 | implemented |
| orchestrator validators | `orchestrator_validators.py` | 480 | implemented |
| qualitative | `qualitative_detector.py` | 101 | implemented |

Total: **2425 lines** of hallucination-prevention code across 9 files.

### 2.4 Pre-commit posture

`/home/georag/.local/share/uv/tools/pre-commit/bin/python -m pre_commit run`
is the canonical invocation path (uv-managed install at `pre-commit
4.6.0`). Two hooks active:

- `fastapi-pydantic-freshness` — blocks commits with stale fastapi
  imports
- `system-prompt-version-bump` — blocks prompt-path commits that
  don't also bump `_SYSTEM_PROMPT_VERSION`

Both proven live in Step 5.

---

## 3. Operational state

Same as Phase 10 plus:

- `bash scripts/phase11_step4_verify.sh` runs the golden suite as
  part of the master sweep (~10s wall time).
- A pre-commit edit to anything under
  `src/fastapi/app/agent/prompts/` will now fire the version-bump
  hook. Operators committing prompt changes can run
  `pre-commit install` once to enable the hook on every commit.
- The Phase 11 audit doc is the canonical reference for §04i
  layer coverage; ground truth for Phase 12 reviewers.

---

## 4. Carry-overs for Phase 12

| ID | Item | Where | Phase 12 rationale |
|----|------|-------|---------------------|
| **R-P3-5** | Generalised dual-write harness | hard-coded | Re-evaluate when second migration target lands |
| **R-P3-6** | Hatchet HA | docker-compose | Path B per `phase8_hatchet_ha_design.md` if forcing function lands |
| **R-P3-9** | Vendor-profile column-mapping | parsers | SME-gated |
| **R-P9-2** | Real ACME activation | Caddy + DNS | Deploy-time |
| **R-P10-1** | Sender HMAC rotate button | admin UI | Pair with Phase 9 rotate-jwt pattern |
| **R-P10-2** | Rotation history panel | admin UI | Surfaces Phase 10 Step 1's audit emissions |
| **R-P11-baseline-1** | Seed `silver.collars` PLS-20-* fixture | golden-test infra | Unlocks 30 of 33 currently-failing golden tests |
| **R-P11-baseline-2** | Seed public-geoscience corpus fixture | golden-test infra | Unlocks 3 pgeo golden tests |
| **R-P11-init-drift** | Update `hallucination/__init__.py` docstring | comments-only | Reflect actual Layer 2 / Layer 5 status |
| **R-P11-l6-config** | Externalise Layer 6 constraints to SME-editable config | `layer6_constraints.py` | Currently inline; CLAUDE.md hard rule 6 wants config |
| **R-P11-prompts-migrate** | Migrate one inline prompt from `orchestrator.py` to `prompts/` | follow-up | Proves the pattern; pairs with version-bump hook |
| **R-P11-A** | Section 04i audit | CLOSED at Step 1 |
| **R-P11-C** | `prompts/` subdirectory canonicalisation | CLOSED at Step 3 |
| **R-P9-1** | Audit row per JWT rotation | CLOSED at Phase 10 Step 1 |
| **R-P9-3** | Phase 5 Step 1 verifier flake | CLOSED at Phase 10 Step 2 |
| **R-P11-B** | Frontend Search/Query page | Phase 12+ candidate; see scoping doc Path B |

---

## 5. Files of record

**New in Phase 11:**

```
docs/phase11_implementation_kickoff.md                             (Step 0)
docs/phase11_section_04i_audit.md                                  (Step 1)
docs/phase11_golden_baseline.md                                    (Step 2)
docs/phase11_handoff.md                                             (this file)
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

**Archived in Phase 11:** none.

---

## 6. Re-running every Phase 11 verifier

```bash
bash scripts/phase11_step1_verify.sh   # §04i audit doc          (6/6)
bash scripts/phase11_step2_verify.sh   # golden-test baseline    (5/5)
bash scripts/phase11_step3_verify.sh   # prompts/ bootstrap      (6/6)
bash scripts/phase11_step4_verify.sh   # golden smoke regression (5/5)
bash scripts/phase11_step5_verify.sh   # pre-commit activation   (6/6)
```

Combined Phase 0 → Phase 11 sweep — **50 verifiers, 324 total checks**
(`scripts/phase11_master_sweep.sh`). The Step 5 invocation depends
on `pre-commit 4.6.0` being installed at
`/home/georag/.local/share/uv/tools/pre-commit/bin/python` — that's
how Phase 5 Step 3 originally set it up.

---

## 7. Phase 12 entry checklist

Before Phase 12 work begins:

1. Read this handoff + `docs/phase11_section_04i_audit.md` +
   `docs/phase11_golden_baseline.md`.
2. Re-run `scripts/phase11_master_sweep.sh` — confirm 324/324
   green.
3. Decide Phase 12 scope. Candidates:
   - **R-P11-baseline-1 + baseline-2** (seed fixtures) — unlocks
     the existing golden suite. Highest-leverage next step.
     Probably ~3-4 fixture migrations + a re-baseline.
   - **R-P11-prompts-migrate** + **R-P11-init-drift** + **R-P11-l6-config**
     — three small RAG-side discipline items pairing well together.
   - **R-P11-B** frontend Search/Query page — first user-facing
     RAG surface; medium-large.
   - **R-P10-1 + R-P10-2** — operator-UI continuations (sender
     HMAC rotate + rotation history panel).
   - **R-P3-6** Hatchet HA — only if forcing function lands.

Five "tight ops + validation" phases in a row (Phase 7 → 11). The
infrastructure + integration + auth + observability + RAG-framework
arcs are now all **mature**. Phase 12 is the natural pivot to
either fixture-completion (unlocking the golden suite for real
quality measurement) or first-user-facing surfaces (R-P11-B
frontend Search page).

End of Phase 11 handoff.
