<?php

declare(strict_types=1);

namespace Tests\Feature;

use Illuminate\Support\Facades\DB;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * CC-03 Item 8 — Project lifecycle column contract on silver.projects.
 *
 * Verifies that:
 *   1. The migration added `lifecycle_state` to silver.projects with the
 *      correct NOT NULL + DEFAULT 'active' contract.
 *   2. The CHECK constraint rejects values outside the four allowed states.
 *   3. The (workspace_id, lifecycle_state) index exists.
 *
 * Read-only catalog inspection on the DEFAULT connection, so it needs the
 * Postgres test connection (`-c phpunit.pgsql.xml`) and the schema the
 * RefreshDatabase group of that config has already migrated; it lives in the
 * "Postgres (read-only)" suite, which runs after it.
 *
 * Until 2026-10-10 this connected to `pgsql_migrations` and skipped when it
 * could not. Under phpunit.xml that connection points at the docker host
 * `postgresql` with no password, which is unreachable on a runner; and the
 * file was in neither suite config, because the manifest guard
 * (PgsqlSuiteManifestTest) only knew the RequiresPostgres / driver-name gates,
 * not "connect to pgsql_migrations or skip". So the contract was checked
 * nowhere. It now uses the normal gate and is listed in phpunit.pgsql.xml.
 *
 * Architecture references
 * -----------------------
 *   CC-03 Item 8 — project hibernation / soft freeze
 */
class ProjectLifecycleTest extends TestCase
{
    use RequiresPostgres;

    /**
     * The lifecycle_state column must exist on silver.projects.
     */
    public function test_lifecycle_state_column_exists(): void
    {
        $exists = DB::connection()
            ->table('information_schema.columns')
            ->where('table_schema', 'silver')
            ->where('table_name', 'projects')
            ->where('column_name', 'lifecycle_state')
            ->exists();

        $this->assertTrue(
            $exists,
            'lifecycle_state column is missing from silver.projects — '
            .'run: php artisan migrate',
        );
    }

    /**
     * The default value must be 'active' — existing rows should not be changed.
     */
    public function test_lifecycle_state_default_is_active(): void
    {
        $default = DB::connection()
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
        $isNullable = DB::connection()
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
        $constraintDef = DB::connection()
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
        $indexExists = DB::connection()
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
            .'run: php artisan migrate',
        );
    }
}
