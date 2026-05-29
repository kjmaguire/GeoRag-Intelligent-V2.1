# Migration Rollback

**Module 10 Chunk 10.8** — recovery procedure for a botched Laravel migration
(or pgTAP regression that surfaces post-deploy).

## TL;DR

```bash
# Roll back the last migration only.
docker compose exec laravel-octane php artisan migrate:rollback --step=1

# Roll back the last N migrations.
docker compose exec laravel-octane php artisan migrate:rollback --step=N

# Roll back ALL migrations (destructive — wipes the schema).
docker compose exec laravel-octane php artisan migrate:reset
```

Always `--step=N` in production. Never `migrate:reset` against a populated
database.

## When to roll back

| Symptom | Roll back? |
|---------|-----------|
| Migration applied, app boots, but a feature breaks | Maybe — fix-forward is usually faster than rollback |
| Migration applied, app refuses to boot (the `boot guard` from Module 9 9.2) | Yes — roll back to the previous green state |
| pgTAP regression on `main` | Yes — block the deploy, roll back if already deployed |
| Data corruption (rare in Laravel migrations) | **STOP** — restore from backup, do NOT migrate forward |
| Performance regression after migration | Investigate first; rollback only if irreparable |

## Pre-flight checks

Before rolling back, capture the current state:

```bash
# 1. Snapshot the migrations table.
docker compose exec -T postgresql pg_dump -U georag -t migrations \
    georag > /tmp/migrations-snapshot-$(date -u +%Y%m%dT%H%M%SZ).sql

# 2. List migrations in batch order (most-recent batch is the rollback target).
docker compose exec -T laravel-octane php artisan migrate:status | tail -10

# 3. Check the application is in a quiescent state — Horizon queue depth
#    near zero, no active SSE streams, no Dagster runs.
docker compose exec laravel-octane php artisan horizon:status
curl -s http://localhost:8888/metrics | grep horizon_queue_depth
curl -s http://localhost:8000/metrics | grep georag_sse_active
```

If anything is in flight, drain first or you'll race a job against the
schema flip.

## Rollback procedure

### Single-migration rollback

The common case. The last migration introduced a bug; revert it.

```bash
# 1. Note the file you're rolling back so you can fix-forward later.
docker compose exec laravel-octane php artisan migrate:status | tail -3
# → 2026_04_22_180000_add_workspace_id_to_query_audit_log    Ran    [Batch=42]

# 2. Roll back step=1 (drops the last batch, which is typically just one
#    migration unless you `php artisan migrate` multiple files at once).
docker compose exec laravel-octane php artisan migrate:rollback --step=1

# 3. Verify schema is back to expected.
docker compose exec laravel-octane php artisan migrate:status | tail -3
# → previous migration shows Ran; the rolled-back one shows nothing.

# 4. Sanity check pgTAP.
bash database/tests/pgtap/run.sh

# 5. Restart Octane workers — boot guard re-runs.
docker compose exec laravel-octane php artisan octane:reload
```

### Multi-migration rollback

If a deploy applied N migrations and they collectively broke the app:

```bash
# Roll back the entire batch.
docker compose exec laravel-octane php artisan migrate:rollback --step=N
```

Each migration's `down()` method runs in reverse order. **Verify** each
migration in the batch has a non-trivial `down()` — Laravel allows `down()`
to be `// nothing` and that's fine for view drops, but if a migration adds
a column NOT NULL with no default, the `down()` must drop the column or
the rollback fails.

### Stuck rollback

If `down()` itself errors:

```
SQLSTATE[42703]: Undefined column: 7 ERROR: column "x" does not exist
```

This means the schema was edited out-of-band (someone ran ALTER TABLE in
psql directly). Don't force the rollback — manually align the schema:

```bash
# 1. Bring up a fresh DB from backup.
bash ops/runbooks/backup-restore.md   # follow that runbook

# 2. Replay migrations up to the known-good batch.
docker compose exec laravel-octane php artisan migrate --step=N
```

This is a last resort. The boot guard from Module 9 9.2 catches missing
pivot tables, but if some other mid-rollback inconsistency takes the
service down for >10 min, restore-from-backup is faster than debugging.

## Coordinating with FastAPI + Dagster

A migration rollback affects the schema both Laravel and FastAPI read.
Dagster ingestion writes to silver tables — if a rollback drops a column
Dagster expects, Dagster jobs will fail.

```bash
# Pause Dagster before rollback.
docker compose stop dagster-daemon

# Roll back migrations.
docker compose exec laravel-octane php artisan migrate:rollback --step=N

# Restart FastAPI (it caches schema introspection on warm pools).
docker compose restart fastapi

# Resume Dagster only after pgTAP is green.
bash database/tests/pgtap/run.sh
docker compose start dagster-daemon
```

## Coordinating with deploy-rollback

Often a botched migration also means rolling back the application image
that depends on the new schema. See `deploy-rollback.md` for the
image + secret rollback dance. Order:

1. Roll back app image to previous SHA.
2. Roll back migrations.
3. Restart FastAPI + Dagster.
4. Verify health.

If the app on the previous image can't function on the new schema (rare
but possible — if a deploy adds a NOT NULL column the old code doesn't
write to), the rollback order is reversed: schema first, then image.
Inspect the migration's `up()` to decide.

## Audit trail

Every rollback logs to `authz_audit` channel via:

```bash
docker compose exec laravel-octane php artisan tinker
>>> Log::channel('authz_audit')->warning('migration_rollback', ['batch' => 42, 'reason' => 'broke boot guard', 'actor' => 'kyle@example.com']);
```

Querying Loki:
```
{channel="authz_audit"} |= "migration_rollback"
```

## Cross-references

- `ops/runbooks/backup-restore.md` — fallback when rollback fails.
- `ops/runbooks/deploy-rollback.md` — image rollback if app + schema
  must both revert.
- `database/tests/pgtap/` — pgTAP suite that proves schema invariants
  after rollback.
- `app/Providers/AppServiceProvider.php` — `project_user` boot guard
  that catches pivot-table drops on Octane reload.
