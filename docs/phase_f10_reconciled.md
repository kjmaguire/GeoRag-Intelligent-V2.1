# Phase F.10 — Prompt drift reconciled (mirror-to-inline path)

**Status:** All 10 prompt variants now match inline byte-for-byte. The
prompts/ package is an explicit mirror — runtime truth remains the
inline `_SYSTEM_PROMPT_*` constants in `orchestrator.py`. Future-work
grafts (rule 4b, graph matched-entity coaching) are documented in
`docs/phase_f10_carry_over_prompt_drift.md`.

## What the carry-over flagged

`docs/phase_f10_carry_over_prompt_drift.md` documented that the
orchestrator carries **two parallel copies of every system prompt**:

1. **Inline** in `orchestrator.py` — the runtime source. `_select_system_prompt`
   returns these strings.
2. **Package** under `app/agent/prompts/orchestrator_*_{dash,colon}.py`
   — never imported by the orchestrator; every edit silently dead code.

Doc-phase 185 (rule 4b — CANONICAL ENTITY NAMING) and Phase F.9 (rule 5b
+ metadata few-shots + graph matched-entity coaching) all landed in
the package only.

## What this pass did

**Reverse-mirror reconciliation.** Rather than try to graft the package
improvements into inline and re-validate (per F.10's variant-by-variant
plan, blocked on eval flakiness — see
`docs/phase_g_followup_eval_matcher_tightening.md`), this pass updates
each package file to match its inline counterpart byte-for-byte. The
package is now an honest mirror, and each file's module docstring
contains:

* A ⚠️ banner stating it is NOT the runtime source of truth
* A pointer to the inline constant that IS
* A drift log listing the improvements that were reverted and why

This unblocks the F.13 (package rename) extraction without
re-introducing the F.10 regression.

### Files touched

| File | Bytes before | Bytes after | Removed |
|---|---|---|---|
| `orchestrator_shared_preamble_dash.py` | 3830 | 3234 | rule 4b CANONICAL ENTITY NAMING (596 B) |
| `orchestrator_shared_preamble_colon.py` | 3830 | 3234 | rule 4b (596 B) |
| `orchestrator_graph_dash.py` body | 1380 over inline | 0 | matched-entity coaching + VERBATIM-property rule + "What type of deposit is the Triple R?" example |
| `orchestrator_graph_colon.py` body | 1380 over inline | 0 | same as dash |

DEFAULT, NUMERIC, NARRATIVE bodies were already byte-identical to their
inline counterparts — only the cascading shared_preamble drift affected
their composed totals.

### Parity verification

```
SHARED_PREAMBLE_DASH      MATCH (3234 bytes)
SHARED_PREAMBLE_COLON     MATCH (3234 bytes)
DEFAULT_DASH              MATCH (4306 bytes)
DEFAULT_COLON             MATCH (4306 bytes)
NUMERIC_DASH              MATCH (5382 bytes)
NUMERIC_COLON             MATCH (5382 bytes)
NARRATIVE_DASH            MATCH (5210 bytes)
NARRATIVE_COLON           MATCH (5210 bytes)
GRAPH_DASH                MATCH (5449 bytes)
GRAPH_COLON               MATCH (5449 bytes)
```

10 / 10 match.

### Canary suites

* `tests/test_context_packing.py` — 4 / 4 pass
* `tests/test_response_assembler_pgeo.py` — 19 / 19 pass

(End-to-end suites that hit `run_deterministic_rag` —
`tests/test_hallucination_failures.py` — exhibit the same sequential-
call flakiness documented in
`docs/phase_g_followup_eval_matcher_tightening.md`, independent of any
prompt change. Investigating the orchestrator state pollution is the
right next step before re-grafting improvements.)

## What's still on the carry-over

The genuinely useful improvements that this reconciliation reverted out
of the package are:

1. **Rule 4b — CANONICAL ENTITY NAMING.** Forces the model to refer to
   graph-resolved entities using their EXACT capitalisation as listed
   in the project preamble's "Top project entities". Reverted because
   F.10 found it makes the model over-conservative on drill-hole
   queries (Q3/Q4/Q5/Q8) when project_overview tool context is also
   present. Needs few-shots that show 4b firing on graph entities
   without suppressing spatial answers.

2. **GRAPH matched-entity property surface coaching** (Phase 20 R-P19-A3
   + Phase 22 R-P20-PROMPT). Teaches the model to find the row marked
   `◉ (matched entity)` in graph results and quote its property bag
   verbatim. Lower-risk than rule 4b because it's content-only (no new
   refusal pattern), so it's a likely candidate to graft first once
   the eval is stable.

Both improvements live in git history (pre-revert state) for whoever
picks up the next pass.

## Why "mirror" and not "graft"

The variant-by-variant graft+validate loop from the F.10 carry-over
needs a stable eval signal. Item 4 of this batch
(`docs/phase_g_followup_eval_matcher_tightening.md`) discovered that the
22-question eval flaps between 11/22 and 18/22 across runs with no code
changes — a sequential-call state pollution we haven't bisected yet.
Without a reliable before/after eval comparison, grafting prompt
changes is gambling.

The mirror approach gives Kyle a clean, documented baseline. The next
focused session can either:

* Fix the eval flakiness first, then graft both improvements with
  confidence; or
* Audit the F.10 carry-over offline (manual SME prompt review on the
  Cameco corpus) and graft the lower-risk graph coaching without
  touching rule 4b.

## What this unblocks

* **F.13 — package rename.** Per F.10's note, F.13 was waiting on
  reconciliation. The package is now consistent + honest about its
  status, so F.13 can proceed.
* **Future prompt edits.** Until the import migration completes, the
  ⚠️ MIRROR FILE banner in every package docstring signals to authors
  that production edits go in `orchestrator.py`, not here. The drift
  log section in each file gives them a place to record any further
  improvements they intend to land later.
