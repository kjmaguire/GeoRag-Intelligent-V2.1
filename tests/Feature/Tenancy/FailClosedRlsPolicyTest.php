<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\Attributes\Test;
use Tests\TestCase;

/**
 * Behavioural proof that the tables flipped by
 * 2026_08_21_030000_close_fail_open_rls_on_unreferenced_tables return ZERO
 * rows when `app.workspace_id` is not bound.
 *
 * Two shapes of RLS policy coexist in this cluster. The fail-open one leads
 * with
 *
 *     NULLIF(current_setting('app.workspace_id', true), '') IS NULL OR ...
 *
 * so an unbound GUC satisfies the first branch and every workspace's rows
 * come back. That is not a theoretical concern: cluster_runner.py bound the
 * GUC outside a transaction (where SET LOCAL is discarded), looked a project
 * up by slug against a GLOBAL unique index, and wrote an import's LAS curves
 * and collar coordinates into another tenant's project. The bind bug is
 * fixed; this test pins the second half — that on these tables an unbound
 * GUC can no longer mean "show me everything".
 *
 * Gated like BrokenChrZeroRlsPoliciesRoleTest: Postgres only, and the
 * `georag_app` role must exist. The gate matters because the suite connects
 * as `georag`, which is the table OWNER and (locally) a superuser with
 * BYPASSRLS — policies do not apply to it at all. Dropping to `georag_app`
 * via SET LOCAL ROLE is what makes this an honest test of the path
 * production actually takes.
 */
final class FailClosedRlsPolicyTest extends TestCase
{
    /**
     * The exact table => policy set flipped by the migration.
     *
     * @var array<string, string>
     */
    private const FLIPPED = [
        'bronze.raw_collar_entries' => 'bronze_raw_collar_entries_workspace_isolation',
        'bronze.raw_geophysical_runs' => 'bronze_raw_geophysical_runs_workspace_isolation',
        'bronze.raw_surveys' => 'bronze_raw_surveys_workspace_isolation',
        'silver.control_points' => 'silver_control_points_workspace_isolation',
        'silver.historic_workings' => 'silver_historic_workings_workspace_isolation_v2',
        'silver.project_boundaries' => 'silver_project_boundaries_workspace_isolation_v2',
        'silver.sample_intervals' => 'sample_intervals_tenant_isolation',
        'silver.ocr_page_quality' => 'tenant_isolation',
        'silver.parser_run_artifacts' => 'tenant_isolation',
        'silver.table_extraction_quality' => 'tenant_isolation',
        'silver.collab_comments' => 'collab_comments_workspace_isolation',
        'audit.audit_ledger_chain_fork_quarantine' => 'chain_fork_quarantine_workspace_isolation',
    ];

    /**
     * The probe table for the behavioural test: NOT NULL workspace_id, no
     * CHECK constraints, no foreign keys, every other NOT NULL column either
     * defaulted or trivially fillable. Picking a fussier table would test
     * this file's knowledge of that table's constraints, not RLS.
     */
    private const PROBE_TABLE = 'bronze.raw_surveys';

    private const WORKSPACE_A = '4f1c1f3e-0000-4000-8000-00000000000a';

    private const WORKSPACE_B = '4f1c1f3e-0000-4000-8000-00000000000b';

    protected function setUp(): void
    {
        parent::setUp();

        if (DB::connection()->getDriverName() !== 'pgsql') {
            $this->markTestSkipped('RLS is Postgres-only.');
        }

        $hasAppRole = DB::selectOne(<<<'SQL'
            SELECT EXISTS (
                SELECT 1 FROM pg_roles
                 WHERE rolname = 'georag_app' AND rolbypassrls = false
            ) AS present
        SQL);

        if (! ($hasAppRole->present ?? false)) {
            $this->markTestSkipped(
                'georag_app role not provisioned on this PG cluster — '.
                'the fail-closed probe needs a role without BYPASSRLS.',
            );
        }
    }

    protected function tearDown(): void
    {
        if (DB::connection()->getDriverName() === 'pgsql') {
            try {
                DB::statement('RESET ROLE');
            } catch (\Throwable) {
                // Connection may already have closed mid-test.
            }
        }

        parent::tearDown();
    }

    /**
     * @return array<string, array{0: string, 1: string}>
     */
    public static function flippedTables(): array
    {
        $cases = [];
        foreach (self::FLIPPED as $table => $policy) {
            $cases[$table] = [$table, $policy];
        }

        return $cases;
    }

    /**
     * The catalog half: the fail-open branch is gone from the policy text.
     *
     * Cheap, covers all twelve, and fails with the offending expression in
     * the message so a regression is diagnosable from CI output alone.
     */
    #[Test]
    #[DataProvider('flippedTables')]
    public function flipped_policy_has_no_unbound_guc_branch(string $table, string $policy): void
    {
        $row = DB::selectOne(
            <<<'SQL'
            SELECT pg_get_expr(pol.polqual, pol.polrelid) AS qual
              FROM pg_policy pol
             WHERE pol.polrelid = to_regclass(?)
               AND pol.polname = ?
            SQL,
            [$table, $policy],
        );

        if ($row === null) {
            $this->markTestSkipped("{$table} is absent from this cluster.");
        }

        $qual = (string) $row->qual;

        // The fail-open shape reads `<guc expr> IS NULL OR ...`. A policy that
        // mentions the GUC only inside a comparison is fine; one that tests
        // the GUC itself for NULL is the shape being removed.
        $this->assertStringNotContainsString(
            "NULLIF(current_setting('app.workspace_id'::text, true), ''::text) IS NULL",
            $qual,
            "Policy {$policy} on {$table} still has the fail-open unbound-GUC branch: {$qual}",
        );

        $this->assertStringNotContainsString(
            "current_setting('app.workspace_id'::text, true) IS NULL",
            $qual,
            "Policy {$policy} on {$table} still has the fail-open unbound-GUC branch: {$qual}",
        );
    }

    /**
     * The half that actually matters: rows, not policy text.
     *
     * Two rows in two different workspaces, read back as `georag_app` under
     * three GUC states. Everything happens inside one transaction that is
     * rolled back, so the probe rows never outlive the test and the SET LOCAL
     * ROLE reverts with it.
     */
    #[Test]
    public function unbound_guc_returns_zero_rows_and_a_bound_one_returns_only_its_own(): void
    {
        $table = self::PROBE_TABLE;

        if (DB::selectOne('SELECT to_regclass(?) IS NOT NULL AS present', [$table])?->present !== true) {
            $this->markTestSkipped("{$table} is absent from this cluster.");
        }

        DB::beginTransaction();

        try {
            // Inserted as the owner, which is exempt from RLS on this table,
            // so setup cannot be silently filtered by the thing under test.
            DB::insert(
                <<<SQL
                INSERT INTO {$table} (workspace_id, hole_id, depth, raw_row)
                VALUES (?::uuid, 'PROBE-A', 10, '{}'::jsonb),
                       (?::uuid, 'PROBE-B', 20, '{}'::jsonb)
                SQL,
                [self::WORKSPACE_A, self::WORKSPACE_B],
            );

            $this->assertSame(2, $this->probeCount($table), 'owner should see both probe rows');

            // Drop to the application role. Reverts on rollback.
            DB::statement('SET LOCAL ROLE georag_app');

            // 1. GUC never set at all.
            $this->assertSame(
                0,
                $this->probeCount($table),
                "{$table} returned rows to georag_app with app.workspace_id unbound — the policy is still fail-open.",
            );

            // 2. GUC set to the empty string. This is not a hypothetical:
            //    BindWorkspaceRlsContext binds '' on every request whose
            //    workspace it cannot resolve, and again in its finally block.
            DB::statement("SELECT set_config('app.workspace_id', '', true)");
            $this->assertSame(
                0,
                $this->probeCount($table),
                "{$table} returned rows to georag_app with app.workspace_id = '' — the empty-string sentinel is still fail-open.",
            );

            // 3. Bound properly: exactly the one row that belongs to it.
            DB::statement("SELECT set_config('app.workspace_id', ?, true)", [self::WORKSPACE_A]);
            $this->assertSame(
                1,
                $this->probeCount($table),
                "{$table} did not return exactly workspace A's row when correctly bound — the flip over-closed.",
            );
        } finally {
            DB::rollBack();
        }
    }

    private function probeCount(string $table): int
    {
        return (int) DB::selectOne(
            "SELECT count(*) AS n FROM {$table} WHERE hole_id LIKE 'PROBE-%'",
        )->n;
    }
}
