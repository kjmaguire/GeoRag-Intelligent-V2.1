# Phase 35 Handoff — R-P15-1 slice 3: 5 colon-variant prompts migrated

**Document version:** 1.0
**Status:** Phase 35 complete. R-P15-1 slice 3 of 4 landed; ~98% of the prompt-migration LOC is done.
**Predecessors:** `docs/phase34_handoff.md`,
`docs/r-p15-1_prompt_migration_scope.md`.

---

## 1. What Phase 35 delivered

The third slice of R-P15-1. Migrates the 5 colon-variant prompts
(shared preamble + 4 task profiles) from inline constants in
`orchestrator.py` to dedicated modules under `prompts/`. The colon
family activates when `settings.CITATION_SPAN_RESOLVER_ENABLED=True`
and differs from the dash family only in citation marker format
(`[DATA:X]` instead of `[DATA-X]`).

| Step | Output | Verifier |
|------|--------|----------|
| 1 | 5 new prompt modules: `prompts/orchestrator_{shared_preamble,default,numeric,narrative,graph}_colon.py`. The 4 task-profile modules each import the colon shared preamble and append their body. All five byte-identical to the previous inline definitions. | `scripts/phase35_step1_verify.sh` |
| 2 | 5 new entries in `_version_registry.py` PROMPT_REGISTRY (now 10 R-P15-1 entries: 5 dash + 5 colon). | (same) |
| 3 | `orchestrator.py` — replaced 5 inline `_SYSTEM_PROMPT_*_COLON` definitions with 5 import statements (~210 LOC freed). | (same) |
| 4 | This handoff + master sweep | — |

---

## 2. Cold-run pass count

| Phase | Cold typical | Peak |
|-------|-------------:|-----:|
| 33 | 30 | 31 |
| 34 | 30 | 31 |
| **35** | **30** | **31** |

Two consecutive runs after the migration: 28 then 31. 31/31 hit on
second run confirms the migration preserves behavior; 28 was natural
variance on gq-014/gq-017 phrase-fragile edges (the same band documented
since Phase 22).

---

## 3. R-P15-1 cumulative progress

| Slice | Theme | LOC freed | Status |
|-------|-------|----------:|--------|
| 33 | dash shared preamble | 47 | ✅ done |
| 34 | 4 dash task profiles | 144 | ✅ done |
| **35** | **5 colon prompts (preamble + 4 profiles)** | **~210** | ✅ **done** |
| 36 | cleanup: dispatch comments + audit close | ~20 | pending |

**Total LOC freed across slices 1-3: ~400 of expected ~420.** The
orchestrator.py file no longer carries any of the 10 inline
`_SYSTEM_PROMPT_*` prompt text blocks; all 10 are now imported from
the canonical `prompts/` tree. Slice 4 is small cleanup only.

---

## 4. Module composition pattern (final form)

All 10 migrated prompts follow the same shape:

```python
"""Phase N — slice of R-P15-1. Migrates the {variant} task profile."""

from __future__ import annotations
from app.agent.prompts.orchestrator_shared_preamble_{dash|colon} import (
    SYSTEM_PROMPT as _SHARED_PREAMBLE,
)

PROMPT_VERSION = "0.1.0"

_BODY = """
TASK PROFILE: ...
"""

SYSTEM_PROMPT = _SHARED_PREAMBLE + _BODY
```

Two shared preamble modules (one dash, one colon) act as the
foundation; 8 task-profile modules (4 dash + 4 colon) compose
on top. The orchestrator imports the 10 composed `SYSTEM_PROMPT`
constants for `_select_system_prompt()` to dispatch to.

---

## 5. Carry-overs for Phase 36

| ID | Item | Priority |
|----|------|----------|
| R-P15-1 slice 4 | Cleanup: remove dispatch-table comments referencing the inline pattern; update `phase15_orchestrator_prompts_audit.md` to mark the audit as resolved | Low — final small slice |
| R-P11-B | Frontend Search/Query page | Medium — user-driven |
| R-P21-CACHE-TELEMETRY-DASHBOARD | Cache skip-reason in operator dashboard | Low — paired with R-P11-B |

---

## 6. Files of record

```
src/fastapi/app/agent/prompts/orchestrator_shared_preamble_colon.py   (Step 1 — new)
src/fastapi/app/agent/prompts/orchestrator_default_colon.py            (Step 1 — new)
src/fastapi/app/agent/prompts/orchestrator_numeric_colon.py            (Step 1 — new)
src/fastapi/app/agent/prompts/orchestrator_narrative_colon.py          (Step 1 — new)
src/fastapi/app/agent/prompts/orchestrator_graph_colon.py              (Step 1 — new)
src/fastapi/app/agent/prompts/_version_registry.py                     (Step 1 — 5 new entries)
src/fastapi/app/agent/orchestrator.py                                  (Step 1 — 5 inline defs → 5 imports)
docs/phase35_handoff.md                                                 (this file)
scripts/phase35_master_sweep.sh                                        (Step 2)
scripts/phase35_step1_verify.sh                                        (Step 1)
```

End of Phase 35 handoff.
