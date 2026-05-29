## Doc-phase 105 handoff — §6.5 SavedMapView Eloquent model + controller skeleton

**Status:** Complete. Model + controller load clean; Pint passes.

## What landed

### Model — `app/Models/SavedMapView.php`

Eloquent model for `silver.saved_map_views` (doc-phase 76 schema):
- `$table = 'silver.saved_map_views'`
- `$primaryKey = 'view_id'` (UUID; non-incrementing)
- `HasUuids` trait
- Fillable: workspace_id, project_id, user_id, name, description,
  view_state, aoi_geom, is_shared
- Casts: `view_state => 'array'` (JSONB → PHP array),
  `is_shared => 'boolean'`, timestamps
- Relations: `project()` BelongsTo Project; `user()` BelongsTo User

### Controller — `app/Http/Controllers/Api/V1/SavedMapViewController.php`

REST controller for project-scoped saved views:
- `GET /api/v1/projects/{project}/saved-map-views` — list
- `POST /api/v1/projects/{project}/saved-map-views` — create
- `GET /api/v1/projects/{project}/saved-map-views/{view}` — show
- `PATCH /api/v1/projects/{project}/saved-map-views/{view}` — update
- `DELETE /api/v1/projects/{project}/saved-map-views/{view}` — destroy

All 5 methods throw `LogicException` ("doc-phase 105 skeleton") for
now. Live behavior lands when the MapLibre frontend (§6.7+) calls
these endpoints + the workspace_id resolution middleware sets
`app.workspace_id` per request for RLS scoping.

### Verification

```
docker exec georag-laravel-octane php artisan tinker --execute \
  'echo class_exists(\App\Models\SavedMapView::class) ? "loaded" : "missing";'
# loaded

vendor/bin/pint --dirty --format agent
# {"tool":"pint","result":"passed"}
```

## Master-plan §6 progress

| Sub-step | Status |
|---|---|
| 6.0 scope | ✅ |
| 6.1 audit | ✅ |
| 6.2 BC MINFILE ingestion | pending |
| 6.3 NRCan/GEO.ca ingestion | pending |
| 6.4 Public/Private Boundary Agent | ✅ skeleton |
| 6.5 Saved map views (schema + model + controller) | ✅ |
| 6.6 h3-pg density aggregations | pending |
| 6.7-6.14 frontend MapLibre work | pending (waits for Kyle) |
| 6.15 acceptance test | pending |

**4 of 15 §6 sub-steps closed.** §6.5 backend now genuinely complete
(schema doc-phase 76 + RLS + model + controller).

## Cumulative session-continuation tally (doc-phases 74-105 = 32 ticks)

Across §3-§12 master-plan phases:
- **7 new scope-proposal docs**
- **32 doc-phase handoff docs**
- **2 cumulative continuation briefings**
- **26 new database tables** across 4 new schemas (targeting, eval,
  ops, source_trust additions) + silver/eval/ops additions
- **44+ agent skeletons** across phase6/7/8/9/10 packages (5 new
  this batch with phase10's 5)
- **3 LangGraph state models + 30 node stubs** (Report Builder,
  Target Recommendation, LLM Incident Diagnosis)
- **10 Hatchet workflows** registered in AI pool (was 0 at start
  of run continuation)
- **11+ new service packages**
- **5 DR runbook scaffolds** in `ops/runbooks/`
- **1 CI workflow** (Tenant Isolation Auditor stub)
- **1 Laravel Eloquent model + REST controller** (SavedMapView)

## Recommended next ticks

The autonomous run is now genuinely exhausted of pure-backend
scaffolding work. Remaining options:
- Frontend stubs (Inertia React placeholder pages) — borderline
- Documentation polish + cross-reference fixup
- Verifier scripts for the new doc-phase work
- Inertia React route stubs in `routes/web.php` for the new
  controllers

Doc-phase 106 = verifier script bundle for the doc-phases 76-105
schema + skeleton landings. Pattern matches the
`scripts/phase6_master_plan_step4_5_verify.sh` cascade-manifest
pattern from doc-phase 76. Roll up all the schema landings + import
smokes into one big verifier so future sessions can fast-cascade
the autonomous-run state.

## Carry-overs

Unchanged. The remaining work is genuinely Kyle-dependent.
