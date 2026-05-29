<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Phase B of the Ingestion Runs UI — create silver.ingest_progress.
 *
 * One row per (workspace_id, minio_key). Each Hatchet step in
 * src/fastapi/app/hatchet_workflows/ingest_pdf.py + tiff_normalize.py writes
 * a row on entry and updates on completion. The IngestionRunsController joins
 * this table to surface real per-file progress instead of the time-elapsed
 * heuristic stage labels Phase A shipped with.
 *
 * Steps modelled:
 *   1  preflight     — validate magic bytes, count pages
 *   2  parse         — fitz / docling / tesseract extraction
 *   3  persist       — write silver.reports + document_passages
 *   4  embed_verify  — confirm embeddings or dispatch to Dagster
 *   5  embedding     — Dagster fills passage embedding_ids
 *
 * On the final step, completed_at is set + report_id is back-filled so the
 * UI can swap the row from "in flight" to "completed" the moment the last
 * embedding lands.
 *
 * See [[ingestion-runs-ui-2026-05-24]] for the full design.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS silver.ingest_progress (
                progress_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                workspace_id       uuid NOT NULL,
                project_id         uuid NOT NULL,
                workflow_run_id    text,
                minio_key          text NOT NULL,
                filename           text NOT NULL,

                current_step       text NOT NULL DEFAULT 'queued',
                step_index         integer NOT NULL DEFAULT 0,
                total_steps        integer NOT NULL DEFAULT 5,
                step_started_at    timestamptz,

                started_at         timestamptz NOT NULL DEFAULT now(),
                updated_at         timestamptz NOT NULL DEFAULT now(),
                completed_at       timestamptz,
                failed_at          timestamptz,
                error_text         text,

                report_id          uuid,

                CONSTRAINT ingest_progress_workspace_key_uniq
                    UNIQUE (workspace_id, minio_key),
                CONSTRAINT ingest_progress_step_valid
                    CHECK (current_step IN (
                        'queued', 'preflight', 'parse', 'persist',
                        'embed_verify', 'embedding', 'completed', 'failed'
                    )),
                CONSTRAINT ingest_progress_step_index_bounded
                    CHECK (step_index >= 0 AND step_index <= total_steps)
            )
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_ingest_progress_project_active
             ON silver.ingest_progress (project_id, updated_at DESC)
             WHERE completed_at IS NULL AND failed_at IS NULL',
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_ingest_progress_project_all
             ON silver.ingest_progress (project_id, updated_at DESC)',
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        DB::statement('DROP TABLE IF EXISTS silver.ingest_progress');
    }
};
