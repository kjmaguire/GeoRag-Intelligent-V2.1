<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * 2026-08-17 CI-gap audit — `WorkspaceRlsCoverageTest` never actually ran
 * in CI (see `.github/workflows/ci.yml` laravel job / `phpunit.pgsql.xml`
 * header), so its real failure went undetected: two workspace-scoped
 * tables carry a `workspace_id` column but never got `ENABLE ROW LEVEL
 * SECURITY` + a policy, despite both being explicitly documented as
 * intended to have one:
 *
 *   - `targeting.target_backtests` — its own creating migration
 *     (2026_05_13_100000_create_targeting_schema.php) states "All tables
 *     RLS-protected via app.workspace_id session setting, same pattern as
 *     silver.* phase3 tables" for the whole file, but this table alone
 *     never got the ENABLE ROW LEVEL SECURITY statement. Its `workspace_id`
 *     is deliberately NULLABLE (platform-wide backtest rows are legitimate
 *     — see WorkspaceRlsCoverageTest's own docblock on
 *     test_second_verified_subset_has_no_fail_open_escape_hatch, which
 *     already documents it as intentionally fail-open) — same nullable
 *     three-clause shape as gold.mv_refresh_log.
 *
 *   - `workspace.workspace_roles` — its creating raw SQL
 *     (database/raw/phase0/10-layer-a-workspace-foundation.sql) documents
 *     "workspace_id NULL ⇒ global role; otherwise workspace-scoped" but
 *     Phase 0's RLS sweep only covered the sibling
 *     workspace.workspace_memberships table, missing this one. Global
 *     (NULL workspace_id) system roles must stay readable from every
 *     workspace, so this also needs the nullable three-clause shape, not
 *     the strict two-clause one.
 *
 * (`ops.support_tickets`, the third gap the audit found, is NOT touched
 * here — its own migration, 2026_05_13_140100_create_ops_support_schema,
 * explicitly documents "ops.* schema is GLOBAL — no workspace RLS;
 * cross-workspace access is logged via app.audit.emit_audit... per
 * §25.3." Adding RLS there would contradict that design decision; it's
 * added to WorkspaceRlsCoverageTest::EXEMPT_TABLES instead.)
 *
 * Same nullable-workspace policy shape as
 * 2026_05_25_173814_enable_rls_on_post_phase0_workspace_tables's
 * enableNullableWorkspacePolicy() (fail-open on unset GUC, matching every
 * other not-yet-fail-closed table in this codebase — see
 * WorkspaceRlsCoverageTest's own docblock on why a blanket fail-closed
 * assertion isn't appropriate here).
 *
 * SQLite (test DB fast suite) does not support RLS — gated on Postgres.
 */
return new class extends Migration
{
    /**
     * @var list<array{schema: string, table: string}>
     */
    private const NULLABLE_TABLES = [
        ['schema' => 'targeting', 'table' => 'target_backtests'],
        ['schema' => 'workspace', 'table' => 'workspace_roles'],
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::NULLABLE_TABLES as $t) {
            $this->enableNullableWorkspacePolicy($t['schema'], $t['table']);
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        foreach (self::NULLABLE_TABLES as $t) {
            $qualified = "{$t['schema']}.{$t['table']}";
            $policy = $this->policyName($t['schema'], $t['table']);
            DB::statement("DROP POLICY IF EXISTS {$policy} ON {$qualified}");
            DB::statement("ALTER TABLE {$qualified} DISABLE ROW LEVEL SECURITY");
        }
    }

    private function policyName(string $schema, string $table): string
    {
        return "{$schema}_{$table}_workspace_isolation";
    }

    private function enableNullableWorkspacePolicy(string $schema, string $table): void
    {
        $qualified = "{$schema}.{$table}";
        $policy = $this->policyName($schema, $table);

        DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");
        DB::statement("DROP POLICY IF EXISTS {$policy} ON {$qualified}");
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
