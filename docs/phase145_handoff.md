## Doc-phase 145 handoff — generate_report + score_targets Hatchet task bodies graduated

**Status:** Live + 6/6 pytest cases. **87/87 substrate verifier**.

## What landed

Bridged the Hatchet workflow layer to the LangGraph pipelines wired
in doc-phase 141. Two Hatchet workflows (`generate_report` doc-phase 83
skeleton, `score_targets` doc-phase 88 skeleton) now have **real task
bodies** that build the compiled LangGraph and `.ainvoke()` it.

### `app/hatchet_workflows/generate_report.py` — graduated

- Builds initial `ReportBuilderState` from the input + report-type registry
- Calls `build_report_builder_graph()` (doc-phase 141)
- Invokes the §15.1 planning pipeline (4 graduated nodes)
- Returns `GenerateReportOutput` with:
  - `success` / `failure_reason` / `failure_stage` (best-effort inferred)
  - `planned_sections_count`, `section_drafts_count`, `evidence_items_count`
  - `sign_off_required` (true for R4/R5 report types)
  - PDF/DOCX/XLSX URIs left None (still-skeleton nodes 5-13)

### `app/hatchet_workflows/score_targets.py` — graduated

- Builds initial `TargetRecommendationState` from input
- Calls `build_target_recommendation_graph()` (doc-phase 141)
- Invokes the §18.2 scoring pipeline (6 graduated nodes including
  §8.7 weighted-formula math)
- Returns `ScoreTargetsOutput` with:
  - `success` / `failure_reason` / `failure_stage`
  - `candidate_zone_count`, `recommended_target_count`
  - `target_model_slug` (selected by `select_commodity_deposit_model`)
  - `top_aggregate_score` (highest ranked target's score)
- Accepts `extra_candidate_zone_wkts` until `generate_candidate_zones`
  graduates; when that happens the wiring auto-populates zones from
  the AOI + evidence layers and this field becomes optional.

### New `target_commodity` input field on score_targets

Lets callers (Laravel queue, Activepieces flow) hint a commodity
without pinning to a specific deposit model slug. The
`select_commodity_deposit_model` node uses this hint to pick from
DEPOSIT_MODEL_TEMPLATES.

## Tests — `src/fastapi/tests/test_hatchet_workflow_bodies.py`

**6 pytest cases, all green:**

`generate_report` (3):
- `test_generate_report_task_body_runs_planning_pipeline` — happy path with weekly_project_digest, asserts planned + draft + evidence counts
- `test_generate_report_task_body_marks_signoff_for_r5_reports` — R5 target_recommendation → sign_off_required=true
- `test_generate_report_task_body_runs_all_11_report_types` — all 11 §15.2 report types complete the planning pipeline

`score_targets` (3):
- `test_score_targets_task_body_with_zones_produces_ranked_output` — 3 zones → 3 ranked targets, all with §8.7 aggregate scores in [0,1]
- `test_score_targets_task_body_with_commodity_hint_selects_model` — `target_commodity='Au'` → gold deposit model selected
- `test_score_targets_task_body_with_no_zones_returns_success_with_zero_counts` — empty zones list → clean success

Tests invoke task bodies via `task.aio_mock_run(input_obj)` — the
public Hatchet API for unit-testing task bodies without a running
Hatchet engine.

## Smoke verification

```bash
docker exec georag-fastapi python -m pytest tests/test_hatchet_workflow_bodies.py -v
# → 6 passed in 2.32s

bash scripts/autonomous_run_substrate_verify.sh
# → 87/87 checks passed
```

## Cumulative session state

- **Doc-phase ticks this run:** 145
- **Hatchet workflow skeletons graduated:** **3 of 11**
  (evaluate_workspace, generate_report, score_targets)
- **§25.4 support agents graduated:** 5 of 5 (closed in doc-phase 144)
- **§18.2 nodes graduated:** 6 of 12
- **§15.1 nodes graduated:** 4 of 12
- **§21.3 capture hooks wired:** 1 of 8
- **Reasoning agent skeletons graduated:** 1
- **LangGraph wirings live:** 2 of 2
- **Live pytest cases:** 161 (155 + 6)
- **Substrate verifier:** **87/87 PASS**

## End-to-end signal flow now live

For the first time, a caller can fire either workflow + get back a
real result that exercises the graduated graph:

```text
Laravel queue / Activepieces flow
  → Hatchet engine picks up the workflow
  → Workflow task body builds + invokes the §15.1 / §18.2 LangGraph
  → Compiled graph runs the graduated nodes (4 / 6 respectively)
  → Returns structured output with partial-state counts + failure_reason
  → Hatchet engine returns the output to the caller
```

When the still-skeleton graph nodes graduate, the wirings extend and
the workflow bodies pick up the new state fields automatically (the
Pydantic output model has placeholder fields ready for them).

## What's next

- **Doc-phase 146** — graduate `support_replay` Hatchet task body
  using the §25.4 agent suite (chain triage→investigate→packet→draft→route)
- **Doc-phase 147+** — open scope:
  - LLM integration for the §15.1 remaining 8 nodes
  - PostGIS spatial pipeline for §18.2 `generate_candidate_zones`
  - §6 BC MINFILE adapter (first real public_geoscience ingestion)
  - Real evaluator for §10.4 (replace synthetic_stub with §04i pipeline)

## Carry-overs

- Both task bodies use a 24h `execution_timeout` (Hatchet warns about
  the deprecated string format; v2 SDK wants `timedelta` instead).
  Cosmetic update — file a Hatchet upgrade ticket alongside the
  SDK bump.
- Both bodies rebuild the LangGraph per-invocation. For high-throughput
  callers, caching the compiled graph at module scope would save a
  few ms per call. Not a bottleneck today.
