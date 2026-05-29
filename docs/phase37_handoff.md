# Phase 37 Handoff — R-P21-CACHE-TELEMETRY-DASHBOARD backend slice

**Document version:** 1.0
**Status:** Phase 37 complete. R-P21 backend slice landed; frontend (R-P11-B) consumes this in a future user-driven phase.
**Predecessors:** `docs/phase36_handoff.md`, `docs/phase30_handoff.md`
(which added the `cache_skipped_reason` column this endpoint reads).

---

## 1. What Phase 37 delivered

The backend half of R-P21-CACHE-TELEMETRY-DASHBOARD — a Laravel admin
JSON endpoint that aggregates `silver.answer_runs` cache columns over
rolling time windows. The frontend dashboard (R-P11-B carry-over) will
consume this endpoint; this slice is independent of and unblocks that
future work.

| Step | Output | Verifier |
|------|--------|----------|
| 1 | `app/Http/Controllers/Admin/CacheTelemetryController.php` — `skipReasons()` method returning 4-key JSON (`window_hours`, `totals`, `skipped_reasons`, `last_hour`). Admin-gate authorized; honours `?window_hours=N` query param clamped to [1, 168]. | `scripts/phase37_step1_verify.sh` (8/8) |
| 2 | `routes/web.php` — `GET /admin/cache-telemetry/skip-reasons.json` registered under the `auth:sanctum` group, mirrors the pattern of other Admin/* routes. | (same) |
| 3 | `tests/Feature/Admin/CacheTelemetryTest.php` — 5 test methods covering guest-redirect, non-admin-forbidden, JSON shape, inserted-rows reflection, and `window_hours` clamping. Gated on `RequiresPostgres` (skipped under SQLite test driver; runs against real PG in the release-rehearsal CI job). | (same) |
| 4 | This handoff + master sweep | — |

---

## 2. Endpoint contract

```
GET /admin/cache-telemetry/skip-reasons.json
GET /admin/cache-telemetry/skip-reasons.json?window_hours=6
```

Response (JSON):

```json
{
  "window_hours": 24,
  "totals": {
    "hits": 97,
    "misses": 418,
    "total": 515,
    "hit_rate": 0.1883
  },
  "skipped_reasons": {
    "zero_candidates": 12,
    "partial_failures": 0,
    "schema_validation_failed": 0,
    "downhole_bypass_legacy": 0,
    "(none)": 411
  },
  "last_hour": {
    "hits": 4,
    "misses": 23,
    "total": 27,
    "hit_rate": 0.1481
  }
}
```

All 5 documented skip-reason keys are always present (zero-valued
when absent from the period) so the frontend can render zero-series
without conditional logic. `"(none)"` aggregates rows where the
cache write succeeded (`cache_skipped_reason IS NULL`).

Auth: `admin` Gate enforced via `$this->authorize('admin')` —
guests get 302 → `/login`, non-admin users get 403.

---

## 3. R-P21-CACHE-TELEMETRY-DASHBOARD progress

| Slice | Phase | Scope | Status |
|-------|------:|-------|--------|
| 1 — Backend endpoint | **37** | JSON API + controller + feature test | ✅ done |
| 2 — Frontend dashboard | TBD | Inertia page consuming the endpoint; chart components | pending (paired with R-P11-B) |

The frontend slice is medium-sized and deserves its own user-driven
session — it needs Inertia + React + chart library decisions, and
slots cleanly alongside R-P11-B (the Search/Query frontend page).
This phase's backend slice is self-contained and lands first so the
data shape is locked.

---

## 4. Cold-run pass count

Phase 37 touches only Laravel code, not the FastAPI agent or the
golden-query suite. Cold-run pass count is unchanged from Phase 36's
typical 30-31/31.

---

## 5. Carry-overs for Phase 38+

| ID | Item | Priority |
|----|------|----------|
| R-P11-B + R-P21 frontend slice | Inertia page for cache telemetry + (separately) the Search/Query page | Medium — user-driven; needs frontend session |
| (Pre-existing) AgentConfig broken-import in routes/web.php | `php artisan route:list` errors on missing `App\Http\Controllers\Admin\AgentConfig\TimeoutsController` (and 3 siblings). Routes referenced at lines 204-215. The classes don't exist on disk. Not caused by Phase 37; route-list has presumably been broken since whenever those routes were added. Worth a cleanup. | Low — `artisan route:list` cosmetic break |

---

## 6. Files of record

```
app/Http/Controllers/Admin/CacheTelemetryController.php   (Step 1 — new)
routes/web.php                                             (Step 1 — route added; Pint also reformatted imports)
tests/Feature/Admin/CacheTelemetryTest.php                 (Step 1 — new)
docs/phase37_handoff.md                                     (this file)
scripts/phase37_master_sweep.sh                            (Step 2)
scripts/phase37_step1_verify.sh                            (Step 1)
```

End of Phase 37 handoff.
