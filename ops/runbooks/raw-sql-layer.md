# The second DDL layer — `database/raw/`

## What this is

GeoRAG has two schema layers, and only one of them reaches Azure.

| Layer | Applied by | State on Azure |
|---|---|---|
| `database/migrations/*.php` | CD, via `laravel-migrate-job` | 257/257 applied, no drift either way |
| `database/raw/**/*.sql` | by hand locally; `ci.yml` against a throwaway Postgres | **never run** |

`cd.yml` deploys by running `php artisan migrate --force` and nothing else.
There is no raw-SQL step anywhere in it.

## Why that is worse than "some objects are missing"

Several `*_for_test_db` migrations create a **cut-down mirror** of a table with
`CREATE TABLE IF NOT EXISTS`, on the stated assumption that production already
has the real one from raw SQL. On a cluster built from migrations alone — which
is every Azure cluster — the mirror runs first and *becomes* the production
schema.

`audit.audit_ledger_verification_runs` is the worst case: Azure carries the
8-column mirror, so the nightly `audit_ledger_verify` workflow has failed on
every run observed since 2026-08-11 with

```
UndefinedColumnError: column "workflow_run_id" of relation
"audit_ledger_verification_runs" does not exist
```

The audit-ledger hash chain has therefore never been verified in production.

Twenty-two objects exist only in raw SQL, and live code queries several of
them — `ShadowRouter` reads `workspace.feature_flags` on every ingest routing
decision, the tool gateway inserts into `workspace.tool_invocations` on every
tool call, and `routers/interpretation.py` reads all four `interpretation.*`
tables. Each of those is a runtime error on Azure today.

## The gate

`scripts/check-raw-migration-parity.php` compares the two trees and fails when
raw SQL declares a table or function no migration creates. It runs in CI as
**Raw SQL / migration parity gate**.

The existing 22-object gap is baselined in `scripts/raw-parity-baseline.txt` so
the gate could land immediately. Anything new fails; so does a baselined entry
that has been closed but left in the file. The list can only shrink.

```bash
php scripts/check-raw-migration-parity.php
```

Regenerate the baseline after closing entries:

```bash
php scripts/check-raw-migration-parity.php --update-baseline
```

## Applying raw SQL

`php artisan db:apply-raw` applies an ordered, explicit manifest
(`database/raw/manifest.json`) through the app's own connection, one
transaction per file, failing loudly. It is deliberately not a directory glob:
`ci.yml` globs and swallows every error with `|| true`, which is fine against a
throwaway database and unacceptable against a real one.

A file earns its place in the manifest by being idempotent, re-run-safe, and by
declaring the relations it operates on, so a database missing them is reported
rather than half-applied.

**Always dry-run against a database you have not applied to before.** The
manifest's phase-0 tenancy files add columns, backfill them, flip them
`NOT NULL`, add FK CASCADEs and enable RLS — they change data, not just shape.

```bash
php artisan db:apply-raw --pretend --database=pgsql_migrations
```

Then, in a maintenance window with a fresh PITR restore point confirmed:

```bash
php artisan db:apply-raw --database=pgsql_migrations
```

Run it from the `laravel-migrate-job` container, which already holds the
credentials CD uses:

```bash
az containerapp job start -g georag -n laravel-migrate-job
```

(That job runs `artisan migrate` only. To run the apply, update its command for
the run, or exec into a `laravel-octane-cc` replica and run it there — the
`pgsql_migrations` connection is the same.)

## Closing the gap for good

Two acceptable end states, and the choice is per-object:

1. **Port it into the migration chain** and delete the raw file. This is the
   direction `2026_08_19_040000` and `2026_08_20_030000` already took, and it is
   right for anything that is plain schema.
2. **Keep it as raw SQL and put it in the manifest**, for DDL the migration
   chain genuinely cannot express well (per-partition RLS sweeps, `\set`-driven
   backfills).

What is not acceptable is a third state: DDL that exists in the repo, is
applied to every developer's laptop and to CI, and never reaches production.
That is the state the gate now prevents.
