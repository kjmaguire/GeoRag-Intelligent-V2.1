<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

/**
 * Test-DB parity migration — adds `workspace_id` to `silver.projects` on the
 * SQLite fast suite. Same pattern as
 * 2026_05_24_220000_provision_reports_workspace_columns_for_test_db (see
 * memory/project_test_db_parity_gap.md).
 *
 * Production/Postgres already has the column from
 * 2026_04_20_100000_create_workspaces_and_data_version, but that ALTER uses
 * an FK REFERENCES clause + multi-ADD-COLUMN syntax, both of which the
 * SQLite compatibility hook in tests/TestCase.php no-ops — so the SQLite
 * test DB never got the column.
 *
 * Needed by the 2026-08-14 citation IDOR fix: CitationController derives the
 * caller's accessible workspaces via
 * `$user->projects()->pluck('silver.projects.workspace_id')`, and the
 * CitationControllerIDORTest cross-tenant cases exercise that path on the
 * SQLite suite.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'sqlite') {
            // Postgres already has the column (2026_04_20_100000); keep this
            // a strict no-op there.
            return;
        }

        if (Schema::hasColumn('projects', 'workspace_id')) {
            return;
        }

        Schema::table('projects', function (Blueprint $table): void {
            $table->uuid('workspace_id')->nullable();
        });
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'sqlite') {
            return;
        }

        Schema::table('projects', function (Blueprint $table): void {
            $table->dropColumn('workspace_id');
        });
    }
};
