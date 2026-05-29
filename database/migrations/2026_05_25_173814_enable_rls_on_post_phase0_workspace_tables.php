<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Tenancy-leak triage 2026-05-25 — second wave after the bronze RLS
 * migration earlier the same day (2026_05_25_170825). The Lakehouse
 * audit surfaced 14 tables that carry workspace_id but were created
 * after the Phase 0 RLS block (database/raw/phase0/96-rls-tenant-*)
 * and never had ENABLE ROW LEVEL SECURITY applied. All from the
 * May 20-25 build-out wave (CC-01/CC-03 + reliability spec); same
 * pattern as 2026_05_19_180100_enable_rls_on_uncovered_workspace_tables.
 *
 * Policy shape — canonical workspace_isolation, fail-open on unset GUC:
 *
 *     USING (
 *       NULLIF(current_setting('app.workspace_id', true), '') IS NULL
 *       OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
 *     )
 *
 * gold.mv_refresh_log is the one nullable-workspace_id table — its
 * policy also exempts NULL rows (matching audit.query_audit_log) so
 * system-level / unassigned log entries stay readable.
 *
 * No schema changes — every covered table already has a UUID
 * workspace_id column. The fail-open shape means existing writers
 * keep working unchanged; RLS engages anywhere the app sets the GUC.
 *
 * Tables covered (14):
 *   bronze.manifest                  bronze.raw_assay_submissions
 *   bronze.raw_collar_entries        bronze.raw_geophysical_runs
 *   bronze.raw_lithology_logs        bronze.raw_qaqc_submissions
 *   bronze.raw_surveys               gold.mv_refresh_log (nullable)
 *   silver.assessment_report_summaries
 *   silver.completeness_findings     silver.control_points
 *   silver.document_domain_tag       silver.geochronology_samples
 *   silver.ingest_progress
 *
 * SQLite (test DB) does not support RLS — gated on Postgres.
 */
return new class extends Migration
{
    /**
     * Tables whose workspace_id is NOT NULL — get the two-clause policy.
     *
     * @var list<array{schema: string, table: string}>
     */
    private const NOT_NULL_TABLES = [
        ['schema' => 'bronze', 'table' => 'manifest'],
        ['schema' => 'bronze', 'table' => 'raw_assay_submissions'],
        ['schema' => 'bronze', 'table' => 'raw_collar_entries'],
        ['schema' => 'bronze', 'table' => 'raw_geophysical_runs'],
        ['schema' => 'bronze', 'table' => 'raw_lithology_logs'],
        ['schema' => 'bronze', 'table' => 'raw_qaqc_submissions'],
        ['schema' => 'bronze', 'table' => 'raw_surveys'],
        ['schema' => 'silver', 'table' => 'assessment_report_summaries'],
        ['schema' => 'silver', 'table' => 'completeness_findings'],
        ['schema' => 'silver', 'table' => 'control_points'],
        ['schema' => 'silver', 'table' => 'document_domain_tag'],
        ['schema' => 'silver', 'table' => 'geochronology_samples'],
        ['schema' => 'silver', 'table' => 'ingest_progress'],
    ];

    /**
     * Tables whose workspace_id is nullable — get the three-clause policy
     * with an IS NULL exemption (matches audit.query_audit_log).
     *
     * @var list<array{schema: string, table: string}>
     */
    private const NULLABLE_TABLES = [
        ['schema' => 'gold', 'table' => 'mv_refresh_log'],
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach (self::NOT_NULL_TABLES as $t) {
            $this->enableTwoClausePolicy($t['schema'], $t['table']);
        }

        foreach (self::NULLABLE_TABLES as $t) {
            $this->enableNullableWorkspacePolicy($t['schema'], $t['table']);
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach ([...self::NOT_NULL_TABLES, ...self::NULLABLE_TABLES] as $t) {
            $qualified = "{$t['schema']}.{$t['table']}";
            $policy = $this->policyName($t['schema'], $t['table']);
            DB::statement("DROP POLICY IF EXISTS {$policy} ON {$qualified}");
            DB::statement("ALTER TABLE {$qualified} DISABLE ROW LEVEL SECURITY");
        }
    }

    private function policyName(string $schema, string $table): string
    {
        // Same naming pattern as 2026_05_19_180100 — schema_table prefix
        // keeps the policy unique in pg_policies even if two schemas
        // share a table name.
        return "{$schema}_{$table}_workspace_isolation";
    }

    private function enableTwoClausePolicy(string $schema, string $table): void
    {
        $qualified = "{$schema}.{$table}";
        $policy = $this->policyName($schema, $table);

        DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");
        DB::statement(<<<SQL
            CREATE POLICY {$policy} ON {$qualified}
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);
    }

    private function enableNullableWorkspacePolicy(string $schema, string $table): void
    {
        $qualified = "{$schema}.{$table}";
        $policy = $this->policyName($schema, $table);

        DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");
        DB::statement(<<<SQL
            CREATE POLICY {$policy} ON {$qualified}
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);
    }
};
