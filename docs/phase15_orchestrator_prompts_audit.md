# Phase 15 Step 2 — Orchestrator inline-prompt audit

**Document version:** 1.1
**Status:** **RESOLVED at Phase 36 close (2026-05-12).**
All 10 inline prompt variants migrated to the canonical `prompts/`
tree across Phases 33-36 (R-P15-1 slices 1-4). The orchestrator no
longer carries any inline prompt text; all 10 variants are imported
as `SYSTEM_PROMPT` from dedicated `prompts/orchestrator_*_{dash,colon}.py`
modules.

**Migration summary**:
- Phase 33: dash shared preamble → `orchestrator_shared_preamble_dash.py`
- Phase 34: 4 dash task profiles → `orchestrator_{default,numeric,narrative,graph}_dash.py`
- Phase 35: colon shared preamble + 4 colon task profiles → `orchestrator_*_colon.py` (5 modules)
- Phase 36: dispatch-comment cleanup + this audit closure
- Total LOC freed from orchestrator.py: ~420
- Total prompt registry entries: 14 (4 pre-existing + 10 R-P15-1)

The historical content below documents the state at Phase 15 close
(2026-05-11) for reference.

---

**Predecessors:** `docs/phase11_section_04i_audit.md`,
`docs/phase12_handoff.md` (Phase 12 Step 2 + Phase 13 Step 1 +
Phase 14 Step 1 each migrated a single isolated prompt).

---

## 1. Why this audit

Phases 12-14 migrated three inline prompts from agent modules to
the canonical `app/agent/prompts/` tree:

| Phase | Migrated | Source file |
|-------|----------|-------------|
| 12 Step 2 | rephrase_system | `escalation.py` |
| 13 Step 1 | classifier_system | `llm_classifier.py` |
| 14 Step 1 | agent_system | `agentic_escalation.py` |

Each of those was an isolated module-level constant with a single
consumer. The remaining inline prompts all live in
`src/fastapi/app/agent/orchestrator.py` (5184 lines) and form a
tightly-coupled set of variants built from a shared preamble.
Phase 15 audits them; the actual migration is deferred to a
later phase that can take the multi-prompt refactor as its
primary scope.

---

## 2. Inventory — 10 variants in `orchestrator.py`

| Line | Constant | Composition |
|-----:|----------|-------------|
| 764 | `_SYSTEM_PROMPT_SHARED_PREAMBLE` | base preamble (security + rules + citations + impossible-premise rules) |
| 817 | `_SYSTEM_PROMPT_DEFAULT` | preamble + "general geological query" suffix |
| 843 | `_SYSTEM_PROMPT_NUMERIC` | preamble + "numerical / factoid" suffix |
| 889 | `_SYSTEM_PROMPT_NARRATIVE` | preamble + "narrative" suffix |
| 925 | `_SYSTEM_PROMPT_GRAPH` | preamble + "graph traversal" suffix |
| 970 | `_SYSTEM_PROMPT_STATIC` | alias = `_SYSTEM_PROMPT_DEFAULT` |
| 983 | `_SYSTEM_PROMPT_SHARED_PREAMBLE_COLON` | preamble (colon variant for some model families) |
| 1036 | `_SYSTEM_PROMPT_DEFAULT_COLON` | colon-preamble + default suffix |
| 1062 | `_SYSTEM_PROMPT_NUMERIC_COLON` | colon-preamble + numeric suffix |
| 1108 | `_SYSTEM_PROMPT_NARRATIVE_COLON` | colon-preamble + narrative suffix |
| 1144 | `_SYSTEM_PROMPT_GRAPH_COLON` | colon-preamble + graph suffix |

`_SYSTEM_PROMPT_VERSION` (line 745) is the cache-key version
gating all of these — the pre-commit hook
`system-prompt-version-bump` from Phase 5 Step 3 / Phase 11 Step 5
enforces a bump on any commit that touches these blocks.

The two `SHARED_PREAMBLE` variants differ in punctuation
("RULES FOR NUMBERS AND NAMES" vs "RULES FOR NUMBERS AND NAMES:")
to suit model-family-specific prompt-following quirks per
PV-02 (2026-04-21).

---

## 3. Why a one-shot migration is the right shape

Migrating these one at a time would leave hybrid state where
some variants live in `prompts/` and others stay inline — every
intermediate state would be confusing to readers. The clean
move is:

1. Create `app/agent/prompts/orchestrator_system.py` exporting all
   ten variants as module-level constants plus the four suffix
   constants.
2. Update `orchestrator.py` lines 764-1144 to import them via:
   ```
   from app.agent.prompts.orchestrator_system import (
       SYSTEM_PROMPT_SHARED_PREAMBLE,
       SYSTEM_PROMPT_DEFAULT,
       SYSTEM_PROMPT_NUMERIC,
       SYSTEM_PROMPT_NARRATIVE,
       SYSTEM_PROMPT_GRAPH,
       SYSTEM_PROMPT_SHARED_PREAMBLE_COLON,
       SYSTEM_PROMPT_DEFAULT_COLON,
       SYSTEM_PROMPT_NUMERIC_COLON,
       SYSTEM_PROMPT_NARRATIVE_COLON,
       SYSTEM_PROMPT_GRAPH_COLON,
   )
   _SYSTEM_PROMPT_SHARED_PREAMBLE = SYSTEM_PROMPT_SHARED_PREAMBLE
   _SYSTEM_PROMPT_DEFAULT = SYSTEM_PROMPT_DEFAULT
   # … etc
   ```
3. Register all ten variants in `_version_registry.py` under one
   `orchestrator_system` umbrella entry (or ten entries — TBD by
   the migration author).
4. Bump `_SYSTEM_PROMPT_VERSION` to 10 on the migration commit.

---

## 4. Migration carry-over

**R-P15-1** — bundled orchestrator prompt migration. Single
self-contained step. Pre-conditions:

- All 10 variants must move together.
- Test that the `select_system_prompt()` function (line 1211)
  still resolves to the right variant after migration.
- `_SYSTEM_PROMPT_VERSION` must bump in the same commit so the
  pre-commit `system-prompt-version-bump` hook is satisfied.

After R-P15-1 lands, the prompts/ registry should have ~13 entries
and ALL inline `_SYSTEM_PROMPT_*` constants in the agent tree
should be gone. R-P12-more-prompts can be marked CLOSED in the
Phase 15+1 handoff.

---

End of audit.
