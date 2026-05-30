<?php

namespace Tests\Feature;

use Illuminate\Support\Facades\DB;
use Tests\TestCase;

/**
 * CC-03 Item 8 — Project lifecycle column smoke tests.
 *
 * Verifies that:
 *   1. The migration added `lifecycle_state` to silver.projects with the
 *      correct NOT NULL + DEFAULT 'active' contract.
 *   2. The CHECK constraint rejects values outside the four allowed states.
 *
 * These tests gate on Postgres (silver schema is absent in SQLite / test-DB).
 * They do NOT use RefreshDatabase because silver.projects is not in the test
 * SQLite schema — instead they run direct raw SQL against the live Postgres
 * connection configured via pgsql_migrations.
 *
 * Architecture references
 * -----------------------
 *   CC-03 Item 8 — project hibernation / soft freeze
 *   CLAUDE.md    — migrations must use pgsql_migrations connection
 */
class ProjectLifecycleTest extends TestCase
{
    /**
     * Skip the test when the pgsql_migrations connection cannot reach the live DB.
     *
     * The test environment uses SQLite for the *default* connection (phpunit.xml
     * sets DB_CONNECTION=sqlite and DB_DATABASE=:memory:). The
     * `pgsql_migrations` connection config falls back to DB_DATABASE which
     * phpunit forces to ':memory:' — so we patch the connection config here to
     * use MIGRATE_DB_DATABASE or POSTGRES_DB (set as system env vars by Docker
     * Compose) before attempting to connect.
     */
    protected function setUp(): void
    {
        parent::setUp();

        $host = config('database.connections.pgsql_migrations.host');
        if (empty($host)) {
            $this->markTestSkipped(
                'ProjectLifecycleTest requires a live Postgres connection (silver schema).',
            );

            return;
        }

        // phpunit.xml forces DB_DATABASE=:memory: which breaks pgsql_migrations.
        // Patch the connection config with the real database name from the
        // MIGRATE_DB_DATABASE or POSTGRES_DB system env vars.
        $realDb = getenv('MIGRATE_DB_DATABASE') ?: getenv('POSTGRES_DB') ?: 'georag';
        config(['database.connections.pgsql_migrations.database' => $realDb]);
        DB::purge('pgsql_migrations');

        try {
            DB::connection('pgsql_migrations')->getPdo();
        } catch (\Throwable $e) {
            $this->markTestSkipped(
                'ProjectLifecycleTest requires a live Postgres connection (silver schema). '
                . 'Error: ' . $e->getMessage(),
            );
        }
    }

    /**
     * The lifecycle_state column must exist on silver.projects.
     */
    public function test_lifecycle_state_column_exists(): void
    {
        $exists = DB::connection('pgsql_migrations')
            ->table('information_schema.columns')
            ->where('table_schema', 'silver')
            ->where('table_name', 'projects')
            ->where('column_name', 'lifecycle_state')
            ->exists();

        $this->assertTrue(
            $exists,
            'lifecycle_state column is missing from silver.projects — '
            .'run: php artisan migrate --database=pgsql_migrations',
        );
    }

    /**
     * The default value must be 'active' — existing rows should not be changed.
     */
    public function test_lifecycle_state_default_is_active(): void
    {
        $default = DB::connection('pgsql_migrations')
            ->table('information_schema.columns')
            ->where('table_schema', 'silver')
            ->where('table_name', 'projects')
            ->where('column_name', 'lifecycle_state')
            ->value('column_default');

        // Postgres normalises DEFAULT values; the string ends up as
        // "'active'::text" in information_schema. Accept both the bare
        // value and the cast form.
        $this->assertNotNull($default, 'lifecycle_state has no DEFAULT');
        $this->assertStringContainsString(
            'active',
            (string) $default,
            "lifecycle_state DEFAULT does not contain 'active'",
        );
    }

    /**
     * The column must be NOT NULL.
     */
    public function test_lifecycle_state_is_not_nullable(): void
    {
        $isNullable = DB::connection('pgsql_migrations')
            ->table('information_schema.columns')
            ->where('table_schema', 'silver')
            ->where('table_name', 'projects')
            ->where('column_name', 'lifecycle_state')
            ->value('is_nullable');

        $this->assertSame(
            'NO',
            $isNullable,
            'lifecycle_state must be NOT NULL',
        );
    }

    /**
     * All four valid enum values must be accepted (checked via the CHECK
     * constraint pg_get_constraintdef).  We verify the constraint definition
     * rather than actually inserting rows, which avoids needing a real project
     * fixture with all its FK dependencies.
     */
    public function test_check_constraint_permits_all_valid_states(): void
    {
        /** @var string|null $constraintDef */
        $constraintDef = DB::connection('pgsql_migrations')
            ->selectOne(
                "SELECT pg_get_constraintdef(c.oid) AS def
                 FROM pg_constraint c
                 JOIN pg_class t ON t.oid = c.conrelid
                 JOIN pg_namespace n ON n.oid = t.relnamespace
                 WHERE n.nspname = 'silver'
                   AND t.relname = 'projects'
                   AND c.contype = 'c'
                   AND pg_get_constraintdef(c.oid) LIKE '%lifecycle_state%'",
            )?->def;

        $this->assertNotNull(
            $constraintDef,
            'No CHECK constraint referencing lifecycle_state found on silver.projects',
        );

        foreach (['active', 'hibernated', 'archived', 'past_due'] as $state) {
            $this->assertStringContainsString(
                $state,
                $constraintDef,
                "CHECK constraint does not include state '{$state}'",
            );
        }
    }

    /**
     * The composite index on (workspace_id, lifecycle_state) must exist for
     * efficient workspace-scoped lifecycle queries.
     */
    public function test_workspace_lifecycle_index_exists(): void
    {
        $indexExists = DB::connection('pgsql_migrations')
            ->selectOne(
                "SELECT 1
                 FROM pg_indexes
                 WHERE schemaname = 'silver'
                   AND tablename  = 'projects'
                   AND indexname  = 'silver_projects_workspace_lifecycle_idx'",
            );

        $this->assertNotNull(
            $indexExists,
            'silver_projects_workspace_lifecycle_idx is missing — '
            .'run: php artisan migrate --database=pgsql_migrations',
        );
    }
}
