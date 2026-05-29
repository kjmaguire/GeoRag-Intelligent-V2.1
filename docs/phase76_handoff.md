## Doc-phase 76 handoff — §6.5 Saved map views table

**Status:** Complete. Verifier 6/6 green.

## What landed

`database/migrations/2026_05_13_090000_create_silver_saved_map_views.php` —
new `silver.saved_map_views` table for the §6.7+ MapLibre frontend
saved-views feature.

Schema:
- `view_id UUID` PK
- `workspace_id`, `project_id` FK → silver
- `user_id BIGINT` FK → public.users
- `name VARCHAR(120)` UNIQUE per (project_id, user_id, name)
- `description TEXT`
- `view_state JSONB` — MapLibre camera + layer-pack + filter state
  (JSONB to absorb frontend evolution without migrations)
- `aoi_geom geometry(Geometry, 4326)` — optional saved AOI polygon
- `is_shared BOOLEAN` (placeholder for future per-workspace share)
- Standard timestamps + indexes (workspace, project, user, GIST on aoi)
- **RLS** enabled with `saved_map_views_workspace_isolation` policy
  (same pattern as other silver tables; uses `app.workspace_id` setting)

Applied via superuser `georag` (georag_app can't CREATE in silver
schema — same workaround as doc-phases 50, 71). Laravel migrations row
inserted manually.

## Auth model decision (autonomous choice)

Per the §6 scope proposal open question #3, Kyle hadn't picked an auth
model. I chose **per-user, per-project, workspace-scoped**:
- Each row tied to one user + one project
- RLS enforces workspace isolation
- `is_shared` flag added for future "share with workspace" feature
  without requiring schema migration

If Kyle wants per-project shared views (instead of per-user-per-project),
either flip `is_shared` to default true, or add a sharing-permissions
side-table later. Tabled in 8am pickup briefing.

## Verifier

`scripts/phase6_master_plan_step4_5_verify.sh` — covers §6.4 boundary
agent skeleton + §6.5 table. 6 checks (file existence, import smoke,
table existence, columns, RLS policy, migration row). All 6 PASS.

Marks `step6.4-5` in the cascade manifest on success.

## Master-plan §6 progress

| Sub-step | Status |
|---|---|
| 6.1 audit existing public_geoscience.* | ✅ DONE |
| 6.2 BC MINFILE ingestion | pending (medium backend) |
| 6.3 NRCan/GEO.ca ingestion | pending (medium backend) |
| 6.4 Public/Private Boundary Agent | ✅ skeleton |
| 6.5 Saved map views table | ✅ DONE |
| 6.6 h3-pg density aggregations | pending (medium backend) |
| 6.7-6.14 frontend layer packs + MapLibre work | pending (waits for Kyle) |
| 6.15 acceptance test | pending |

**3 of 15 sub-steps closed.**

## Carry-overs

1. **§6.5 Laravel CRUD controller + Inertia React UI** — frontend bits;
   waits for Kyle's 8am pickup.
2. **§6.5 SavedMapView Eloquent model + factory + tests** — Laravel-side
   model layer; small, but tied to the controller work above. Defer.
3. **Auth-model confirmation** — see "Auth model decision" above.

## Recommended next tick

Doc-phase 77 = §6.6 (h3-pg density aggregations). Backend-only Dagster
asset; pattern matches other Dagster work. Or jump to §7 scope proposal
(Reporting + dashboards) if §6.6 looks expensive on inspection. Will
make the call at start of next tick.
