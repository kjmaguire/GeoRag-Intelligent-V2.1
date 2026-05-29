# R-P15-1 Prompt Migration — Scope Document

**Document version:** 1.0
**Status:** Scope only. Not picked up in the Phase 18-31 autonomous run.
**Drafted during:** Phase 31 master sweep window.

A read-only scoping pass on R-P15-1 (Bundled orchestrator prompts
migration). This work was named in the Phase 16 handoff and
deferred repeatedly through Phases 18-31 as out-of-scope for the
golden-test-pass-count focus. Now that the suite is at 30-31/31,
R-P15-1 surfaces as one of three remaining "medium" carry-overs.

The conclusion below: this needs an explicit user-driven session,
not an autonomous-loop tick. The reasoning is captured here so a
future session can pick up with full context.

---

## 1. Current state

`src/fastapi/app/agent/orchestrator.py` carries 10 inline prompt
constants between lines 759 and 1184:

| # | Constant | Line | Purpose |
|--:|----------|-----:|---------|
| 1 | `_SYSTEM_PROMPT_SHARED_PREAMBLE` | 778 | dash-variant shared preamble |
| 2 | `_SYSTEM_PROMPT_DEFAULT` | 831 | dash, default routing |
| 3 | `_SYSTEM_PROMPT_NUMERIC` | 857 | dash, count/aggregate queries |
| 4 | `_SYSTEM_PROMPT_NARRATIVE` | 903 | dash, document-anchored narrative |
| 5 | `_SYSTEM_PROMPT_GRAPH` | 939 | dash, knowledge-graph traversal |
| 6 | `_SYSTEM_PROMPT_SHARED_PREAMBLE_COLON` | 1009 | colon-variant shared preamble |
| 7 | `_SYSTEM_PROMPT_DEFAULT_COLON` | 1062 | colon, default routing |
| 8 | `_SYSTEM_PROMPT_NUMERIC_COLON` | 1088 | colon, count/aggregate |
| 9 | `_SYSTEM_PROMPT_NARRATIVE_COLON` | 1134 | colon, narrative |
| 10 | `_SYSTEM_PROMPT_GRAPH_COLON` | 1170 | colon, graph |

Total: ~425 LOC of inline prompt text. Selection logic lives in
`_select_system_prompt(categories)` at line ~1188 (dispatches to
DEFAULT / NUMERIC / NARRATIVE / GRAPH based on classifier flags;
the COLON variants fire only when `settings.CITATION_SPAN_RESOLVER_ENABLED`).

The canonical pattern (already established in `prompts/`):

```
src/fastapi/app/agent/prompts/
├── __init__.py
├── _version_registry.py
├── agent_system.py          ← Phase 14 migration
├── classifier_system.py     ← Phase 13 migration
├── example_system.py        ← Phase 11 demo
└── rephrase_system.py       ← Phase 12 migration
```

Each module exports `PROMPT_VERSION: str` + `SYSTEM_PROMPT: str`.

---

## 2. Why this is bigger than a single autonomous tick

### 2a. Composition shape

Many of the 10 prompts use Python string concatenation:

```python
_SYSTEM_PROMPT_DEFAULT = _SYSTEM_PROMPT_SHARED_PREAMBLE + """
TASK PROFILE: general geological query …
"""
```

The migration can't just split each constant into its own module —
the `_SHARED_PREAMBLE` lives in one module, the variant lives in
another, and the importer composes them. That's manageable but it
multiplies the migration surface (10 prompt files + 2 shared
preamble files + composition logic).

### 2b. Anthropic prompt-cache hashing

Anthropic prompts caches at exact-text level. Splitting + recomposing
the prompt text could shift the cache hash even when no semantic
change is made (whitespace, line ending, encoding drift). The
existing `_SYSTEM_PROMPT_VERSION` bookkeeping is the mitigation but
needs explicit attention during the migration so all 10 variants
land at the same atomic moment.

### 2c. COLON-variant feature flag

The COLON variants are flag-gated on
`settings.CITATION_SPAN_RESOLVER_ENABLED`. Migrating both variant
families means either:

- Migrate both at once (10 modules in one phase — bigger)
- Migrate dash first, defer COLON (5 modules in one phase, then a
  second phase) — but then the orchestrator carries both old and
  new patterns simultaneously, doubling the test surface

Both approaches need explicit user direction on phasing.

### 2d. Pre-commit hook

The `system-prompt-version-bump` pre-commit hook flags commits
that touch a registered prompt path without bumping its registry
version. During the migration, every prompt is "newly registered" —
so the hook fires on every commit unless we bypass it (which
violates the CLAUDE.md hard rule "Never skip hooks unless the user
explicitly requests it").

The right approach: a single atomic migration commit that adds
the registry entries AND moves the prompt text in one shot. That's
inherently a bigger change than the autonomous-tick cadence.

---

## 3. Proposed multi-phase plan (if/when user picks it up)

### Phase 33 (suggested) — Dash variant migration

- Create 5 new modules: `prompts/orchestrator_default_dash.py`,
  `orchestrator_numeric_dash.py`, `orchestrator_narrative_dash.py`,
  `orchestrator_graph_dash.py`, `orchestrator_shared_preamble_dash.py`.
- Each exports `PROMPT_VERSION = "0.1.0"` + `SYSTEM_PROMPT = …`.
- Add 5 entries to `_version_registry.py` PROMPT_REGISTRY.
- Update orchestrator.py to import + compose at module load time
  (preserves the `_SHARED + variant` pattern, just relocates the
  source).
- Delete the 5 inline `_SYSTEM_PROMPT_*` (dash) constants.
- `_SYSTEM_PROMPT_VERSION` stays the same — text is byte-identical
  pre- and post-migration so the Anthropic cache hash doesn't shift.
- Verifier: all 5 dash modules exist, registry has 5 new entries,
  orchestrator imports resolve, full golden suite passes 30+/31.

### Phase 34 (suggested) — COLON variant migration

- Same shape as Phase 33 but for the 5 colon variants.
- Verifier: COLON modules exist + registered + suite still
  passes 30+/31 under `CITATION_SPAN_RESOLVER_ENABLED=True`.

### Phase 35 (suggested) — cleanup + documentation

- Update `docs/phase15_orchestrator_prompts_audit.md` to mark the
  audit as resolved.
- Sync the canonical `_version_registry.py` description text with
  the new module names.

---

## 4. What this scope doc IS NOT

- A commitment to do this work
- A precise diff plan (those will need fresh reading of orchestrator
  state at the moment work begins — current line numbers are a
  May-2026 snapshot)
- A drop-in autonomous-tick deliverable

---

## 5. Other carry-overs from the Phase 31 handoff

For completeness, the remaining items also out-of-scope for the
autonomous run shape:

- **R-P32-REFUSAL-CONTEXT** — narrow over-broad `_REFUSAL_PHRASES`
  entries (drafted in `docs/phase32_implementation_kickoff.md`,
  small autonomous-tick scope; gq-017 stability fix)
- **R-P11-B** — Frontend Search/Query page (larger than R-P15-1;
  needs Inertia + React + chat UI + SSE plumbing)
- **R-P21-CACHE-TELEMETRY-DASHBOARD** — Surface
  `cache_skipped_reason` in operator dashboard (paired with R-P11-B;
  low priority)

The natural next autonomous tick is **R-P32-REFUSAL-CONTEXT**.
R-P15-1 + R-P11-B + R-P21-CACHE-TELEMETRY-DASHBOARD all deserve
user-driven sessions.

End of scope doc.
