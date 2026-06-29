<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Enable workspace-scoped RLS on silver.drill_traces.
 *
 * ADR-0007 PR-4 — the create migration
 * (2026_04_20_170000_create_silver_drill_traces.php) built the table without
 * a tenant-isolation policy. This migration adds the canonical policy
 * identified during the 2026-05-25 RLS coverage audit (project_rls_coverage_audit.md).
 *
 * Policy shape matches every other silver table added post-audit:
 *   USING / WITH CHECK:
 *     workspace_id IS NOT DISTINCT FROM
 *         NULLIF(current_setting('app.workspace_id', true), '')::uuid
 *     OR current_setting('app.workspace_id', true) IS NULL
 *     OR current_setting('app.workspace_id', true) = ''
 *
 * The IS NOT DISTINCT FROM idiom handles NULL on both sides (unset GUC) and
 * avoids the chr(0) sentinel bug documented in project_chr0_rls_sentinel.md.
 *
 * Migration is idempotent: DROP POLICY IF EXISTS before CREATE.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement('ALTER TABLE silver.drill_traces ENABLE ROW LEVEL SECURITY;');
        DB::statement('ALTER TABLE silver.drill_traces FORCE ROW LEVEL SECURITY;');

        DB::statement('DROP POLICY IF EXISTS tenant_isolation ON silver.drill_traces;');

        DB::statement(<<<'SQL'
            CREATE POLICY tenant_isolation ON silver.drill_traces
                USING (
                    workspace_id IS NOT DISTINCT FROM
                        NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    OR current_setting('app.workspace_id', true) IS NULL
                    OR current_setting('app.workspace_id', true) = ''
                )
                WITH CHECK (
                    workspace_id IS NOT DISTINCT FROM
                        NULLIF(current_setting('app.workspace_id', true), '')::uuid
                    OR current_setting('app.workspace_id', true) IS NULL
                    OR current_setting('app.workspace_id', true) = ''
                )
        SQL);
    }

    public function down(): void
    {
        DB::statement('DROP POLICY IF EXISTS tenant_isolation ON silver.drill_traces;');
        DB::statement('ALTER TABLE silver.drill_traces DISABLE ROW LEVEL SECURITY;');
    }
};
