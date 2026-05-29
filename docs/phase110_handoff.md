## Doc-phase 110 handoff — Targeting + Hypotheses + DecisionRecords Eloquent models

**Status:** Complete. 10 models + 3 factories. **13/13 smoke tests pass.** Pint clean.

## What landed

### Eloquent models (10 new files)

**Targeting namespace** (`app/Models/Targeting/`):
- `TargetRecommendation` — final ranked recommendation per
  `targeting.target_recommendations` (with `reviewDecisions()` +
  `outcomes()` HasMany)
- `TargetReviewDecision` — R5 sign-off with QP credential metadata
  per `targeting.target_review_decisions`
- `TargetOutcome` — post-drilling outcome (Phase 12 training input)
  per `targeting.target_outcomes`

**Silver namespace** (`app/Models/Silver/`):
- `Hypothesis` — competing hypothesis per `silver.hypotheses`
  (with `evidenceLinks()` HasMany)
- `HypothesisEvidenceLink` — supporting/contradicting/missing/
  recommended_test rows
- `DecisionRecord` — eight §21.3 decision types per
  `silver.decision_records` (with `evidenceLinks()`, `options()`,
  `outcomes()`, `lessonsLearned()` HasMany)
- `DecisionEvidenceLink` — supporting/contradicting/context
- `DecisionOption` — options considered (with `was_chosen` flag)
- `DecisionOutcome` — post-decision outcome tracking
- `DecisionLessonLearned` — retrospective captures (written by
  `field_outcome_learning` workflow)

Each model:
- HasUuids trait + non-incrementing string PK
- `$timestamps = false` (schema uses domain-specific timestamps)
- JSONB casts to `'array'`; numeric casts to `'float'`; boolean
  casts where applicable
- BelongsTo + HasMany relations spanning the targeting + silver +
  User cross-references

### Factories (3 new files)

- `Database\Factories\Targeting\TargetRecommendationFactory` —
  default state + `topRanked()` (rank=1)
- `Database\Factories\Silver\HypothesisFactory` — default state
  (ai_suggested), `accepted()`, `rejected()`
- `Database\Factories\Silver\DecisionRecordFactory` — default
  (random decision_type), `targetRecommendation()`,
  `reportSignoff()`

Sibling tables (TargetReviewDecision, TargetOutcome, HypothesisEvidenceLink,
DecisionEvidenceLink/Option/Outcome/LessonLearned) intentionally
have no factories — they're created via the parent factory's
`hasMany` chain when tests need them; standalone factories add
maintenance for little use.

### Smoke test — `tests/Feature/Api/V1/Doc110ModelLoadTest.php`

13 individual class_exists assertions covering all 10 models +
3 factories. Pure class-loading test (no DB I/O), so it catches
namespace + autoload drift without needing migrations to apply.

```
docker exec georag-laravel-octane php artisan test --compact \
  tests/Feature/Api/V1/Doc110ModelLoadTest.php
# Tests: 13 passed (13 assertions)
# Duration: 0.33s

vendor/bin/pint --dirty --format agent
# {"tool":"pint","result":"passed"}
```

## Cumulative model layer (post doc-phase 110)

Eloquent models now exist for the following autonomous-run schemas:

| Schema | Model | Factory | Smoke test |
|---|---|---|---|
| silver.saved_map_views (doc-phase 76) | SavedMapView | ✅ | ✅ (doc-phase 108) |
| eval.golden_questions (doc-phase 97) | GoldenQuestion | ✅ | (covered by Doc110 pattern) |
| ops.support_tickets (doc-phase 97) | SupportTicket + 2 related | ✅ | — |
| targeting.target_recommendations (doc-phase 85) | TargetRecommendation + 2 related | ✅ | ✅ |
| silver.hypotheses (doc-phase 91) | Hypothesis + EvidenceLink | ✅ | ✅ |
| silver.decision_records (doc-phase 92) | DecisionRecord + 4 related | ✅ | ✅ |

**6 schemas, 14 models, 5 factories, 16 smoke assertions total** in
the Laravel model layer for this autonomous-run substrate.

Not yet modeled (lowest-leverage — no obvious near-term consumer):
- targeting.target_models / target_model_versions (read-only ML
  config; FastAPI side handles directly)
- targeting.target_candidate_zones (PostGIS-heavy; raw SQL more
  natural)
- targeting.target_scores / score_factors / uncertainties (ditto)
- targeting.target_backtests (Phase 12 metrics; FastAPI-side)
- silver.geological_ontology_terms / synonyms (FastAPI lookup
  service)
- silver.source_trust_scores / features (FastAPI fusion layer)
- eval.run_results / run_summaries (Hatchet workflow writes; UI
  reads via aggregation queries)
- ops.support_ticket_traces / replay_runs (modelled but no factory)

These can spawn Eloquent models when a Laravel-side consumer emerges.

## Cumulative session tally (doc-phases 74-110 = 37 ticks)

The Laravel model layer for the autonomous-run substrate is now
substantively complete. Frontend pass can build against any of these
models directly.

## Recommended next ticks

Genuinely no more autonomous-safe substrate work remains in the
Laravel model layer at high-priority. Remaining options:
- Eloquent models for the lower-priority tables (above list) — but
  premature without a known consumer
- More Inertia React route stubs in `routes/web.php` — borderline
- Documentation polish
- Update the rollup substrate verifier to include the new model
  classes

Doc-phase 111 (if continuing) = extend `autonomous_run_substrate_verify.sh`
to assert the 14 model classes + 5 factories load. Marginal value
since the dedicated Doc110ModelLoadTest already covers it, but
useful for the single-command rollup story.

## Carry-overs

Unchanged. Substrate verifier still 36/36. Adding the Laravel model
checks would bump it to 50/50.
