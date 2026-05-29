# Phase F.9 — `query_project_overview` tool wiring + prompt rule 5b

**Status:** Complete. Golden eval **6/10 → 9/10** (+50% pass rate).

## What landed

A new structured tool `query_project_overview(project_id)` that surfaces
the silver-tier project metadata + dataset capability fields the
deterministic classifier previously couldn't reach:

* `silver.projects.company`, `commodity`, `region` (county + state in
  one column, e.g. `"CARBON, WY"`), `slug`, `project_name`
* Distinct log-curve names from `silver.well_log_curves` joined via
  `silver.collars.project_id`
* Total drillhole count

The classifier routes to it when the query contains any of ~40
project-overview keywords (company, county, state, "what
measurements", "does the dataset", "uranium grade measurements", etc.).

## Files changed

| Layer | File | Change |
|---|---|---|
| Data type | `src/fastapi/app/agent/tools.py` | `ProjectOverviewResult` dataclass + `query_project_overview()` async fn |
| Classifier | `src/fastapi/app/agent/query_classification.py` | `_PROJECT_OVERVIEW_KEYWORDS` set + new `project_overview` boolean in `_classify_query()` output |
| Orchestrator | `src/fastapi/app/agent/orchestrator.py` | Dispatch branch under `if categories.get("project_overview"):` |
| Context | `src/fastapi/app/agent/orchestrator.py` | `_build_context` formats `ProjectOverviewResult` as a "PROJECT OVERVIEW" block with explicit county/state split |
| Helpers | `src/fastapi/app/agent/tool_result_helpers.py` | `_is_empty_tool_result` + `_build_retrieval_summary` register new type |
| Assembler | `src/fastapi/app/agent/response_assembler.py` | `_extract_source_id`, `_extract_document_title`, `_extract_relevance` for new type |
| Citation | `src/fastapi/app/agent/citation_binding.py` | `query_project_overview` → DATA / postgis |
| System prompt | `src/fastapi/app/agent/prompts/orchestrator_shared_preamble_{colon,dash}.py` | Rule 5b — PROJECT OVERVIEW SCOPE. PROMPT_VERSION 0.2.0 → 0.3.0 |
| Few-shots | `src/fastapi/app/agent/prompts/orchestrator_default_{colon,dash}.py` | Replaced the "Toronto weather" refusal magnet with positive metadata few-shots + a less generic refusal escape hatch |
| Cache | `src/fastapi/app/agent/orchestrator.py` | `_SYSTEM_PROMPT_VERSION` 9 → 10 |

## Numbers — before vs after

| Question | Pre-F.9 | Post-F.9 | What changed |
|---|---|---|---|
| 1 — What company drilled the holes in section 28N 79W? | FAIL | **FAIL** | Tool data is present in context (CAMECO RESOURCES) but Qwen3 still refuses, matching the SECURITY clause's refusal pattern on "section 28N 79W" geographic syntax. Documented in carry-overs. |
| 2 — How many drill holes? | PASS | PASS | unchanged |
| 3 — Total depth of 36-1042? | PASS | PASS | unchanged |
| 4 — When was 36-1042 logged? | PASS | PASS | unchanged |
| 5 — What geophysical measurements? | FAIL | **PASS** | curve catalog now in context |
| 6 — Max drilled depth? | PASS | PASS | unchanged |
| 7 — What county and state? | FAIL | **PASS** | county + state extracted from `region` field |
| 8 — Dataset includes uranium grade? | FAIL | **PASS** | curve catalog + commodity in context |
| 9 — Total uranium production rate? | PASS (refusal) | PASS (refusal) | correctly still refuses |
| 10 — What deposit type? | PASS | PASS | unchanged |

**Total: 6/10 → 9/10**, 3 newly-passing, 0 regressions.

## Why Q1 still fails (and what would fix it)

Verified through diagnostic harness `tmp/f9_diag_q1.py`:

1. Classifier: `project_overview: True` ✓
2. Tool fires: `company=CAMECO RESOURCES region=CARBON, WY collars=63 curves=16` ✓
3. PROJECT OVERVIEW block reaches the LLM context ✓
4. LLM still emits: *"I can only answer geological questions about this project's exploration data."*

The model is matching the SECURITY clause's refusal pattern despite
my prompt updates. Possible causes:

* Qwen3-Instruct (the family — current build is `Qwen/Qwen3-14B-AWQ`;
  this debug was originally on the 30B-A3B variant but the same refusal
  alignment is present in the 14B dense build) is alignment-tuned toward
  refusal when query shape is unusual ("section 28N 79W" is a PLSS
  township-range syntax, not common English).
* The SECURITY clause's literal refusal text is *exactly* what the
  model emits — it's a magnet.
* Temperature 0.10 makes the refusal pattern sticky.

Three credible fixes for a follow-up phase:

1. **Switch the model to a less alignment-aggressive variant for
   metadata queries** — e.g. route project_overview to the Anthropic
   Sonnet path which doesn't have this magnet behavior.
2. **Reword the SECURITY clause** so it doesn't share text with the
   metadata-refusal example. The refusal escape hatch text "I can
   only answer geological questions" is the magnet; replacing it
   with a distinct phrase would let the metadata few-shots win.
3. **Lower the question's ambiguity at the eval-pack level** —
   "What company drilled the holes in this project?" (no "section
   28N 79W") almost certainly passes given the few-shot match.
   But this is changing the test, not the system.

None of those changes are scoped here. The +3-question improvement
ships now; Q1 is filed as a known model-behavior issue.

## Test impact

Canary suite: **201 / 0** (no regressions, all pre-F.9 passing tests
still pass). Phase F.8 + F.5 + F.4 + F.5b verification scripts all
still clean.

## Architectural notes for future tool additions

The Phase F.9 pattern is now the template for "structured-tool wiring":

1. Add a `Result` dataclass to `tools.py` with a `count` attribute so
   the Phase F.4 empty-result filter handles it automatically.
2. Add an async query function with `@_metered("query_name")`.
3. Add classifier keywords to `query_classification.py`.
4. Add a `categories[name]` boolean + an `if categories.get(name):`
   dispatch branch in `orchestrator.py`.
5. Add an `_extract_source_id` / `_extract_document_title` /
   `_extract_relevance` branch in `response_assembler.py`.
6. Add to `_is_empty_tool_result` + `_build_retrieval_summary` in
   `tool_result_helpers.py`.
7. Add to `_TOOL_NAME_TO_KIND` + `_TOOL_NAME_TO_STORE` in
   `citation_binding.py`.
8. Add an `isinstance(result, NewResult):` branch in
   `_build_context` (still in `orchestrator.py` until Phase F.11).
9. Optionally add a system-prompt rule + few-shot if the LLM
   behaviour needs steering.

Steps 1–8 are mechanical and ~30 minutes. Step 9 is the LLM-tuning
half-day. The orchestrator refactor (F.10 → F.13) will eventually
extract `_build_context` into its own module, simplifying step 8.
