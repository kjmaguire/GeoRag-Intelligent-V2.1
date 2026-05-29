## Doc-phase 94 handoff — §9.11 + §9.13 final §9 workflows

**Status:** Complete. Both workflows registered in AI pool. §9 backend
skeleton tree now fully scaffolded.

## What landed

### §9.11 — `field_outcome_learning` Hatchet workflow

`src/fastapi/app/hatchet_workflows/field_outcome_learning.py`:
- `FieldOutcomeLearningInput` (workspace_id, project_id, outcome_ids)
- `FieldOutcomeLearningOutput` (success, backtests_written,
  lessons_written, retraining_triggered, error)
- 2h execution_timeout; retries=1.
- Body raises NotImplementedError.
- Pattern matches `score_targets` + `generate_report`.

Trigger: new `targeting.target_outcomes` rows. Updates target-model
backtests + writes `silver.decision_lessons_learned` rows tied to
the original sign-off decision. Phase 12 future: triggers XGBoost
retraining at threshold.

### §9.13 — `what_changed_detector` Hatchet workflow

`src/fastapi/app/hatchet_workflows/what_changed_detector.py`:
- `WhatChangedInput` (workspace_id, optional project_id, time window,
  detect_request_id for idempotency)
- `WhatChangedOutput` with counts for new ingestions, new/updated
  public records, new claims, target score shifts, optional
  downstream report_request_id.
- 30m execution_timeout; retries=1.

Feeds the existing §7.2 `what_changed` report template (already in
`templates.py` from doc-phase 82). Cadence configurable per
workspace (per Kyle's tabled question default = weekly).

### Worker registration

Both workflows added to the AI pool in `worker.py`. `worker --list`
confirms all four §7/§8/§9 long-running workflows are visible:

    generate_report
    score_targets
    field_outcome_learning
    what_changed_detector

## Master-plan §9 progress (FINAL — backend-side)

| Sub-step | Status |
|---|---|
| 9.0 scope | ✅ |
| 9.1 ontology schema | ✅ |
| 9.2 ontology seeds | ✅ |
| 9.3 SME ontology population | pending (Kyle) |
| 9.4 hypotheses schema | ✅ |
| 9.5 hypothesis agent | ✅ skeleton |
| 9.6 spatial relationship | ✅ skeleton |
| 9.7 next-best-data | ✅ skeleton |
| 9.8 analogue finder | ✅ skeleton |
| 9.9 decision intelligence schema | ✅ |
| 9.10 decision capture facade | ✅ skeleton |
| 9.11 field_outcome_learning workflow | ✅ skeleton |
| 9.12 data lineage graph UI | pending (frontend; waits for Kyle) |
| 9.13 What Changed detector workflow | ✅ skeleton |
| 9.14 acceptance test | pending |

**12 of 14 §9 sub-steps closed (86%)** — only frontend (9.12) and
acceptance (9.14) remain. Acceptance test sits at the end of the
phase by design.

## Recommended next ticks

§9 backend autonomous-safe work is FUNCTIONALLY COMPLETE. Next:

**Doc-phase 95** = §10 (Eval harness + Customer Support Cockpit)
scope proposal. Continues the §5-§9 scope-proposal pattern. §10
is the next master-plan phase per the deliverable list.

After §10 scope: §11 (DR + perf hardening), §12 (XGBoost + advanced
learning).

## Carry-overs

Unchanged from prior §9 ticks:
1. **Unified image rebuild** — geopandas + rasterio + mplstereonet +
   langgraph + weasyprint + python-docx + openpyxl.
2. **Kyle SME content** — §8.3 Athabasca + §8.7 weights + §8.9
   sign-off mechanism + §9.3 ontology population.
3. **Activepieces install** — gates §7.11 + §8.11 + What Changed
   delivery layer.
4. **Frontend ticks** (§6.7-6.14, §7.12-7.15, §8.10, §9.12) — wait
   for Kyle's product-feel review.
