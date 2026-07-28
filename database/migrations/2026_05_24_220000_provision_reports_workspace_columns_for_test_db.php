<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Database\Schema\Blueprint;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Schema;

/**
 * Test-DB parity migration — adds the four tenant/multi-project columns the
 * production phase0 raw-SQL adds to silver.reports but that never made it
 * into the Laravel migration chain. See memory/project_test_db_parity_gap.md.
 *
 * Production columns (from database/raw/phase0/96-rls-tenant-isolation-block1.sql):
 *   - workspace_id   uuid NOT NULL
 *   - project_id     uuid NULL
 *   - qp_name        text[] NOT NULL DEFAULT '{}'
 *   - effective_date date NULL
 *
 * Without these, controllers that join silver.reports on project_id (e.g.
 * IngestionRunsController, OverviewController.buildIngestSummary) blow up
 * under the test-DB schema.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            Schema::table('reports', function (Blueprint $table): void {
                $table->uuid('workspace_id')->nullable();
                $table->uuid('project_id')->nullable();
                $table->json('qp_name')->default('[]');
                $table->date('effective_date')->nullable();
            });

            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.reports
                ADD COLUMN IF NOT EXISTS workspace_id uuid,
                ADD COLUMN IF NOT EXISTS project_id   uuid,
                ADD COLUMN IF NOT EXISTS qp_name      text[] NOT NULL DEFAULT '{}',
                ADD COLUMN IF NOT EXISTS effective_date date
        SQL);
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            Schema::table('reports', function (Blueprint $table): void {
                $table->dropColumn([
                    'workspace_id',
                    'project_id',
                    'qp_name',
                    'effective_date',
                ]);
            });

            return;
        }

        DB::statement(<<<'SQL'
            ALTER TABLE silver.reports
                DROP COLUMN IF EXISTS workspace_id,
                DROP COLUMN IF EXISTS project_id,
                DROP COLUMN IF EXISTS qp_name,
                DROP COLUMN IF EXISTS effective_date
        SQL);
    }
};
