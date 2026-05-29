## Doc-phase 85 handoff — §8.1 `targeting.*` schema migrations

**Status:** Complete. Schema + 10 tables + 7 RLS policies live. Verified.

## What landed

`database/migrations/2026_05_13_100000_create_targeting_schema.php` —
single migration file creating the entire §18.6 schema:

| Table | Purpose |
|---|---|
| `target_models` | deposit model templates (10 types ship in §8.2) |
| `target_model_versions` | A/B versioning (weighted/xgboost/ensemble) |
| `target_candidate_zones` | generated polygons (PostGIS POLYGON 4326) |
| `target_scores` | per-zone aggregate score + uncertainty |
| `target_score_factors` | per-zone, per-factor SHAP-equivalent breakdown |
| `target_uncertainties` | per-factor + aggregate uncertainty |
| `target_recommendations` | final ranked recommendations |
| `target_review_decisions` | geologist R5 sign-off with QP credentials |
| `target_outcomes` | post-drilling outcomes (Phase 12 backtest input) |
| `target_backtests` | per-model performance metrics (Phase 12) |

### Constraints + indexes

- 6 indexes (workspace+project lookups, run_id, GIST on zone_geom,
  recommendations by run+rank, decisions+outcomes by recommendation_id).
- CHECK constraints on enums:
  - `scoring_kind ∈ {weighted, xgboost, ensemble}`
  - `method ∈ {bayesian, bootstrap, analytical, heuristic}`
  - `decision ∈ {accepted, modified, rejected, signed_off}`
  - `hit_or_miss ∈ {hit, miss, partial, pending, unresolvable}`
  - `slug ~ '^[a-z][a-z0-9_]*$'`
- ON DELETE cascades from parents (workspace → zone → scores →
  factors); RESTRICT on `model_version_id` on scores +
  recommendations (versions are immutable once scoring runs reference
  them).

### RLS — 7 policies

- **Direct workspace_id columns** (4 tables): `target_candidate_zones`,
  `target_recommendations`, `target_review_decisions`,
  `target_outcomes`. Each gets a standard workspace-isolation policy.
- **target_scores** gets a direct workspace_id column + policy
  for fast index-friendly RLS.
- **target_score_factors + target_uncertainties** scope via EXISTS
  subquery on `target_scores` (avoids duplicating workspace_id on a
  hot per-factor row).
- **target_models + target_model_versions + target_backtests** are
  workspace-INDEPENDENT (model definitions are global; backtests
  optionally scope by workspace_id NULL for global metrics). No RLS
  enabled on these three.

### Apply pattern

Same as §6.5 / §5.3-5.5 (doc-phases 50, 71, 76):
1. `php artisan migrate --pretend` extracted the SQL.
2. Stripped Laravel formatting via `grep '⇂' | sed`.
3. Applied as superuser `georag` (georag_app can't CREATE schemas).
4. Manually INSERT-ed migrations row.

Verification:
```
SELECT count(*) FROM pg_tables WHERE schemaname='targeting'; -- 10
SELECT count(*) FROM pg_policies WHERE schemaname='targeting'; -- 7
```

## Master-plan §8 progress

| Sub-step | Status |
|---|---|
| 8.0 scope proposal | ✅ DONE (doc-phase 84) |
| 8.1 `targeting.*` schema migrations | ✅ DONE |
| 8.2 Deposit model loader (10 templates) | pending |
| 8.3 Athabasca uranium SME content | pending (Kyle) |
| 8.4 Target Recommendation Graph state + nodes | pending |
| 8.5 11 target agents skeletons | pending |
| 8.6 score_targets Hatchet workflow | pending |
| 8.7 Weighted scoring formula | pending (Kyle weights) |
| 8.8 SHAP-equivalent score factor writer | pending |
| 8.9 Sign-off ceremony | pending (Kyle decisions) |
| 8.10 Target Pack map layer | pending (Kyle) |
| 8.11 Activepieces target workflows | pending (Activepieces gate) |
| 8.12 Recommendation Explainer Agent | pending |
| 8.13 Acceptance test | pending |

**1 of 14 §8 sub-steps closed.**

## Recommended next tick

Doc-phase 86 = §8.4 Target Recommendation Graph state model + node
stubs. Same pattern as §7.1 (doc-phase 80). Pydantic
`TargetRecommendationState` + 11 node stubs in
`app/services/target_recommendation/`.

Alternative: doc-phase 86 = §8.2 deposit model loader skeleton + 10
empty template stubs. Lighter tick.

Both reasonable; I'll pick at start of next tick. Graph state is
higher-leverage (locks contract for 11 agents).

## Carry-overs

1. **Image rebuild** still required for langgraph (§7 + §8 graph wiring).
2. **Kyle SME data** for §8.3 Athabasca uranium content + §8.7 scoring
   weights + §8.9 sign-off mechanism.
3. **Activepieces install status** — gates §8.11 + §7.11.
4. **`targeting.*` not under Laravel's migration ownership for future
   alter statements** — the schema was applied via superuser; future
   ALTER TABLE statements for this schema follow the same dance.
