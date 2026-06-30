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
        // Skip BEFORE parent::setUp() so RefreshDatabase doesn't run its
        // migrate:fresh against SQLite (where Phase 0's raw-SQL CREATE
        // TABLE statements are no-op'd and any subsequent reference
        // explodes). Read the connection from $_SERVER directly because
        // the Laravel app hasn't booted yet at this point — phpunit.xml's
        // <env force="true"> populates $_SERVER via tests/bootstrap.php.
        $conn = $_SERVER['DB_CONNECTION']
            ?? $_ENV['DB_CONNECTION']
            ?? getenv('DB_CONNECTION')
            ?: 'sqlite';

        if ($conn !== 'pgsql') {
            $this->markTestSkipped(
                'Requires the postgres test connection. Run with `-c phpunit.pgsql.xml`.',
            );

            return;
        }

        parent::setUp();
    }
}
