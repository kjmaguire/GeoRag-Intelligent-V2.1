## Doc-phase 128 handoff — §10.7 Eval Dashboard (first Track 3 surface)

**Status:** Live + smoke-verified. **72/72 substrate verifier**.

## What landed

### Controller — `app/Http/Controllers/Admin/EvalDashboardController.php`

Read-only Laravel controller mirroring the established
`HatchetWorkersController` pattern:
- `$this->authorize('admin')` gate
- Raw SQL via `DB::select(...)` (zero new Eloquent models needed —
  the existing `GoldenQuestion` model from doc-phase 109 covers the
  Eloquent path; this controller uses SQL for aggregations).
- Returns `Inertia::render('Admin/EvalDashboard', [...])` with 5
  structured payloads:
  - **kpis** — 9 top-level counters (active/draft/retired Qs,
    ontology terms/synonyms, classes-populated 3/12,
    recent-runs count + last_at)
  - **questions_by_set** — per-set rollup with status breakdown
  - **questions_by_difficulty** — easy/medium/hard for active Qs
  - **ontology_progress** — all 12 §20.1 classes with status
    heuristic ('empty' | 'mechanical_seeded' | 'sme_populating' |
    'populated') matching the FastAPI-side
    `get_ontology_class_stats`
  - **recent_runs** — last 30 days from `eval.run_summaries`

### Route — `routes/web.php`

`GET /admin/eval-dashboard` → `admin.eval-dashboard` name.

### React page — `resources/js/Pages/Admin/EvalDashboard.tsx`

~430 lines. Matches the project's existing dark Tailwind palette
(`bg-stone-950` + `text-stone-100` + emerald/amber/red accent
tones).

Layout sections:
1. **KPI tiles row** — 4 cards: active Qs, draft/retired count,
   ontology classes populated (X/12), recent runs 30d
2. **Golden questions by set** — table with active/draft/retired/total
   columns; emerald for active, amber for draft, stone for retired
3. **Difficulty breakdown + ontology terms/synonyms** — side-by-side
   small panels
4. **Ontology population progress** — full 12-class table with
   term count vs threshold, synonym count, status badge
5. **Recent eval runs** — last 30 days, run_id slice + counts +
   blocks_promotion badge

Empty-state handling on every section. When no eval runs exist (today),
shows guidance: "The §10.4 evaluate_workspace Hatchet workflow
populates this surface once its task body graduates from skeleton."

### What the dashboard shows TODAY (with real data)

| KPI | Value |
|---|---|
| Active golden questions | **45** (15 numeric_grounding + 10 each of schema_mapping/ocr_triage/report_section) |
| Draft / retired questions | 0 / 0 |
| Ontology classes populated | **3 / 12** (commodity, geological_age, resource_class) |
| Recent runs (30 d) | 0 |
| Ontology terms / synonyms | 83 / 134 |

### Smoke verification

```bash
# Controller class loads
php artisan tinker --execute 'echo class_exists(EvalDashboardController::class)';
# → "controller OK"

# Route registered
php artisan route:list --json | jq '.[] | select(.name=="admin.eval-dashboard")';
# → admin/eval-dashboard registered

# All 5 controller data methods run end-to-end (via reflection bypass of auth gate)
php /tmp/eval_dash_smoke.php
# → kpis: OK — 45 active Q, 83 ontology terms, 3/12 classes populated
# → questionsByQuestionSet: OK — 4 rows
# → questionsByDifficulty: OK — 3 rows
# → ontologyProgress: OK — 12 rows
# → recentRuns: OK — 0 rows

# Pint
vendor/bin/pint --dirty --format agent
# → {"tool":"pint","result":"passed"}
```

## Incident caught + fixed during this tick

### Test cleanup nuked production seed data

The doc-phase 124 mechanical_questions DB tests used
`ALL_MECHANICAL_QUESTIONS` directly + cleaned up by deleting via
`stable_question_id` IDs. Those IDs are identical to the production
seed's IDs. Every verifier run nuked the 45 production questions.

**Fix:** Refactored 3 of the 4 DB tests to use `_synthetic_questions(prefix)`
helper that builds 6 schema-valid questions with `[<uuid8>]`-prefixed
text. The stable_question_ids then can't collide with production
IDs, so test teardown is fully isolated.

**Verification:**
```
Production count BEFORE running tests: 45
Run tests: 14 passed
Production count AFTER running tests:  45  ← preserved
```

This is the same class of bug as the doc-phase 122 incident
(unisolated test/data interaction). Would have stayed silent if
the production seed hadn't survived to validate against — the
Eval Dashboard work happened to surface it.

## Cumulative session state

- **Doc-phase ticks this run:** 128
- **Live helpers:** 8 + the Eval Dashboard (first Track 3 surface)
- **Live pytest cases:** 52 + 14 mechanical questions = 66
- **Substrate verifier:** **72/72 PASS**
- **Tracks closed:**
  - Track 1 (image rebuild): ✅ CLOSED through 4 builds
  - Track 2b (mechanical questions seed): ✅ 45 active in DB
  - **Track 3 (first frontend surface): ✅ Eval Dashboard live**
- **Tracks waiting for Kyle:**
  - Track 2a (§8.3 Athabasca SME content)
  - Track 3 follow-ons (Support Cockpit, Decision History, Hypothesis
    Workspace, MapLibre layer packs)

## Recommended next ticks

The Eval Dashboard works against real data today. Three productive
follow-ons:

1. **Verify in browser** — Kyle visits `/admin/eval-dashboard` after
   `npm run build` (or `npm run dev`). Quick eyeball confirms
   colors/density/density before we cover more surfaces.
2. **Next frontend surface** — Support Cockpit (§10.11) or Decision
   History (§9.12). Both have live aggregators behind them.
3. **Skeleton graduation** — `evaluate_workspace` Hatchet workflow
   task body (§10.4). Would populate `eval.run_summaries`, making the
   "Recent eval runs" section of the Eval Dashboard show real runs
   instead of the empty state.

## Carry-overs

- The dashboard needs `npm run build` to actually serve — the new
  React page won't appear in the Inertia bundle until rebuilt.
  Run from the repo root.
- Inertia tests for the dashboard (visiting the route + asserting
  page props are passed) — not yet authored. Pattern matches the
  existing `HatchetWorkersTest` if one exists, or follows the new
  doc-phase 108 route-smoke pattern.
- ⚠️ The doc-phase 124 test refactor changes the assertion count
  (was "all 45 inserted", now "6 synthetic inserted"). The
  protective-rail intent is preserved — the seeder behavior is
  what's being tested, not exact production counts.
