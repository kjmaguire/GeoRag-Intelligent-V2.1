# Phase 34 Handoff — R-P15-1 slice 2: 4 dash task-profile prompts migrated

**Document version:** 1.0
**Status:** Phase 34 complete. R-P15-1 slice 2 of 4 landed.
**Predecessors:** `docs/phase33_handoff.md`,
`docs/r-p15-1_prompt_migration_scope.md`.

---

## 1. What Phase 34 delivered

The second slice of R-P15-1. Migrates the 4 dash-variant
task-profile prompts (DEFAULT / NUMERIC / NARRATIVE / GRAPH) from
inline `_SYSTEM_PROMPT_*` constants in `orchestrator.py` to
dedicated modules under the canonical `prompts/` tree.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | 4 new prompt modules: `prompts/orchestrator_{default,numeric,narrative,graph}_dash.py`. Each imports `_SHARED_PREAMBLE` from Phase 33's module + appends its own `_BODY` to expose a composed `SYSTEM_PROMPT`. All four byte-identical to the previous inline definitions. | `scripts/phase34_step1_verify.sh` |
| 2 | 4 new entries in `_version_registry.py` PROMPT_REGISTRY. | (same) |
| 3 | `orchestrator.py` — replaced 4 inline `_SYSTEM_PROMPT_* = _SHARED + """..."""` blocks with 4 import statements (4-line each). Eliminates ~150 lines of inline prompt text from the orchestrator. | (same) |
| 4 | This handoff + master sweep | — |

---

## 2. Why this slice came out clean

Phase 33's shared preamble migration established the import pattern.
Each task-profile module follows the same three-section shape:

```python
from app.agent.prompts.orchestrator_shared_preamble_dash import (
    SYSTEM_PROMPT as _SHARED_PREAMBLE,
)

PROMPT_VERSION = "0.1.0"

_BODY = """
TASK PROFILE: …
"""

SYSTEM_PROMPT = _SHARED_PREAMBLE + _BODY
```

The composed `SYSTEM_PROMPT` is byte-identical to the
previous inline `_SYSTEM_PROMPT_X` definition, so the Anthropic
prompt-cache hash stays unchanged. Smoke test confirms:

```
default=4306  numeric=5382  narrative=5210  graph=6233
preamble_in_default=True
task_in_default=True (TASK PROFILE: general)
task_in_graph=True   (knowledge-graph traversal)
```

---

## 3. Cold-run pass count

| Phase | Cold typical | Peak |
|-------|-------------:|-----:|
| 32 | 31 | 31 |
| 33 | 30 | 31 |
| **34** | **30** (band 30-31) | **31** |

No regression. The migration is text-level only — no semantic
change.

---

## 4. Lines removed from orchestrator.py

| Migration | Inline LOC | Replacement LOC | Net reduction |
|-----------|-----------:|----------------:|--------------:|
| Phase 33 (shared preamble) | 51 | 4 | 47 |
| Phase 34 (4 task profiles) | ~160 | 16 (4 imports × 4 lines each) | 144 |
| **R-P15-1 dash slice total** | **~210** | **20** | **~190** |

orchestrator.py is now ~190 lines shorter for the dash-variant
prompt block alone. Slice 3 will migrate the 5 colon variants for
another ~210 line reduction.

---

## 5. Carry-overs for Phase 35+

| ID | Item | Priority |
|----|------|----------|
| R-P15-1 slice 3 | Migrate the 5 colon-variant prompts (shared preamble + 4 task profiles) | Medium — next autonomous-tick slice |
| R-P15-1 slice 4 | Cleanup: remove dispatch-table comments referencing the inline pattern; update `phase15_orchestrator_prompts_audit.md` | Low |
| R-P11-B | Frontend Search/Query page | Medium — user-driven |
| R-P21-CACHE-TELEMETRY-DASHBOARD | Cache skip-reason in operator dashboard | Low |

---

## 6. Files of record

```
src/fastapi/app/agent/prompts/orchestrator_default_dash.py       (Step 1 — new)
src/fastapi/app/agent/prompts/orchestrator_numeric_dash.py       (Step 1 — new)
src/fastapi/app/agent/prompts/orchestrator_narrative_dash.py     (Step 1 — new)
src/fastapi/app/agent/prompts/orchestrator_graph_dash.py         (Step 1 — new)
src/fastapi/app/agent/prompts/_version_registry.py               (Step 1 — 4 new entries)
src/fastapi/app/agent/orchestrator.py                            (Step 1 — 4 inline defs → 4 imports)
docs/phase34_handoff.md                                           (this file)
scripts/phase34_master_sweep.sh                                  (Step 2)
scripts/phase34_step1_verify.sh                                  (Step 1)
```

End of Phase 34 handoff.
