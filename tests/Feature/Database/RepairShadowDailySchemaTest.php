<?php

declare(strict_types=1);

namespace Tests\Feature\Database;

use Illuminate\Support\Facades\DB;
use PHPUnit\Framework\Attributes\Group;
use Tests\TestCase;

/**
 * gold.repair_shadow_daily used to be created at runtime by the
 * repair_shadow_aggregate Hatchet workflow's _DDL block. That needed CREATE on
 * the database, which georag_app does not have, so it failed on every scheduled
 * run in production ("permission denied for database georag") and the table
 * never existed there.
 *
 * Migration 2026_08_19_060000 now declares it. These assertions replace the
 * substring checks that lived in src/fastapi/tests/test_repair_shadow_aggregate.py
 * against the old _DDL string — the guarantees are the same, but checked against
 * the database the migration actually produced.
 */
#[Group('database')]
final class RepairShadowDailySchemaTest extends TestCase
{
    protected function setUp(): void
    {
        parent::setUp();

        if (DB::connection()->getDriverName() !== 'pgsql') {
            $this->markTestSkipped('gold.repair_shadow_daily is a PostgreSQL-only object.');
        }
    }

    public function test_table_exists(): void
    {
        $this->assertNotNull(
            DB::selectOne("SELECT to_regclass('gold.repair_shadow_daily') AS oid")->oid,
            'gold.repair_shadow_daily is missing — migration 2026_08_19_060000 did not run.',
        );
    }

    /**
     * The composite PK is what makes the daily upsert idempotent: the same
     * workspace on the same day always lands on the same row.
     */
    public function test_primary_key_is_workspace_id_and_for_date(): void
    {
        $columns = DB::select(
            "SELECT a.attname
               FROM pg_index i
               JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
              WHERE i.indrelid = 'gold.repair_shadow_daily'::regclass
                AND i.indisprimary
              ORDER BY a.attname",
        );

        $this->assertSame(
            ['for_date', 'workspace_id'],
            array_map(static fn ($row): string => $row->attname, $columns),
        );
    }

    /**
     * The table is workspace-scoped. FORCE matters as well as ENABLE — without
     * it the owner role bypasses the policy entirely.
     */
    public function test_row_level_security_is_enabled_and_forced(): void
    {
        $row = DB::selectOne(
            "SELECT relrowsecurity, relforcerowsecurity
               FROM pg_class
              WHERE oid = 'gold.repair_shadow_daily'::regclass",
        );

        $this->assertTrue($row->relrowsecurity, 'RLS is not enabled.');
        $this->assertTrue($row->relforcerowsecurity, 'RLS is not FORCEd — the owner would bypass it.');
    }

    public function test_workspace_isolation_policy_exists(): void
    {
        $this->assertNotNull(
            DB::selectOne(
                "SELECT 1 AS present FROM pg_policies
                  WHERE schemaname = 'gold'
                    AND tablename = 'repair_shadow_daily'
                    AND policyname = 'repair_shadow_daily_workspace_isolation'",
            ),
            'The workspace-isolation policy is missing.',
        );
    }

    /**
     * georag_app performs the upsert; the policy is what enforces tenancy.
     */
    public function test_app_role_can_select_insert_update(): void
    {
        if (DB::selectOne("SELECT 1 AS present FROM pg_roles WHERE rolname = 'georag_app'") === null) {
            $this->markTestSkipped('georag_app role is not provisioned on this cluster.');
        }

        foreach (['SELECT', 'INSERT', 'UPDATE'] as $privilege) {
            $this->assertTrue(
                DB::selectOne(
                    'SELECT has_table_privilege(?, ?, ?) AS granted',
                    ['georag_app', 'gold.repair_shadow_daily', $privilege],
                )->granted,
                sprintf('georag_app is missing %s on gold.repair_shadow_daily.', $privilege),
            );
        }
    }
}
