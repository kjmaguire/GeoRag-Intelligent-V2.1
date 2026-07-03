<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Close the RLS coverage gaps surfaced by the widened (audit 2026-06-28)
 * WorkspaceRlsCoverageTest denylist: four tables carry a workspace_id
 * column with no policy. Three are fixed here; the fourth
 * (ops.support_tickets) is a documented exemption in the test instead —
 * ops.* is GLOBAL by design (§25.2/§25.3): cross-workspace support access
 * is audit-logged via emit_audit(action_type='support_access') and the
 * cockpit is admin-Gate-scoped, not RLS-blocked.
 *
 * 1. workflow.workflow_runs + workflow.workflow_run_events — test-DB
 *    parity gap, NOT a production gap. Production picks up RLS + the
 *    `tenant_isolation` policy from database/raw/phase0/95-rls-policies.sql
 *    (applied once at PG init); the Laravel test DB gets the tables from
 *    2026_05_14_140100_provision_workflow_schema_for_test_db and never
 *    runs phase0 raw SQL. Same no-op-when-covered reconciliation
 *    semantics as 2026_05_25_175214: production (RLS + policy already
 *    present) is untouched; the test DB gets a first-time install of the
 *    canonical policy. Deliberately NO `OR workspace_id IS NULL` clause:
 *    the production tenant_isolation policy (IS NOT DISTINCT FROM shape)
 *    hides NULL-workspace system rows from tenant sessions, so the
 *    reconciled shape matches — system rows are visible only when the
 *    app.workspace_id GUC is unset (admin/verifier paths).
 *
 * 2. targeting.target_backtests — genuine gap in BOTH environments.
 *    2026_05_13_100000_create_targeting_schema RLS-protected 7 sibling
 *    tables but skipped target_backtests per phase85 ("workspace-
 *    INDEPENDENT ... no RLS"). That call no longer holds: the
 *    field_outcome_learning Hatchet workflow writes per-workspace rows
 *    with real workspace_ids, so per-tenant hit-rate metrics live in the
 *    table. Installed unconditionally (DROP-first so re-runs under
 *    RefreshDatabase are clean — custom schemas survive migrate:fresh,
 *    see doc-phase 172 note in the targeting migration). Policy includes
 *    `OR workspace_id IS NULL` so model-global backtest rows stay visible
 *    to every workspace session.
 *
 * Canonical policy shape (fail-open when the GUC is unset) copied from
 * 2026_05_25_175214_enable_rls_on_phase0_workspace_tables_reconciliation.
 */
return new class extends Migration
{
    public function getConnection(): ?string
    {
        // ALTER TABLE / CREATE POLICY need the owner role (georag), not the
        // runtime georag_app. On the SQLite test connection there is no
        // pgsql_migrations server; fall back so the guard no-ops cleanly.
        return config('database.default') === 'sqlite' ? null : 'pgsql_migrations';
    }

    public function up(): void
    {
        if (config('database.default') === 'sqlite') {
            return;
        }

        // ── workflow.* — reconcile (no-op where production already covers) ──
        $this->reconcileTable('workflow', 'workflow_runs');
        $this->reconcileTable('workflow', 'workflow_run_events');

        // ── targeting.target_backtests — enforce (gap in production too) ──
        DB::statement('ALTER TABLE targeting.target_backtests ENABLE ROW LEVEL SECURITY');
        DB::statement('DROP POLICY IF EXISTS target_backtests_workspace_isolation ON targeting.target_backtests');
        DB::statement(<<<'SQL'
            CREATE POLICY target_backtests_workspace_isolation ON targeting.target_backtests
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);
    }

    public function down(): void
    {
        if (config('database.default') === 'sqlite') {
            return;
        }

        // Only drop what WE created. Production's phase0 tenant_isolation
        // policies on workflow.* carry a different name and are untouched.
        foreach (['workflow_runs', 'workflow_run_events'] as $table) {
            DB::statement(
                "DROP POLICY IF EXISTS workflow_{$table}_workspace_isolation_v2 ON workflow.{$table}"
            );
        }

        // target_backtests had no RLS at all before this migration, so a
        // full revert disables it again (leaving RLS on with no policy
        // would default-deny georag_app).
        DB::statement('DROP POLICY IF EXISTS target_backtests_workspace_isolation ON targeting.target_backtests');
        DB::statement('ALTER TABLE targeting.target_backtests DISABLE ROW LEVEL SECURITY');
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

    private function reconcileTable(string $schema, string $table): void
    {
        if (! $this->tableExists($schema, $table)) {
            // Table missing in this environment — WorkspaceRlsCoverageTest
            // will surface any genuine gap.
            return;
        }

        // Already covered? No-op (the production path).
        if ($this->rlsEnabled($schema, $table) && $this->hasAnyPolicy($schema, $table)) {
            return;
        }

        $qualified = "{$schema}.{$table}";

        DB::statement("ALTER TABLE {$qualified} ENABLE ROW LEVEL SECURITY");

        // Distinct _v2 suffix so we never collide with the phase0
        // tenant_isolation policy name production already carries.
        DB::statement(<<<SQL
            CREATE POLICY {$schema}_{$table}_workspace_isolation_v2 ON {$qualified}
              USING (
                NULLIF(current_setting('app.workspace_id', true), '') IS NULL
                OR workspace_id = NULLIF(current_setting('app.workspace_id', true), '')::uuid
              )
        SQL);
    }
};
