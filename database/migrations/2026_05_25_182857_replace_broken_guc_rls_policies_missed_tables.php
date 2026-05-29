<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Follow-up to 2026_05_25_180924_replace_broken_guc_rls_policies_with_canonical
 * — covers the four silver tables the first migration omitted:
 *
 *   silver.collars           — broken `collars_project_scope` policy was
 *                              dropped in production by phase0 raw SQL
 *                              (96-rls-tenant-isolation-block1.sql), so
 *                              the initial pg_policies audit never saw
 *                              it. The test DB still has it.
 *   silver.drill_traces      — first migration treated this as DROP_ONLY
 *                              because production has a canonical via
 *                              phase0 SQL. Test DB does not, so after
 *                              the first migration ran the test DB had
 *                              the table RLS-on with zero policies.
 *   silver.mineral_claims    — same situation as drill_traces.
 *   silver.review_audit_log  — same situation as drill_traces.
 *
 * **Idempotent + no-op-when-already-good.** Each install uses
 * DROP POLICY IF EXISTS then CREATE POLICY, so it's safe to re-run
 * on production where the canonical already exists (we'll just
 * recreate the same policy).
 *
 * SQLite (test DB) does not support RLS — gated on Postgres.
 */
return new class extends Migration
{
    /**
     * Table → broken policies to drop before installing canonical.
     *
     * @var array<string, list<string>>
     */
    private const TARGETS = [
        'collars' => ['collars_project_scope'],
        'drill_traces' => ['drill_traces_tenant_scope'],
        'mineral_claims' => ['mineral_claims_project_scope'],
        'review_audit_log' => ['review_audit_log_project_scope'],
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach (self::TARGETS as $tbl => $brokenPolicies) {
            if (! $this->tableExists($tbl)) {
                continue;
            }

            foreach ($brokenPolicies as $broken) {
                DB::statement("DROP POLICY IF EXISTS {$broken} ON silver.{$tbl}");
            }

            // Only install the canonical when workspace_id exists.
            // Test DB lacks workspace_id on several silver tables (added
            // by phase0 raw SQL in production) — CREATE POLICY would
            // fail there. The WorkspaceRlsCoverageTest tracks the gap
            // via its EXEMPT_TABLES list; lifting that list is the
            // separate test-DB parity follow-up.
            if (! $this->hasWorkspaceIdColumn($tbl)) {
                continue;
            }

            $policy = "{$tbl}_workspace_isolation";
            DB::statement("ALTER TABLE silver.{$tbl} ENABLE ROW LEVEL SECURITY");
            DB::statement("DROP POLICY IF EXISTS {$policy} ON silver.{$tbl}");
            DB::statement(<<<SQL
                CREATE POLICY {$policy} ON silver.{$tbl}
                  USING (
                    NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                    OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
                  )
            SQL);
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach (array_keys(self::TARGETS) as $tbl) {
            $policy = "{$tbl}_workspace_isolation";
            DB::statement("DROP POLICY IF EXISTS {$policy} ON silver.{$tbl}");
        }
    }

    private function tableExists(string $table): bool
    {
        return DB::table('information_schema.tables')
            ->where('table_schema', 'silver')
            ->where('table_name', $table)
            ->exists();
    }

    private function hasWorkspaceIdColumn(string $table): bool
    {
        return DB::table('information_schema.columns')
            ->where('table_schema', 'silver')
            ->where('table_name', $table)
            ->where('column_name', 'workspace_id')
            ->exists();
    }
};
