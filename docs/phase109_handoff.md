## Doc-phase 109 handoff — Eloquent models for eval + ops schemas

**Status:** Complete. 4 models + 2 factories. All load + Pint clean.

## What landed

### Eloquent models

| File | Schema | Purpose |
|---|---|---|
| `app/Models/Eval/GoldenQuestion.php` | `eval.golden_questions` | Per-question golden eval definition (§10.1) |
| `app/Models/Ops/SupportTicket.php` | `ops.support_tickets` | Customer-reported issue (§10.8) |
| `app/Models/Ops/SupportTicketTrace.php` | `ops.support_ticket_traces` | Many-to-many tickets ↔ trace_ids (§10.8) |
| `app/Models/Ops/SupportReplayRun.php` | `ops.support_replay_runs` | Workflow replay attempts (§10.10) |

Each model:
- `HasUuids` trait + non-incrementing string PK
- `$table` references the schema-qualified table name
- `$timestamps = false` (these schemas use domain-specific timestamps
  like `reported_at`, `authored_at`, `initiated_at` rather than
  Laravel's `created_at`/`updated_at`)
- JSONB columns cast to `'array'` (context_setup, expected_*)
- Boolean columns cast to `'boolean'`
- BelongsTo / HasMany relations to User + cross-table:
  - GoldenQuestion → author + reviewer (User)
  - SupportTicket → reporter, assignee (User) + traces, replayRuns
  - SupportTicketTrace → ticket, addedBy
  - SupportReplayRun → ticket, initiatedBy

### Factories

`database/factories/Eval/GoldenQuestionFactory.php`:
- Default: draft, `schema_mapping` set, mechanical question
- States: `active()`, `retired()`, `publicPrivateBoundary()` (with
  §2.9 must_contain/must_not_contain phrases),
  `targetRecommendation()` (with §18.1 "drill here" language
  enforcement), `refusalExpected(reason)` for refusal_correctness set.

`database/factories/Ops/SupportTicketFactory.php`:
- Default: open ticket, random channel + category + severity
- States: `assigned()` (with status flip to investigating),
  `critical()` (severity + phone channel), `resolved()` (with
  resolution_summary + resolved_at + assignee).

### Smoke verification

```
docker exec georag-laravel-octane php artisan tinker --execute '...'
# All 4 model + 2 factory classes load.

vendor/bin/pint --dirty --format agent
# {"tool":"pint","result":"passed"}
```

## Why these 4 first

Out of the 26 new tables across this run, these 4 are the most
likely to be hit by the next layer of work:
- `eval.golden_questions` is the data source for the §10.7 Eval
  Dashboard (Kyle frontend pass) AND for the `evaluate_workspace`
  Hatchet workflow body graduation.
- `ops.support_tickets` + traces + replay_runs are the data source
  for the §10.11 Customer Support Cockpit UI.

Other tables (targeting.*, hypotheses, decision_records, source_trust)
are reached via FastAPI service packages first — they don't need
Eloquent models yet. Models for those can land when a Laravel-side
consumer emerges.

## Master-plan §10 status update

§10 backend is now MORE complete than the doc-phase 99 tally:
- Eval schema: ✅ + Eloquent model
- Eval factories ready for feature tests
- Ops schema: ✅ + Eloquent models for all 3 tables
- Support agents: ✅ skeletons
- Workflows: ✅ skeletons + registered

The §10 frontend pass (10.3 / 10.7 / 10.11) is now genuinely
unblocked on the backend side — Inertia React pages just need to
hit the model layer + paginate.

## Cumulative session-continuation tally — doc-phases 74-109 = 36 ticks

The backend autonomous-safe substrate now also has Laravel model
coverage for the highest-leverage tables.

## Recommended next ticks

Doc-phase 110+ = more model graduations as needed:
- `targeting.target_recommendations` model (for Target Dashboard)
- `silver.hypotheses` model (for Interpretation Workspace)
- `silver.decision_records` model (for decision history UI)

Each follows the pattern from doc-phase 105 + this tick. Or pivot
back to the run substrate verifier — extend it to assert the new
models load.

## Carry-overs

Same as prior. Plus:
- The 4 new models work but skip Laravel timestamps; consumers need
  to use the schema-specific timestamp fields explicitly.
- Eval + ops timestamps are TIMESTAMPTZ; cast as `datetime`.
