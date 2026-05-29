# Data-Version Runbook

Documents the §05d monotonicity contract for `data_version`. Use this when debugging a missing bump, verifying a restore preserved monotonicity, or understanding what downstream systems the version drives.

---

## What it is

`data_version BIGINT NOT NULL DEFAULT 0` exists on two tables:

- `silver.workspaces.data_version` — per-workspace monotonic counter
- `projects.data_version` — per-project monotonic counter

Both are initialized to `0` at row creation. After the first committed ingestion run they each advance to `1`. They only ever increase — never reset, never reused.

**Current values (as of 2026-04-20, post first real commit):**

```sql
SELECT workspace_id, name, data_version, updated_at
  FROM silver.workspaces;
-- Expected: a0000000-0000-0000-0000-000000000001 | Default Workspace | 1 | <timestamp>

SELECT id, data_version
  FROM projects;
-- Expected: two project rows, both at data_version = 1
```

Fresh installs start at `0`. A value of `0` means no committed ingestion run has completed for that workspace/project.

---

## Where it bumps

`data_version` is incremented by exactly one asset: `commit_ingestion_run` in `src/dagster/georag_dagster/assets/commit_ingestion_run.py`.

The increment happens inside a single atomic DB transaction:

```sql
UPDATE silver.workspaces
   SET data_version = data_version + 1
 WHERE workspace_id = :workspace_id
RETURNING data_version;

UPDATE projects
   SET data_version = data_version + 1
 WHERE id IN (:project_ids)
RETURNING id, data_version;
```

Both UPDATEs are inside the same `psycopg2` transaction (same `conn` object). If either fails, both roll back — the version never partially bumps.

`data_version` does NOT bump:
- On upload to Bronze (MinIO/SeaweedFS)
- On parser start or Bronze asset materialization
- On any Silver or Gold asset materialization
- On any asset check evaluation
- If any upstream blocking asset check fails (the commit gate never executes)

---

## Where it's consumed

| Consumer | How | Module |
|---|---|---|
| Retrieval cache key | `data_version` used as part of the Redis cache key so a new ingestion run invalidates stale cached query results | Module 4 |
| Reverb broadcast | `commit_ingestion_run` emits `workspace_data_version` and `project_data_versions` as materialization metadata; Module 7 reads these to broadcast the `ingestion.progress` Reverb event | Module 7 |
| Martin tile ETags | Martin tile functions will need the `data_version` incorporated into the ETag hash to invalidate browser tile caches when new spatial data lands. Requires extending the Martin tile function signature to return `(bytea, etag_hash)` | Module 8 |

---

## The monotonic trigger

Two PostgreSQL `BEFORE UPDATE` triggers enforce the invariant at the database layer:

- `silver.workspaces`: `workspaces_data_version_monotonic`
- `projects`: `projects_data_version_monotonic`

Both call `enforce_data_version_monotonic()`:

```sql
CREATE OR REPLACE FUNCTION silver.enforce_data_version_monotonic()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.data_version < OLD.data_version THEN
        RAISE EXCEPTION 'data_version is monotonic — cannot decrement from % to %',
            OLD.data_version, NEW.data_version;
    END IF;
    RETURN NEW;
END;
$$;
```

The trigger fires on `WHEN (NEW.data_version IS DISTINCT FROM OLD.data_version)`. An attempted decrement causes the transaction to roll back immediately. Cannot be bypassed at the application layer — only a superuser dropping the trigger would bypass it.

**Verified test (do not run in production — shown for reference):**

```sql
-- This will FAIL with the monotonic error. Shown only for documentation.
-- UPDATE silver.workspaces SET data_version = -1
--   WHERE workspace_id = 'a0000000-0000-0000-0000-000000000001';
-- ERROR: data_version is monotonic — cannot decrement from 1 to -1
```

---

## Debugging: bump did not happen

**Checklist in order:**

1. Did `commit_ingestion_run` execute at all?
   ```bash
   docker compose logs georag-dagster-daemon --tail 200 | grep 'commit_ingestion_run'
   ```
   If no log line: the asset was skipped. Move to step 2.

2. Which blocking asset check failed?
   Check Dagster UI → Assets → each asset with checks → "Checks" tab. Any `FAILED` check with `blocking=True` prevents `commit_ingestion_run` from executing.

3. Did the workspace config match?
   ```sql
   SELECT workspace_id, name, data_version FROM silver.workspaces;
   ```
   If the workspace UUID passed in `CommitIngestionRunConfig.workspace_id` doesn't exist, the asset fails with `ValueError` (workspace not found).

4. DB-level confirmation:
   ```sql
   SELECT workspace_id, data_version, updated_at FROM silver.workspaces;
   SELECT id, data_version FROM projects WHERE data_version > 0;
   ```
   A value of `0` after a run that appeared successful means the commit asset did not execute or its transaction rolled back.

---

## Debugging: bump happened twice

Should not be possible — the atomic transaction prevents partial double-bumps and Dagster does not execute the same run twice unless explicitly re-triggered. If ever observed:

1. Check Dagster run history for double-materialization of `commit_ingestion_run`:
   ```bash
   docker compose logs georag-dagster-daemon --tail 500 | grep 'commit_ingestion_run.*SUCCESS'
   ```
   Two SUCCESS lines with different run IDs = the asset was materialized twice (e.g. a retry or a manual re-trigger while the first run was still in flight).

2. Confirm `commit_ingestion_run` has no retry policy — it should not. Check `src/dagster/georag_dagster/assets/commit_ingestion_run.py` for a `retry_policy=` argument on the `@asset` decorator. If present, that is a bug.

3. The double-bump is not harmful (both increments are valid and monotonic), but investigate the trigger to prevent a third.

---

## Never decrement — even restores

After a database restore from backup, the restored `data_version` value may be lower than the pre-incident value (the backup predates the incident). The monotonic trigger will still reject any `UPDATE` that decrements.

**Post-restore procedure:**

1. Check the restored value:
   ```sql
   SELECT workspace_id, data_version FROM silver.workspaces;
   ```

2. If the restored value is lower than the pre-incident value known from monitoring/logs, bump it forward to at least the pre-incident value:
   ```sql
   -- Example: pre-incident value was 7, restore brought back 4.
   -- Bump forward to 7 (or higher — monotonicity only requires ≥ pre-incident).
   UPDATE silver.workspaces
      SET data_version = 7
    WHERE workspace_id = 'a0000000-0000-0000-0000-000000000001'
      AND data_version < 7;
   ```

3. Repeat for `projects`:
   ```sql
   UPDATE projects SET data_version = 7 WHERE id = '<project_uuid>' AND data_version < 7;
   ```

4. Verify the trigger accepts the forward bump:
   ```sql
   SELECT data_version FROM silver.workspaces
    WHERE workspace_id = 'a0000000-0000-0000-0000-000000000001';
   -- Must be ≥ pre-incident value
   ```

This procedure ensures retrieval cache keys are invalidated (they reference `data_version`), Martin tile ETags are refreshed, and the system does not serve stale cached results as if they were current.

Reference: `ops/runbooks/backup-restore.md` for the full restore procedure.

---

_Written 2026-04-20 during Module 3 Phase D. Update this file whenever the underlying procedure changes._
