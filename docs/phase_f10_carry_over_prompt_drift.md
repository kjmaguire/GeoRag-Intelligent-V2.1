# Phase F.10 — Investigated + carry-over (prompt-drift discovery)

**Status:** Investigation complete; structural extraction reverted to
preserve the 9/10 golden-eval baseline. The underlying drift remains as
a documented carry-over.

## What I found

The orchestrator carries **two parallel copies of every system prompt**:

1. **Inline** in `app/agent/orchestrator.py` (lines 192-614 in the
   current file) — the strings actually used at runtime because that's
   what `_select_system_prompt` returns.
2. **Package** under `app/agent/prompts/orchestrator_*_{colon,dash}.py`
   — never imported by the orchestrator, so edits there silently no-op.

Every "Phase 33 Step 1" migration docstring in the prompts/ package
claims `"Text byte-identical to the previous inline ..."`. That was true
at migration time. Since then:

* **doc-phase 185** added rule 4b (canonical entity naming) to the
  prompts/ package only — never made it into the inline copies.
* **Phase F.9** authored a rule 5b + metadata-question few-shots in the
  prompts/ package + bumped `PROMPT_VERSION` 0.2.0 → 0.3.0 — also
  never reached the inline copies.

Result: Phase F.9's prompt edits were effectively dead code. The 9/10
eval jump from Phase F.9 came **entirely from the new `query_project_overview`
tool wiring + the PROJECT OVERVIEW context block** — the prompt rule
5b and the metadata few-shots had zero effect on the eval result.

## What I tried in F.10

A `prompt_builders.py` module that imports `SYSTEM_PROMPT` from each
prompts/ package file + re-exports it, then deletes the inline copies
from orchestrator.py. The structural extraction worked (4534 → 3883
LOC) but the eval **regressed to 4/10**: Q3 (drill-hole total depth),
Q4 (logged date), Q5/Q8 (curve catalog) refused with "I don't have
data on that in this project" even though the spatial collar query
returned the answer.

The regression source is **rule 4b** (canonical entity naming, added
doc-phase 185 to prompts/ only). When activated against drill-hole
queries with the new project_overview tool context simultaneously
present, the model becomes over-conservative and matches the legacy
refusal magnet ("I don't have data on that in this project") rather
than reading the spatial data.

Reverting the prompts/ package to the c012a9d snapshot **also** broke
the eval (Q5/Q7/Q8 regressed) because that c012a9d state predated
Phase F.9's tool wiring — so rolling back picked up an orchestrator
without `project_overview` dispatch.

## Resolution

Reverted both `orchestrator.py` + `prompts/` to the c012a9d snapshot,
then **manually re-applied just the Phase F.9 orchestrator changes**
(import + `if categories.get("project_overview"):` dispatch + the
`_build_context` `isinstance(result, ProjectOverviewResult)` branch).
That restored the 9/10 baseline with the inline prompts still serving
runtime traffic.

Canary post-revert: **244 / 0** (same as pre-F.10).

## Carry-over for a future tick

Resolving the drift properly requires **prompt-by-prompt validation**:

1. For each of the 8 prompt variants
   (default/numeric/narrative/graph × dash/colon), compare the inline
   c012a9d text to the prompts/ package text. Identify which divergence
   points are intentional (doc-phase 185, F.9) and which are accidental.
2. Author a single canonical text per variant in the prompts/ package
   that includes the intentional improvements WITHOUT introducing the
   over-conservative drift pattern that breaks drill-hole queries.
3. Re-run the 10-question eval after each variant update. Don't proceed
   to the next variant until the eval is at least 9/10.
4. Once all 8 variants ship in the package + pass the eval, repeat the
   F.10 structural extraction.

Expected cost: 2-4 hours of focused work (one variant ≈ 20-30 min of
edit-verify cycles).

## Why this matters

The drift isn't just cosmetic — it means **future prompt edits made to
the prompts/ package don't reach production**. Any author who edits a
file under `app/agent/prompts/orchestrator_*` expecting the LLM to see
the change is silently misled. Either:

* Delete the prompts/ package entirely and keep only the inline copies
  (admit that the migration was incomplete), or
* Complete the migration as above and delete the inline copies.

Until then, the inline copies in orchestrator.py are the canonical
source of truth for the runtime prompt text.

## Files touched (reverted to c012a9d state)

* `src/fastapi/app/agent/orchestrator.py` — back to pre-F.10 shape,
  plus re-applied F.9 dispatch (15 LOC)
* `src/fastapi/app/agent/prompts/*.py` — back to c012a9d state

## Files I won't ship

* `src/fastapi/app/agent/prompt_builders.py` (deleted) — the extraction
  module worked structurally but the eval regression makes it
  unshippable without prompt reconciliation first.

## What this DOES unblock

The F.6 / F.7 / F.8 refactors stand — they didn't touch prompts. The
remaining F.11 (`context_builder.py`) + F.12 (`llm_calls.py`) + F.13
(package rename) extractions also don't touch prompts, so they can
proceed without this carry-over blocking them.
