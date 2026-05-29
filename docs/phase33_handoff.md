# Phase 33 Handoff — R-P15-1 slice 1: shared preamble migration

**Document version:** 1.0
**Status:** Phase 33 complete. First slice of R-P15-1 landed.
**Predecessors:** `docs/phase32_handoff.md`,
`docs/r-p15-1_prompt_migration_scope.md`.

---

## 1. What Phase 33 delivered

The first atomic slice of R-P15-1 (Bundled orchestrator prompts
migration). Moves the dash-variant shared preamble (51 lines of
prompt text) from `orchestrator.py` to a dedicated module under
the canonical `prompts/` tree.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `src/fastapi/app/agent/prompts/orchestrator_shared_preamble_dash.py` — new module exporting `PROMPT_VERSION="0.1.0"` + `SYSTEM_PROMPT=<51-line preamble>`. Text byte-identical to the prior inline definition so the Anthropic prompt-cache hash stays unchanged. | `scripts/phase33_step1_verify.sh` |
| 2 | `src/fastapi/app/agent/prompts/_version_registry.py` — new registry entry under the "Phase 15+ migrations land below this line" marker. | (same) |
| 3 | `src/fastapi/app/agent/orchestrator.py` — inline definition removed; `_SYSTEM_PROMPT_SHARED_PREAMBLE` now imported as `SYSTEM_PROMPT` from the new module. The four task-profile constants (DEFAULT / NUMERIC / NARRATIVE / GRAPH) keep their existing `_SYSTEM_PROMPT_SHARED_PREAMBLE + """..."""` concatenation pattern. | (same) |
| 4 | This handoff + master sweep | — |

---

## 2. Why this is the right shape for slice 1

The shared preamble is the foundation that all four task-profile
prompts depend on. Migrating it first establishes the
import-and-concatenate pattern in code (not just docs) and lets
subsequent slices be even smaller — each subsequent phase moves
one task-profile body, with the preamble import already in place.

The migration is also the easiest unit of R-P15-1 to verify is
correct: the preamble text is a single literal block (no
concatenation), and the byte-identical content guarantees the
Anthropic prompt-cache hash stays the same. The pre-commit
hook's `system-prompt-version-bump` gate also stays satisfied
because we registered the new prompt at v0.1.0 *and* the
orchestrator import is the only consuming reference.

---

## 3. Cold-run pass count

| Phase | Cold typical | Peak |
|-------|-------------:|-----:|
| 32 | 31 | 31 |
| **33** | **30** (in this run; band 30-31) | 31 |

Phase 33 ran 30/31 in the immediate post-change check (gq-014 +
gq-017 on their variance edges). Within the post-Phase-32 typical
30-31 band. No regression from the migration.

---

## 4. Planned R-P15-1 follow-up slices

Per `docs/r-p15-1_prompt_migration_scope.md`:

| Slice | Scope | Estimated LOC |
|-------|-------|--------------:|
| 33 (done) | dash shared preamble | ~80 |
| 34 (next) | dash DEFAULT + NUMERIC + NARRATIVE + GRAPH (4 modules) | ~300 |
| 35 | colon shared preamble + 4 colon task profiles (5 modules) | ~350 |
| 36 | cleanup: remove the dispatch-table comments referencing the inline pattern; update the audit doc | ~20 |

Each slice independent, each leaves the suite at typical 30-31/31.

---

## 5. Carry-overs for Phase 34+

| ID | Item | Priority |
|----|------|----------|
| R-P15-1 (continued) | Slice 2: migrate the 4 dash task-profile bodies | Medium — Phase 34 candidate |
| R-P15-1 (continued) | Slice 3: migrate the 5 colon-variant prompts | Medium — Phase 35 candidate |
| R-P11-B | Frontend Search/Query page | Medium — user-driven |
| R-P21-CACHE-TELEMETRY-DASHBOARD | Cache skip-reason in operator dashboard | Low — paired with R-P11-B |

---

## 6. Files of record

```
src/fastapi/app/agent/prompts/orchestrator_shared_preamble_dash.py   (Step 1 — new module)
src/fastapi/app/agent/prompts/_version_registry.py                    (Step 1 — registry entry)
src/fastapi/app/agent/orchestrator.py                                 (Step 1 — inline def replaced with import)
docs/phase33_handoff.md                                               (this file)
scripts/phase33_master_sweep.sh                                       (Step 2)
scripts/phase33_step1_verify.sh                                       (Step 1)
```

End of Phase 33 handoff.
