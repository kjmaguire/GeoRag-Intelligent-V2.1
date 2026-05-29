# Phase 72-73 Handoff — Chart export contract spec + §5 agent skeletons

**Status:** Both ticks complete. Combined handoff to be efficient on
context.

## Doc-phase 72: §17.4 chart export contract spec

`docs/chart_export_contract_spec.md` — reads master-plan §17.4 +
§17.5, locks the implementation contract for §5.9.

Key deliverable: a `ChartExportPayload` Pydantic model that EVERY
chart-producing endpoint (§5.6-5.8 + later) MUST return. The
Pydantic response_model validation IS the §17.4 enforcement gate —
FastAPI refuses to send a response missing any required field.

The 6 required fields per §17.4:
- source_data (gold tables + row IDs + external sources)
- method (free-text describing generation)
- filters (any applied filters)
- crs (when spatial)
- citations (list)
- confidence_warnings (per-data-point)

Also captured §17.5's 6 visual agents — §5 ships 2 of them
(Drillhole Visual QA + Visual Readiness); the other 4 ship in
later phases.

## Doc-phase 73: §5.10 + §5.11 agent skeletons

New module `app/agents/phase5/`:
- `drillhole_visual_qa.py` — `@georag_agent` decorated; risk_tier
  R1; takes `collar_id`, returns visualization-readiness report.
  Skeleton (`NotImplementedError` body).
- `visual_readiness.py` — `@georag_agent` decorated; risk_tier R1;
  takes `viz_kind` + `collar_id` or `project_id`, returns
  ready/supported/message structure. Skeleton.
- `__init__.py` — re-exports both

Both imports clean inside the running georag-fastapi container.
Both follow the existing phase0 agent pattern (`@georag_agent`
decorator from `app.agents`).

## Why skeletons not implementations

The agents call into §5.6-5.8 visualization endpoints + read
gold.* tables. The endpoints don't exist yet (need image rebuild
for geopandas/rasterio per doc-phase 71). The gold tables exist
but are empty (need Dagster assets).

Skeleton-now-implement-later is the same pattern as doc-phase 49
(all 8 OCR module skeletons) followed by doc-phases 51-54 graduating
them. Interface contract locked; behavior lands when callers exist.

## Master-plan §5 progress

| Sub-step | Status |
|---|---|
| 5.1 dep audit | ✅ DONE |
| 5.2 deps in pyproject | ✅ PARTIAL (rebuild pending) |
| 5.3-5.5 gold tables | ✅ DONE |
| 5.6-5.8 viz endpoints | pending (needs rebuild) |
| 5.9 chart export contract spec | ✅ DONE (spec only; enforcement in 5.6-5.8) |
| 5.10 Drillhole Visual QA Agent | ✅ skeleton |
| 5.11 Visual Readiness Agent | ✅ skeleton |
| 5.12 React frontend | pending |
| 5.13 acceptance | pending |

**8 of 13 sub-steps closed** (4 done, 1 partial, 3 skeleton/spec).
Remaining 5 are visualization endpoints + frontend + acceptance.

The visualization endpoints + frontend NEED the fastapi image rebuild
to bring in geopandas + rasterio + mplstereonet. That's the main
unblock for the next ticks.

## Carry-overs for next ticks

1. **fastapi image rebuild** — still pending; blocks §5.6-5.8
2. **Dagster assets** to populate the 3 gold tables — separate work,
   not strictly blocking endpoints (endpoints can return empty
   results when no rows exist; QA agents flag that case)
3. **§5.12 React frontend** — needs the endpoints first
4. **§5.13 acceptance test** — needs the 20-collar fixture exercising
   the full chain

## Recommended next tick

Doc-phase 74 = open §6 (PublicGeo + MapLibre) scope proposal OR
write §5 Dagster assets for the gold tables. The Dagster work is
substantial (each asset = ~100-200 lines + tests). §6 scope proposal
is cheaper + sets up the next phase.

Per the autonomy grant ("push through more phases"), §6 scope
proposal is the higher-leverage choice — it advances the master-plan
forward without waiting on the image rebuild.
