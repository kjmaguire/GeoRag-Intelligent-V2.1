<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Phase 1 of the ingestion reliability spec — move silver.ingest_progress from
 * "one row per (workspace_id, minio_key)" to "one row per run" with immutable
 * terminal states.
 *
 * Why:
 *   The original schema overwrites the row on every retry, which means:
 *     - A delayed worker can clobber a failed row with completed (and vv).
 *     - Recovery dispatches have nowhere to record their lineage —
 *       parent_run_id is needed to link recovery work to the original.
 *     - Hatchet's on_failure_task hook can't safely assume the row state.
 *
 * Per spec invariant: every run reaches exactly one terminal state and
 * terminal states are immutable. Recovery work creates new rows linked to
 * the original via parent_run_id.
 *
 * Migration shape:
 *   1. Add new columns: run_id, status, current_stage, last_stage_started_at,
 *      last_heartbeat_at, worker_id, attempt_number, parent_run_id,
 *      recovery_reason, triggered_by
 *   2. Backfill from existing current_step / completed_at / failed_at
 *   3. Drop UNIQUE(workspace_id, minio_key) — replaced by view below
 *   4. Add CHECK on status (queued|started|completed|failed|cancelled|timed_out)
 *   5. Add silver.ingest_progress_latest_per_file view so IngestionRunsController
 *      keeps working without changes in this phase
 *
 * IngestionRunsController + _progress.py will be migrated to run_id-based
 * reads/writes in the same PR, but the view keeps any unconverted reader path
 * (or the rollback case) safe.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.ingest_progress
                ADD COLUMN IF NOT EXISTS run_id                 uuid,
                ADD COLUMN IF NOT EXISTS status                 text,
                ADD COLUMN IF NOT EXISTS current_stage          text,
                ADD COLUMN IF NOT EXISTS last_stage_started_at  timestamptz,
                ADD COLUMN IF NOT EXISTS last_heartbeat_at      timestamptz,
                ADD COLUMN IF NOT EXISTS worker_id              text,
                ADD COLUMN IF NOT EXISTS attempt_number         integer NOT NULL DEFAULT 1,
                ADD COLUMN IF NOT EXISTS parent_run_id          uuid,
                ADD COLUMN IF NOT EXISTS recovery_reason        text,
                ADD COLUMN IF NOT EXISTS triggered_by           text NOT NULL DEFAULT 'upload'
        SQL);

        // Backfill run_id and status for existing rows. run_id mirrors
        // progress_id so any in-flight UI references stay valid; status is
        // derived from the existing completion/failure timestamps.
        DB::statement(<<<'SQL'
            UPDATE silver.ingest_progress
            SET run_id = COALESCE(run_id, progress_id),
                status = COALESCE(status, CASE
                    WHEN completed_at IS NOT NULL THEN 'completed'
                    WHEN failed_at    IS NOT NULL THEN 'failed'
                    WHEN current_step = 'queued'  THEN 'queued'
                    ELSE 'started'
                END),
                current_stage = COALESCE(current_stage, current_step)
        SQL);

        DB::statement('ALTER TABLE silver.ingest_progress ALTER COLUMN run_id SET NOT NULL');
        DB::statement('ALTER TABLE silver.ingest_progress ALTER COLUMN run_id SET DEFAULT gen_random_uuid()');
        DB::statement('ALTER TABLE silver.ingest_progress ALTER COLUMN status SET NOT NULL');
        DB::statement("ALTER TABLE silver.ingest_progress ALTER COLUMN status SET DEFAULT 'queued'");

        // The original idempotency constraint blocks per-run rows. Drop it —
        // the view below + the attempt_number/parent_run_id columns are the
        // new "latest per file" surface.
        DB::statement('ALTER TABLE silver.ingest_progress DROP CONSTRAINT IF EXISTS ingest_progress_workspace_key_uniq');

        // run_id needs its own uniqueness (we look it up via the polling API).
        DB::statement('CREATE UNIQUE INDEX IF NOT EXISTS ingest_progress_run_id_unique ON silver.ingest_progress (run_id)');

        // Recovery linkage. SET NULL on delete so cleanup of an original run
        // doesn't cascade-delete its recovery chain.
        DB::statement(<<<'SQL'
            ALTER TABLE silver.ingest_progress
                ADD CONSTRAINT ingest_progress_parent_run_fk
                FOREIGN KEY (parent_run_id) REFERENCES silver.ingest_progress(run_id) ON DELETE SET NULL
        SQL);

        // Terminal state CHECK. Allowed values are the spec's six.
        DB::statement('ALTER TABLE silver.ingest_progress DROP CONSTRAINT IF EXISTS ingest_progress_status_valid');
        DB::statement(<<<'SQL'
            ALTER TABLE silver.ingest_progress
                ADD CONSTRAINT ingest_progress_status_valid
                CHECK (status IN ('queued','started','completed','failed','cancelled','timed_out'))
        SQL);

        DB::statement('ALTER TABLE silver.ingest_progress DROP CONSTRAINT IF EXISTS ingest_progress_triggered_by_valid');
        DB::statement(<<<'SQL'
            ALTER TABLE silver.ingest_progress
                ADD CONSTRAINT ingest_progress_triggered_by_valid
                CHECK (triggered_by IN (
                    'upload', 'embed_pending_sweep', 'nightly_integrity_sweep',
                    'manual_retry', 'stale_run_sweep'
                ))
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_ingest_progress_latest_per_file
             ON silver.ingest_progress (workspace_id, minio_key, attempt_number DESC, started_at DESC)',
        );

        // Stale-heartbeat sweep + on_failure_task look up active runs by status
        // — partial index keeps the working set small.
        DB::statement(
            "CREATE INDEX IF NOT EXISTS idx_ingest_progress_active_heartbeat
             ON silver.ingest_progress (last_heartbeat_at)
             WHERE status = 'started'",
        );

        // Backward-compatible view. IngestionRunsController joins this without
        // any code change — surfaces the highest-attempt row per file as
        // "the current run" while the underlying table stores all attempts.
        DB::statement(<<<'SQL'
            CREATE OR REPLACE VIEW silver.ingest_progress_latest_per_file AS
            SELECT DISTINCT ON (workspace_id, minio_key)
                progress_id,
                run_id,
                workspace_id,
                project_id,
                workflow_run_id,
                minio_key,
                filename,
                current_step,
                step_index,
                total_steps,
                step_started_at,
                started_at,
                updated_at,
                completed_at,
                failed_at,
                error_text,
                report_id,
                status,
                current_stage,
                last_stage_started_at,
                last_heartbeat_at,
                worker_id,
                attempt_number,
                parent_run_id,
                recovery_reason,
                triggered_by
            FROM silver.ingest_progress
            ORDER BY workspace_id, minio_key, attempt_number DESC, started_at DESC
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP VIEW IF EXISTS silver.ingest_progress_latest_per_file');
        DB::statement('ALTER TABLE silver.ingest_progress DROP CONSTRAINT IF EXISTS ingest_progress_parent_run_fk');
        DB::statement('ALTER TABLE silver.ingest_progress DROP CONSTRAINT IF EXISTS ingest_progress_status_valid');
        DB::statement('ALTER TABLE silver.ingest_progress DROP CONSTRAINT IF EXISTS ingest_progress_triggered_by_valid');
        DB::statement('DROP INDEX IF EXISTS silver.idx_ingest_progress_latest_per_file');
        DB::statement('DROP INDEX IF EXISTS silver.idx_ingest_progress_active_heartbeat');
        DB::statement('DROP INDEX IF EXISTS silver.ingest_progress_run_id_unique');

        DB::statement(<<<'SQL'
            ALTER TABLE silver.ingest_progress
                DROP COLUMN IF EXISTS run_id,
                DROP COLUMN IF EXISTS status,
                DROP COLUMN IF EXISTS current_stage,
                DROP COLUMN IF EXISTS last_stage_started_at,
                DROP COLUMN IF EXISTS last_heartbeat_at,
                DROP COLUMN IF EXISTS worker_id,
                DROP COLUMN IF EXISTS attempt_number,
                DROP COLUMN IF EXISTS parent_run_id,
                DROP COLUMN IF EXISTS recovery_reason,
                DROP COLUMN IF EXISTS triggered_by
        SQL);

        // Restore the original idempotency constraint on rollback so any code
        // still on the old schema can keep upserting.
        DB::statement(<<<'SQL'
            ALTER TABLE silver.ingest_progress
                ADD CONSTRAINT ingest_progress_workspace_key_uniq
                UNIQUE (workspace_id, minio_key)
        SQL);
    }
};
