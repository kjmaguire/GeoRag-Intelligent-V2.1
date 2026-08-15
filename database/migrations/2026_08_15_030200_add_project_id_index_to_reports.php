<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * DB dimension push-to-9.5 sweep (2026-08-15) — missing index found on a
 * hot read path.
 *
 * silver.reports.project_id was added by
 * database/raw/phase0/96-rls-tenant-isolation-block1.sql (mirrored for the
 * test DB by 2026_05_24_220000_provision_reports_workspace_columns_for_test_db.php)
 * alongside workspace_id — but only workspace_id got a supporting index
 * (idx_reports_workspace_id, same raw-SQL file). project_id never did,
 * despite being the WHERE/JOIN column on nearly every project-scoped page
 * load:
 *
 *   - Foundry\ReportController::index/show    (->where('project_id', ...))
 *   - Foundry\CorpusController                (project_id filter + join)
 *   - Foundry\SourcesController                (project_id filter, x2)
 *   - Foundry\OverviewController::buildIngestSummary
 *   - Foundry\IngestionRunsController          (raw SQL WHERE r.project_id = ?)
 *
 * Composite (project_id, updated_at DESC) rather than a bare project_id
 * index — every one of those call sites also orders by updated_at DESC
 * (ReportController.php:39), so the composite serves both the filter and
 * the sort without a separate Sort step.
 *
 * Test-DB parity: no *_provision_*_for_test_db sibling needed — this only
 * indexes a column the existing provision migration
 * (2026_05_24_220000_provision_reports_workspace_columns_for_test_db.php)
 * already creates on the pgsql test DB.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement(
            'CREATE INDEX IF NOT EXISTS idx_reports_project_id
             ON silver.reports (project_id, updated_at DESC)',
        );
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP INDEX IF EXISTS silver.idx_reports_project_id');
    }
};
