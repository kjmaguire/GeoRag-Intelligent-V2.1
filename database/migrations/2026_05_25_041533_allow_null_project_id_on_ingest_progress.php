<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Phase 5 of the reliability spec — drop NOT NULL on
 * silver.ingest_progress.project_id so the nightly integrity sweep can
 * write workspace-scoped audit rows (stage='integrity_sweep') that
 * aren't tied to a single project.
 *
 * The original Phase B schema made project_id NOT NULL on the
 * assumption that every row tracks a single file ingestion. That holds
 * for the upload path; integrity-sweep rows are an audit artifact that
 * spans every project in the workspace.
 *
 * The CHECK / FK / index set on the column is preserved — only the
 * NOT NULL constraint changes.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        DB::statement('ALTER TABLE silver.ingest_progress ALTER COLUMN project_id DROP NOT NULL');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }
        // Reverting requires no rows have NULL project_id. Best effort:
        // delete the integrity-sweep rows so the column can re-add NOT NULL.
        DB::statement('DELETE FROM silver.ingest_progress WHERE project_id IS NULL');
        DB::statement('ALTER TABLE silver.ingest_progress ALTER COLUMN project_id SET NOT NULL');
    }
};
