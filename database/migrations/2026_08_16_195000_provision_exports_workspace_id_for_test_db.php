<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

/**
 * Test-DB parity migration — adds `workspace_id` to `silver.exports` on the
 * SQLite fast suite. Same pattern as
 * 2026_08_14_000000_provision_projects_workspace_id_for_test_db (see
 * memory/project_test_db_parity_gap.md).
 *
 * Production/Postgres already has the column, added as a raw-SQL Tier C
 * column by database/raw/phase0/97-rls-tenant-isolation-block2.sql, which
 * the SQLite compatibility hook in tests/TestCase.php never mirrors (raw
 * SQL files aren't run against the SQLite fixture at all).
 *
 * Needed by the 2026-08-16 exports NOT NULL fix: ExportController::store()
 * now sets `workspace_id` from the parent project, and ExportControllerTest
 * exercises that path (Export::create()) on the SQLite suite.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'sqlite') {
            // Postgres already has the column (phase0 raw SQL); keep this a
            // strict no-op there.
            return;
        }

        if (Schema::hasColumn('exports', 'workspace_id')) {
            return;
        }

        Schema::table('exports', function (Blueprint $table): void {
            $table->uuid('workspace_id')->nullable();
        });
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'sqlite') {
            return;
        }

        Schema::table('exports', function (Blueprint $table): void {
            $table->dropColumn('workspace_id');
        });
    }
};
