## Doc-phase 107 handoff — SavedMapView route registration + factory

**Status:** Complete. 5 routes registered + factory loads + Pint passes.

## What landed

### Route registration

`routes/api.php` — added under the `auth:sanctum` group alongside
projects + collars:

```php
Route::apiResource('projects.saved-map-views', SavedMapViewController::class)
    ->scoped()
    ->parameters(['saved-map-views' => 'view']);
```

`scoped()` enforces project-scoping (the view must belong to the
project in the URL). `parameters` shortens `{saved_map_view}` to
`{view}` in route URIs.

`route:list --json` confirms **5 named routes registered**:
- `projects.saved-map-views.index` — GET
- `projects.saved-map-views.store` — POST
- `projects.saved-map-views.show` — GET
- `projects.saved-map-views.update` — PUT/PATCH
- `projects.saved-map-views.destroy` — DELETE

### Factory

`database/factories/SavedMapViewFactory.php` — seeds feature tests
with realistic data:
- UUID `view_id` + `workspace_id`
- `project_id` via `Project::factory()` chain
- `user_id` via `User::factory()` chain
- `name` unique 3-word string
- `view_state` realistic MapLibre payload: camera (longitude in
  Saskatchewan/BC range, latitude, zoom, bearing, pitch) +
  `active_layer_pack` (private_project | public_geo | qa | target)
  + empty `filters` array
- `aoi_geom = null` (optional column)
- `is_shared = false` default
- `shared()` state for `is_shared=true`

### Model update

`app/Models/SavedMapView.php`:
- Added `use HasFactory` trait + PHPDoc `@use HasFactory<SavedMapViewFactory>`
- Now matches the canonical Laravel-Boost pattern from
  `app/Models/Project.php`.

### Verification

```
docker exec georag-laravel-octane php artisan route:list --json
# 5 saved-map routes registered

docker exec georag-laravel-octane php artisan tinker --execute \
  'echo class_exists(\Database\Factories\SavedMapViewFactory::class) ? "OK" : "missing";'
# OK

vendor/bin/pint --dirty --format agent
# {"tool":"pint","result":"passed"}
```

## Master-plan §6.5 status — backend complete

| Layer | Status |
|---|---|
| Database schema + RLS (doc-phase 76) | ✅ |
| Eloquent model (doc-phase 105) | ✅ |
| REST controller skeleton (doc-phase 105) | ✅ |
| Route registration (this tick) | ✅ |
| Factory (this tick) | ✅ |
| Inertia React frontend (§6.7+) | pending (Kyle) |
| Feature tests (CRUD round-trip) | pending — needs controller bodies first |

Backend is **fully scaffolded**. The 5 routes resolve to controller
methods that throw `LogicException` until the controller bodies
graduate from skeleton.

## Recommended next ticks

Genuinely autonomous-safe ground is now exhausted. Remaining
options are all increasingly borderline:
- Add a phpunit feature test stub that verifies routes 404/422 cleanly
  on missing project/auth (skeleton-friendly)
- Inertia React route stubs in `routes/web.php` for the future
  cockpit + map UIs
- Documentation polish + cross-reference fixup

If continuing, doc-phase 108 = a smoke feature test that exercises
the 5 saved-map-view routes against the `LogicException` skeleton
bodies (asserts the routes are wired but bodies are skeleton-marked,
not silently broken).

## Carry-overs

Same as prior. The autonomous-run substrate is genuinely complete
for backend; everything else needs Kyle.
