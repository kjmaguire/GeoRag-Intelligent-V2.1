## Doc-phase 172 handoff — RLS policy idempotency + Inertia testing-path fix — Track3 admin tests unblocked

**Status:** Live + 110/110 substrate verifier + Track3 eval-dashboard 4/4 pass under pgsql phpunit + FastAPI 56/56 regression preserved.

## What landed

Two pre-existing test-infrastructure carry-overs cleared:

### Part 1 — CREATE POLICY idempotency (5 migrations)

`migrate:fresh` (the engine behind Laravel's `RefreshDatabase` trait)
drops tables in the default `public` schema but leaves non-public
schemas alone. So `silver`, `targeting`, `audit`, `eval` schemas +
their tables + their RLS policies all persist between test sessions.
When the next session re-runs migrations, `CREATE POLICY` errors
with `policy "foo" for table "bar" already exists` because Postgres
has no `CREATE POLICY IF NOT EXISTS` syntax.

Fix pattern (already established in `2026_04_17_120200`): prepend
each `CREATE POLICY` with a `DROP POLICY IF EXISTS` of the same name.

Migrations patched (5 of 5 that needed it):

| Migration | Policies fixed |
|---|---|
| `2026_05_13_090000_create_silver_saved_map_views.php` | 1 |
| `2026_05_13_100000_create_targeting_schema.php` | 7 (4 in foreach + 3 single) |
| `2026_05_13_120000_create_silver_hypotheses.php` | 2 |
| `2026_05_13_130000_create_decision_intelligence_schema.php` | 5 (1 single + 4 in foreach) |
| `2026_05_13_150000_create_source_trust_schema.php` | 2 |

Pattern, applied uniformly:

```php
DB::statement('DROP POLICY IF EXISTS foo_workspace_isolation ON silver.foo;');
DB::statement(<<<'SQL'
    CREATE POLICY foo_workspace_isolation
        ON silver.foo
        USING (workspace_id::text = current_setting('app.workspace_id', true))
        WITH CHECK (workspace_id::text = current_setting('app.workspace_id', true));
SQL);
```

### Part 2 — Inertia testing page-path config override

The `inertiajs/inertia-laravel` package default points
`pages.paths` at `resource_path('js/pages')` — lowercase. GeoRAG's
React pages live under `resources/js/Pages` — capital P, per the
TypeScript-React convention.

In testing mode, `inertia.testing.ensure_pages_exist = true`. The
package's `AssertableInertia::component()` checks for the component
on disk relative to `pages.paths`. With the casing mismatch, every
admin-dashboard render assertion failed with:

```
Inertia page component file [Admin/EvalDashboard] does not exist.
```

Even though the file IS on disk.

Fix: created `config/inertia.php` overriding the package default
with the correct capital-P path. Runtime impact zero (Inertia
renderer doesn't use the page-paths config at runtime — Vite handles
resolution). Testing mode gets the right path.

```php
'testing' => [
    'ensure_pages_exist' => true,
    'page_paths' => [resource_path('js/Pages')],
    'page_extensions' => ['js', 'jsx', 'ts', 'tsx'],
],
```

## Live verification

```bash
# Track3DashboardsTest eval-dashboard cases under pgsql phpunit
docker exec georag-laravel-octane php artisan test --compact \
    -c phpunit.pgsql.xml \
    tests/Feature/Admin/Track3DashboardsTest.php \
    --filter=eval_dashboard
# → 4 passed in 4.03s
# (was 4 failed pre-doc-phase-172 — policy collision blocked migration)

# FastAPI eval regression
docker exec georag-fastapi python -m pytest \
    tests/test_eval_validators.py tests/test_real_rag_evaluator.py \
    tests/test_evaluate_workspace_workflow.py tests/test_eval_real_rag_nightly_workflow.py
# → 56 passed in 44.46s

# Substrate verifier
bash scripts/autonomous_run_substrate_verify.sh
# → 110/110 checks passed (was 108 — +2 doc-phase-172 checks)
```

## Substrate verifier — 2 new checks

| Check | Verifies |
|---|---|
| `migrations:policy-drop-first` | All 5 workspace-isolation migrations contain `DROP POLICY IF EXISTS` |
| `config:inertia-page-paths` | `config/inertia.php` exists + pins testing paths to capital-P `js/Pages` |

## Cumulative session state — 41 ticks closed

- **Doc-phase ticks this run:** **41** (132 → 172)
- **Substrate verifier:** **110/110 PASS**
- **Live pytest cases:** 284
- **Laravel test cases (Track3 eval-dashboard, pgsql):** 4/4 PASS
- **Sections closed:** §25.4 + §6 + §04i validators
- **§04i validators:** 6 of 6 — graduated + dashboard-surfaced
- **Evaluator kinds wireable:** 3
- **Hatchet AI pool workflows:** 12
- **§10.6 nightly cron:** live
- **§21.3 types covered:** 8 of 8
- **Migrations idempotent under pgsql phpunit:** 5 of 5 affected
- **PublicGeo features on map:** 95

## What's next

The remaining 3 Track3 dashboard tests (`decision_history_admin`,
`support_cockpit_admin`, `support_cockpit_status_filter`) still fail
under pgsql phpunit — root cause is `audit.audit_ledger` is
provisioned by raw SQL (`database/raw/phase0/100-audit-verify-function.sql`
+ companions) NOT by Laravel migrations. So `migrate:fresh` doesn't
create it in `georag_test`.

That's a **separate carry-over** (Track3 audit-table provisioning
in test DB) — needs either:
- a fresh migration mirroring the raw SQL, OR
- a test bootstrap step that runs the raw SQL before
  `RefreshDatabase` fires, OR
- a guard in `AuditEmitter` that returns silently when
  `audit.audit_ledger` is absent in the current connection

Other productive next directions (carried forward):

- **Wire the cron's `failure_summary` into Slack/PagerDuty** via the
  existing `external_notification` workflow
- **Fix bge-reranker-base ONNX config** so Layer 5 sharpens
- **Ingest a sample project's documents** so retrieval surfaces real
  chunks
- **SME-author core_chat / public_private_boundary / target_recommendation**
  question sets

## Carry-overs

- Doc-phase 171's `test_eval_dashboard_failure_layer_breakdown_returns_canonical_buckets`
  assertion now formally runs under pgsql phpunit — fully closing
  the 171 carry-over.
- The `audit.audit_ledger` raw-SQL provisioning gap blocks 3 of the
  14 Track3DashboardsTest cases. Filed as carry-over; verifier check
  + workaround (test-bootstrap hook OR mirror migration) is the next
  tractable step on the Track3 test-suite cleanup path.
- The DROP-first idempotency pattern is now consistent across all
  workspace-isolation migrations (11 of 11). New RLS migrations
  should follow the same pattern — add a `migrations:policy-drop-first`
  check for any future migration in this family.
- `config/inertia.php` is a tiny override (60 LOC). If the project
  ever publishes the full Inertia config, the override merges
  cleanly; only the two path-related keys differ from the package
  default.
