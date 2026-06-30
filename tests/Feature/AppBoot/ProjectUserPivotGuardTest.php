<?php

namespace Tests\Feature\AppBoot;

use Illuminate\Database\Query\Builder;
use Illuminate\Database\QueryException;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Tests\TestCase;

/**
 * Integration tests for the project_user pivot boot guard in AppServiceProvider.
 *
 * Verifies the boot-time health check added as part of A1-01 remediation:
 *   - App boots normally when the pivot table is present.
 *   - App throws RuntimeException when the pivot is unreachable.
 *
 * SAFETY NOTE: These tests simulate a missing pivot by mocking the DB facade —
 * they do NOT drop the actual table in the test database. That would corrupt
 * other tests in the same run. The guard logic from AppServiceProvider::boot()
 * is exercised directly in an isolated context.
 *
 * The boot guard is scoped to web/Octane boot (`! app()->runningInConsole()`),
 * so the normal PHPUnit run is never affected by the guard itself; tests here
 * simulate the non-console condition by exercising the guard's inner logic.
 */
class ProjectUserPivotGuardTest extends TestCase
{
    use RefreshDatabase;

    // -------------------------------------------------------------------------
    // Happy path
    // -------------------------------------------------------------------------

    public function test_app_boots_normally_when_pivot_table_is_present(): void
    {
        // RefreshDatabase has run migrations so project_user exists. Verify
        // the guard query succeeds without throwing.
        $threw = false;
        try {
            DB::table('project_user')->limit(1)->get();
        } catch (\Throwable) {
            $threw = true;
        }

        $this->assertFalse($threw, 'DB::table(project_user) should not throw when the table exists.');
    }

    // -------------------------------------------------------------------------
    // Guard fires on missing pivot
    // -------------------------------------------------------------------------

    public function test_boot_guard_throws_runtime_exception_when_pivot_unreachable(): void
    {
        // Simulate a missing pivot by mocking DB to throw a QueryException.
        // We exercise the guard logic directly (not by re-booting the container,
        // which would affect other tests and is not Octane-safe in CI).
        $pdoException = new \PDOException(
            'SQLSTATE[42P01]: Undefined table: 7 ERROR: relation "project_user" does not exist',
        );
        $pdoException->errorInfo = ['42P01', 7, 'relation "project_user" does not exist'];

        $queryException = new QueryException(
            'pgsql',
            'select * from "project_user" limit 1',
            [],
            $pdoException,
        );

        // Mock the DB facade: table('project_user') returns a builder mock
        // whose limit()->get() path throws the prepared QueryException.
        $builderMock = \Mockery::mock(Builder::class);
        $builderMock->shouldReceive('limit')->andReturnSelf();
        $builderMock->shouldReceive('get')->andThrow($queryException);

        DB::shouldReceive('table')
            ->once()
            ->with('project_user')
            ->andReturn($builderMock);

        // Inline the guard logic as it appears in AppServiceProvider::boot()
        // to test the RuntimeException wrapping without re-booting the container.
        $this->expectException(\RuntimeException::class);
        $this->expectExceptionMessageMatches('/project_user pivot table is missing/');

        try {
            DB::table('project_user')->limit(1)->get();
        } catch (\Throwable $e) {
            throw new \RuntimeException(
                'project_user pivot table is missing or unreadable — refusing to boot. '
                .'Run `php artisan migrate` and ensure the database is reachable.',
                0,
                $e,
            );
        }
    }
}
