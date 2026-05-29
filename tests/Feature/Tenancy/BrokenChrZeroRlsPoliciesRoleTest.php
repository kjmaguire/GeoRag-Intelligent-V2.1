<?php

declare(strict_types=1);

namespace Tests\Feature\Tenancy;

use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\Test;
use Tests\TestCase;

/**
 * Behavioral pen-test for the 2026-05-28 chr(0) sentinel fix.
 *
 * Companion to WorkspaceRlsCoverageTest::test_no_policy_uses_chr_zero_sentinel
 * (which is a pg_policies catalog scan). This test exercises the actual
 * failure mode: as the `georag_app` role (BYPASSRLS=false) with no
 * `app.workspace_id` GUC set, `SELECT count(*) FROM <affected_table>`
 * was raising `null character not permitted` because the policy
 * expression itself evaluates `chr(0)`. After migration
 * 2026_05_29_190000_replace_broken_chr0_rls_policies, the policy uses
 * the empty-string sentinel and the SELECT must succeed.
 *
 * Gated like GuardSchemaRlsTest: Postgres only, georag_app role must
 * exist (memory: project_init_roles_gap — fresh clusters can lack it),
 * target tables must exist.
 */
final class BrokenChrZeroRlsPoliciesRoleTest extends TestCase
{
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
                'chr(0) policy probe requires it to drop BYPASSRLS.',
            );
        }
    }

    protected function tearDown(): void
    {
        if (DB::connection()->getDriverName() === 'pgsql') {
            try {
                DB::statement('RESET ROLE');
            } catch (\Throwable $e) {
                // Connection may have already closed mid-test.
            }
        }
        parent::tearDown();
    }

    /**
     * @return array<string, array{0: string}>
     */
    public static function affectedTables(): array
    {
        return [
            'silver.workspaces' => ['silver.workspaces'],
            'silver.target_rationales' => ['silver.target_rationales'],
        ];
    }

    #[Test]
    public function georag_app_can_select_from_silver_workspaces_without_chr_zero_error(): void
    {
        $this->assertSelectDoesNotThrow('silver.workspaces');
    }

    #[Test]
    public function georag_app_can_select_from_silver_target_rationales_without_chr_zero_error(): void
    {
        $this->assertSelectDoesNotThrow('silver.target_rationales');
    }

    private function assertSelectDoesNotThrow(string $qualifiedTable): void
    {
        [$schema, $table] = explode('.', $qualifiedTable, 2);

        $tableExists = DB::table('information_schema.tables')
            ->where('table_schema', $schema)
            ->where('table_name', $table)
            ->exists();
        if (! $tableExists) {
            $this->markTestSkipped("{$qualifiedTable} missing on test DB — apply migrations first.");
        }

        // Drop to georag_app (no BYPASSRLS) so the policy is evaluated.
        // Do NOT preset app.workspace_id — the bug fires precisely when
        // the policy hits its "GUC unset" sentinel branch.
        DB::statement('SET ROLE georag_app');

        try {
            $count = DB::selectOne("SELECT count(*) AS n FROM {$qualifiedTable}");
            $this->assertIsNumeric($count->n ?? null, "count(*) on {$qualifiedTable} returned non-numeric");
        } catch (\Throwable $e) {
            if (str_contains($e->getMessage(), 'null character not permitted')) {
                $this->fail(
                    "RLS policy on {$qualifiedTable} still uses chr(0) sentinel — "
                    .'fail-closed under psycopg2/pgsql wire codec. Re-run migration '
                    .'2026_05_29_190000_replace_broken_chr0_rls_policies.',
                );
            }
            if (str_contains($e->getMessage(), 'permission denied for')) {
                // Test DB lacks GRANT SELECT for georag_app on this silver
                // table — a test-DB provisioning gap covered separately by
                // memory `project_init_roles_gap`. Production has the
                // grant (verified 2026-05-28 via psycopg2 + asyncpg probes
                // from the fastapi container). Skip rather than false-fail
                // — the catalog-level check in
                // WorkspaceRlsCoverageTest::test_no_policy_uses_chr_zero_sentinel
                // catches the same regression without needing the grant.
                $this->markTestSkipped(
                    "georag_app lacks SELECT on {$qualifiedTable} in this test DB; "
                    .'the catalog-level chr(0) check covers the same regression.',
                );
            }
            throw $e;
        }
    }
}
