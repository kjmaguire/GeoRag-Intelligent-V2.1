## Doc-phase 108 handoff — SavedMapView route smoke test

**Status:** Complete. 3 tests / 7 assertions pass. Pint clean.

## What landed

`tests/Feature/Api/V1/SavedMapViewRoutesTest.php` — 3 smoke tests:

1. **`test_index_requires_authentication`** — `GET /api/v1/projects/{p}/saved-map-views`
   without auth returns 401.
2. **`test_store_requires_authentication`** — `POST` without auth returns 401.
3. **`test_all_five_routes_are_registered`** — asserts all 5 route
   names (index/store/show/update/destroy) appear in the route list.

### Why smoke + not full CRUD round-trip

The doc-phase 105 controller bodies throw `LogicException`. Full
CRUD tests would all fail with 500 errors right now. The smoke tests
verify the route + middleware path WITHOUT touching the skeleton
bodies — they assert the wiring is correct so when the controller
graduates, the upgrade is a transparent extension rather than a
hunt for broken route registration.

Smoke-test verification:

    docker exec georag-laravel-octane php artisan test --compact \
      tests/Feature/Api/V1/SavedMapViewRoutesTest.php
    # Tests: 3 passed (7 assertions)
    # Duration: 0.36s

## Cumulative session-continuation tally (doc-phases 74-108 = 35 ticks)

The autonomous run continuation has covered all backend skeleton
work that could land without Kyle SME, image rebuild, or frontend.
Doc-phase 106's rollup verifier passes 36/36 checks; doc-phase 108
adds 3 more route smoke assertions on top.

Genuinely-clean stopping point. The remaining work is Kyle's:
1. **Image rebuild bundle** — unlocks ~30 skeleton graduations
2. **Kyle SME content** — ontology, golden questions, deposit-model
   attributes, scoring weights, DR runbook details
3. **Frontend pass** — MapLibre, dashboards, sign-off UIs, lineage UI

## Recommended next ticks

The autonomous run is now exhaustively done. If the user says
"continue" again, options:
- Doc polish + cross-reference fixup (low-leverage, mostly
  housekeeping)
- Wait for Kyle on the three tracks above
- Spawn off-session tasks for any of the open carry-overs (which
  the session-spawn tool can do)

Doc-phase 109+ = if the user continues, switch to housekeeping
mode: cleanup imports, run pint across the whole project, run the
whole test suite, regenerate the agent registry, etc. Each is small
+ value-add but not net-new substrate.

## Carry-overs

Same as prior. Substrate verifier still 36/36 green. Route smoke
test adds 3/7 to the verified-state tally. Master plan is fully
scoped + autonomously-safe substrate is complete.
