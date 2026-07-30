<?php

declare(strict_types=1);

namespace Tests\Concerns;

/**
 * Skip the test class when the active DB connection is SQLite.
 *
 * The default `phpunit.xml` forces DB_CONNECTION=sqlite for the fast
 * SQLite suite. Tests that read/write Phase 0 schemas living outside
 * Laravel's Schema-Builder migrations (workflow.*, workspace.*, audit.*)
 * cannot run there because SQLite has no PG-only types (jsonb, uuid,
 * timestamptz) and the raw-SQL CREATE TABLE statements are no-op'd by
 * the SQLite compatibility shim in Tests\TestCase.
 *
 * Use this trait alongside RefreshDatabase on a Feature test that targets
 * the dedicated `georag_test` PostgreSQL database (see phpunit.pgsql.xml).
 *
 *   class FooTest extends TestCase
 *   {
 *       use RefreshDatabase;
 *       use RequiresPostgres;
 *
 *       public function test_thing(): void { ... }
 *   }
 */
trait RequiresPostgres
{
    protected function setUp(): void
    {
        // The Docker service exports DB_CONNECTION=pgsql, while phpunit.xml
        // force-overrides Laravel's test connection to SQLite. Reading the
        // process environment before the application boots therefore sees
        // the wrong value. Boot first and use Laravel's resolved connection;
        // this is the only source that reflects PHPUnit's effective config.
        parent::setUp();

        if (config('database.default') !== 'pgsql') {
            $this->markTestSkipped(
                'Requires the postgres test connection. Run with `-c phpunit.pgsql.xml`.',
            );

            return;
        }
    }
}
