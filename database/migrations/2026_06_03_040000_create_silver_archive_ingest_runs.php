<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create silver.archive_ingest_runs — parent row for ZIP archive uploads.
 *
 * Background (2026-06-03 audit item C)
 * -------------------------------------
 * The `ingest_zip_archive` Hatchet workflow has the cameco-recovery
 * silent-failure shape: retries=0, no on_failure_task, no progress
 * surface. When extraction crashes mid-archive the user got a 201 from
 * the upload endpoint and then nothing — the workflow vanished from
 * operator view. Memory `[[cameco-recovery-2026-06-02]]` documented
 * the pattern; this migration closes the observability gap for the
 * archive workflow specifically.
 *
 * Design (per AUDIT_AND_FIX_REPORT.md Theme D)
 * ----------------------------------------------
 * One ARCHIVE row per ZIP upload (this table) + one CHILD row per
 * extracted file (reusing existing `silver.ingest_progress` with a
 * new `archive_run_id` FK). Operators see:
 *
 *   archive 7e3a-... (Cameco/zip-2026-06-03.zip) — status=partial,
 *     file_count=47, files_succeeded=43, files_failed=4
 *
 * Plus four ingest_progress rows for the 4 failed children with
 * normal current_stage/error_text. Cancel-archive future work
 * collapses naturally to "cancel parent → cancel queued children".
 *
 * State machine
 * -------------
 *   queued      → extracting  : workflow picked up the ZIP
 *   extracting  → fanning_out : zip extracted, dispatching per-file
 *   fanning_out → completed   : every child reached a terminal state OK
 *   fanning_out → partial     : some children completed, some failed
 *   *           → failed      : workflow crashed (on_failure_task)
 *
 * Terminal states: completed, failed, partial, cancelled.
 *
 * RLS: workspace_id scoped, same canonical pattern as ingest_progress.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            // silver.* tables live in pgsql only — see [[test-db-parity-gap]].
            return;
        }

        // ── archive_ingest_runs parent table ─────────────────────────
        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.archive_ingest_runs (
                archive_run_id        UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id          UUID         NOT NULL,
                project_id            UUID         NOT NULL,
                run_id                UUID         NOT NULL,
                minio_key             TEXT         NOT NULL,
                filename              TEXT         NOT NULL,
                status                TEXT         NOT NULL DEFAULT 'queued',
                file_count            INTEGER      NULL,
                files_succeeded       INTEGER      NOT NULL DEFAULT 0,
                files_failed          INTEGER      NOT NULL DEFAULT 0,
                files_skipped         INTEGER      NOT NULL DEFAULT 0,
                error_text            TEXT         NULL,
                workflow_run_id       TEXT         NULL,
                triggered_by          TEXT         NOT NULL DEFAULT 'upload',
                started_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                completed_at          TIMESTAMPTZ  NULL,
                failed_at             TIMESTAMPTZ  NULL,
                updated_at            TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

                CONSTRAINT archive_ingest_runs_status_valid CHECK (
                    status IN (
                        'queued', 'extracting', 'fanning_out',
                        'completed', 'failed', 'partial', 'cancelled'
                    )
                ),
                CONSTRAINT archive_ingest_runs_triggered_by_valid CHECK (
                    triggered_by IN ('upload', 'manual_retry', 'cron_recovery')
                ),
                CONSTRAINT archive_ingest_runs_run_id_unique UNIQUE (run_id),
                CONSTRAINT archive_ingest_runs_counts_nonneg CHECK (
                    files_succeeded >= 0
                    AND files_failed >= 0
                    AND files_skipped >= 0
                )
            )
        SQL);

        DB::statement('CREATE INDEX IF NOT EXISTS idx_archive_ingest_runs_workspace_started
                       ON silver.archive_ingest_runs (workspace_id, started_at DESC)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_archive_ingest_runs_project
                       ON silver.archive_ingest_runs (project_id, started_at DESC)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_archive_ingest_runs_minio_key
                       ON silver.archive_ingest_runs (workspace_id, minio_key)');

        // ── FK to silver.workspaces ─────────────────────────────────
        DB::statement(
            'ALTER TABLE silver.archive_ingest_runs'
            .' ADD CONSTRAINT archive_ingest_runs_workspace_id_fkey'
            .' FOREIGN KEY (workspace_id) REFERENCES silver.workspaces(workspace_id) ON DELETE CASCADE',
        );

        // ── RLS ──────────────────────────────────────────────────────
        // Canonical workspace_isolation policy — same shape as the
        // ingest_progress + targeting policies. Fail-open when GUC
        // is unset (admin / migration paths).
        DB::statement('ALTER TABLE silver.archive_ingest_runs ENABLE ROW LEVEL SECURITY');
        DB::statement('ALTER TABLE silver.archive_ingest_runs FORCE ROW LEVEL SECURITY');
        DB::statement(<<<'SQL'
            CREATE POLICY archive_ingest_runs_workspace_isolation
              ON silver.archive_ingest_runs
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
              WITH CHECK (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);

        // ── Grants ──────────────────────────────────────────────────
        // georag_read for SELECT (UI surfaces); georag_write for INSERT/
        // UPDATE (workflow code path). Existing georag_app inherits both.
        DB::statement('GRANT SELECT ON silver.archive_ingest_runs TO georag_read');
        DB::statement('GRANT INSERT, UPDATE ON silver.archive_ingest_runs TO georag_write');

        // ── Link ingest_progress child rows back to the archive ────
        // Nullable: only zip-extracted files carry an archive_run_id;
        // direct uploads (PDF / TIFF / individual files) leave it NULL.
        // ON DELETE SET NULL so dropping an archive_ingest_runs row
        // doesn't cascade-delete the per-file lineage history.
        DB::statement(
            'ALTER TABLE silver.ingest_progress'
            .' ADD COLUMN IF NOT EXISTS archive_run_id UUID NULL',
        );
        DB::statement(
            'ALTER TABLE silver.ingest_progress'
            .' DROP CONSTRAINT IF EXISTS ingest_progress_archive_run_id_fkey',
        );
        DB::statement(
            'ALTER TABLE silver.ingest_progress'
            .' ADD CONSTRAINT ingest_progress_archive_run_id_fkey'
            .' FOREIGN KEY (archive_run_id)'
            .' REFERENCES silver.archive_ingest_runs(archive_run_id)'
            .' ON DELETE SET NULL',
        );
        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_ingest_progress_archive_run_id'
            .' ON silver.ingest_progress (archive_run_id)'
            .' WHERE archive_run_id IS NOT NULL',
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS silver.idx_ingest_progress_archive_run_id');
        DB::statement(
            'ALTER TABLE silver.ingest_progress'
            .' DROP CONSTRAINT IF EXISTS ingest_progress_archive_run_id_fkey',
        );
        DB::statement(
            'ALTER TABLE silver.ingest_progress'
            .' DROP COLUMN IF EXISTS archive_run_id',
        );
        DB::statement('DROP TABLE IF EXISTS silver.archive_ingest_runs CASCADE');
    }
};
