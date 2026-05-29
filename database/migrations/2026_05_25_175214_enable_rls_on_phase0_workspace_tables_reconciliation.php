<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Test-DB ↔ production reconciliation 2026-05-25 — converts the RLS
 * coverage that production picks up from `database/raw/phase0/96-99*.sql`
 * (applied once at PG init via scripts/phase0_step2_apply.sh) into a
 * proper Laravel migration so the test DB inherits the same shape via
 * RefreshDatabase.
 *
 * Without this migration, the WorkspaceRlsCoverageTest needs a
 * `EXEMPT_TEST_DB_ONLY_TABLES` list of 14 tables that have RLS in
 * production but not in test. This migration empties that list.
 *
 * **No-op-when-covered semantics.** For each target table we check
 * pg_policies + pg_class to see if RLS is already enabled AND a policy
 * exists. Only if BOTH are missing do we install the canonical
 * workspace_isolation policy. Production tables already have RLS +
 * policies (under various names — `tenant_isolation`,
 * `<table>_workspace_isolation`, etc.) so this migration is a no-op
 * there; in the test DB it's the first-time install.
 *
 * Important nuance: we don't try to rename or canonicalize *existing*
 * production policies, even though several of them are subtly broken
 * (e.g. `silver.geochemistry.geochemistry_tenant_scope` checks
 * `georag.workspace_id` instead of `app.workspace_id` — separate
 * bug, separate fix). The job here is parity, not cleanup.
 *
 * SQLite (test DB) does not support RLS — gated on Postgres.
 *
 * Tables covered: matches EXEMPT_TEST_DB_ONLY_TABLES in
 * tests/Feature/Tenancy/WorkspaceRlsCoverageTest.php exactly.
 */
return new class extends Migration
{
    /**
     * @var list<array{schema: string, table: string, nullable_ws: bool}>
     *
     * `nullable_ws` controls whether the policy includes the
     * `OR workspace_id IS NULL` clause — needed for tables where the
     * workspace_id column is NULL-able and legitimate NULL rows exist
     * (audit/system rows).
     */
    private const TARGETS = [
        ['schema' => 'audit',  'table' => 'audit_ledger',                    'nullable_ws' => true],
        ['schema' => 'audit',  'table' => 'audit_ledger_verification_runs',  'nullable_ws' => true],
        ['schema' => 'gold',   'table' => 'cross_section_panels',            'nullable_ws' => false],
        ['schema' => 'gold',   'table' => 'drillhole_intervals_visual',      'nullable_ws' => false],
        ['schema' => 'gold',   'table' => 'structure_measurements_visual',   'nullable_ws' => false],
        ['schema' => 'silver', 'table' => 'geochemistry',                    'nullable_ws' => false],
        ['schema' => 'silver', 'table' => 'geological_formations',           'nullable_ws' => false],
        ['schema' => 'silver', 'table' => 'geophysics_surveys',              'nullable_ws' => false],
        ['schema' => 'silver', 'table' => 'historic_workings',               'nullable_ws' => false],
        ['schema' => 'silver', 'table' => 'project_boundaries',              'nullable_ws' => false],
        ['schema' => 'silver', 'table' => 'projects',                        'nullable_ws' => false],
        ['schema' => 'silver', 'table' => 'reports',                         'nullable_ws' => false],
        ['schema' => 'silver', 'table' => 'review_queue',                    'nullable_ws' => false],
        ['schema' => 'silver', 'table' => 'spatial_features',                'nullable_ws' => false],
    ];

    public function up(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        foreach (self::TARGETS as $t) {
            $this->reconcileTable($t['schema'], $t['table'], $t['nullable_ws']);
        }
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() === 'sqlite') {
            return;
        }

        // We only drop policies WE created (those named with the
        // reconciliation suffix). Pre-existing production policies are
        // untouched.
        foreach (self::TARGETS as $t) {
            $qualified = "{$t['schema']}.{$t['table']}";
            $policy = $this->reconciledPolicyName($t['schema'], $t['table']);
            DB::statement("DROP POLICY IF EXISTS {$policy} ON {$qualified}");
        }
    }

    private function reconciledPolicyName(string $schema, string $table): string
    {
        // Use a distinct suffix so we don't collide with the policy
        // production already has under `<table>_workspace_isolation`
        // or `tenant_isolation`.
        return "{$schema}_{$table}_workspace_isolation_v2";
    }

    private function tableExists(string $schema, string $table): bool
    {
        return DB::table('information_schema.tables')
            ->where('table_schema', $schema)
            ->where('table_name', $table)
            ->exists();
    }

    private function rlsEnabled(string $schema, string $table): bool
    {
        $row = DB::selectOne(
            'SELECT c.relrowsecurity AS rls
               FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
              WHERE n.nspname = ? AND c.relname = ?',
            [$schema, $table],
        );

        return $row && (bool) $row->rls;
    }

    private function hasAnyPolicy(string $schema, string $table): bool
    {
        return DB::table('pg_policies')
            ->where('schemaname', $schema)
            ->where('tablename', $table)
            ->exists();
    }

    private function reconcileTable(string $schema, string $table, bool $nullableWs): void
    {
        if (! $this->tableExists($schema, $table)) {
            // Table missing entirely — likely a migration ordering issue
            // or the table isn't created in this environment yet. Skip
            // silently; the WorkspaceRlsCoverageTest will surface any
            // genuine gap.
            return;
        }

        // Already covered? No-op (the production path).
        if ($this->rlsEnabled($schema, $table) && $this->hasAnyPolicy($schema, $table)) {
            return;
        }

        $qualified = "{$schema}.{$table}";

        // ENABLE ROW LEVEL SECURITY is idempotent in Postgres; safe to
        // run even on a table that already had RLS but no policy.
        DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");

        $policy = $this->reconciledPolicyName($schema, $table);
        $nullClause = $nullableWs ? "OR workspace_id IS NULL\n                " : '';

        DB::statement(<<<SQL
            CREATE POLICY {$policy} ON {$qualified}
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                {$nullClause}OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);
    }
};
