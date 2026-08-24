<?php

declare(strict_types=1);

namespace Tests\Feature\AppBoot;

use App\Providers\AppServiceProvider;
use Illuminate\Database\Query\Builder;
use Illuminate\Database\QueryException;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Mockery\MockInterface;
use Tests\TestCase;

/**
 * The project_user pivot boot guard in AppServiceProvider.
 *
 * These tests call {@see AppServiceProvider::guardProjectUserPivot()} — the
 * real method — rather than re-typing its body. The previous version of this
 * file did the latter: it inlined a copy of the guard and asserted the copy
 * threw, so it passed no matter what the actual provider did. A test that
 * cannot fail when the code changes is not covering the code.
 *
 * What the guard must get right is the DIFFERENCE between two failures that
 * used to be caught by one `catch (\Throwable)`:
 *
 *   - the pivot is missing        → permanent, refuse to boot
 *   - the database is unreachable → transient, boot anyway
 *
 * The second case is not theoretical here: georag-pg-cc is deliberately
 * Stopped 00:00–10:00 UTC by the nightly cost schedule, and the old guard
 * turned that planned downtime into a crash-looping web tier.
 */
class ProjectUserPivotGuardTest extends TestCase
{
    use RefreshDatabase;

    private function guard(): void
    {
        (new AppServiceProvider($this->app))->guardProjectUserPivot();
    }

    /** Build a QueryException carrying a specific SQLSTATE. */
    private function queryException(string $sqlstate, string $message, string $sql): QueryException
    {
        $pdo = new \PDOException($message);
        $pdo->errorInfo = [$sqlstate, 7, $message];

        // QueryException reads the SQLSTATE from the previous exception's
        // errorInfo, so `code` on the PDOException itself must match too —
        // otherwise getCode() returns 0 and every SQLSTATE check misfires.
        $reflected = new \ReflectionProperty(\Exception::class, 'code');
        $reflected->setValue($pdo, $sqlstate);

        return new QueryException('pgsql', $sql, [], $pdo);
    }

    // -------------------------------------------------------------------------
    // Happy path
    // -------------------------------------------------------------------------

    public function test_guard_passes_when_pivot_table_is_present(): void
    {
        // RefreshDatabase has migrated, so project_user exists and the real
        // connection answers `select 1`.
        $this->guard();

        $this->assertTrue(true, 'guardProjectUserPivot() returned without throwing.');
    }

    // -------------------------------------------------------------------------
    // Reachable database, broken schema → fatal
    // -------------------------------------------------------------------------

    public function test_guard_refuses_to_boot_when_pivot_is_missing(): void
    {
        $missing = $this->queryException(
            '42P01',
            'SQLSTATE[42P01]: Undefined table: 7 ERROR: relation "project_user" does not exist',
            'select * from "project_user" limit 1',
        );

        // `select 1` succeeds (the server is up); only the pivot read fails.
        DB::shouldReceive('selectOne')->once()->with('select 1')->andReturn((object) ['?column?' => 1]);
        DB::shouldReceive('table')->once()->with('project_user')->andReturn(
            $this->builderThatThrows($missing),
        );

        $this->expectException(\RuntimeException::class);
        $this->expectExceptionMessageMatches('/project_user pivot table is missing/');

        $this->guard();
    }

    public function test_guard_refuses_to_boot_when_the_app_role_lacks_select_on_the_pivot(): void
    {
        // A GRANT gap presents as a table that exists but cannot be read.
        // It is just as permanent, and just as human-fixable, as a missing
        // table — this deployment has already shipped a CD break of exactly
        // that shape, so the guard must treat it the same way.
        $denied = $this->queryException(
            '42501',
            'SQLSTATE[42501]: Insufficient privilege: 7 ERROR: permission denied for table project_user',
            'select * from "project_user" limit 1',
        );

        DB::shouldReceive('selectOne')->once()->with('select 1')->andReturn((object) ['?column?' => 1]);
        DB::shouldReceive('table')->once()->with('project_user')->andReturn(
            $this->builderThatThrows($denied),
        );

        $this->expectException(\RuntimeException::class);
        $this->expectExceptionMessageMatches('/schema or privilege problem/');

        $this->guard();
    }

    // -------------------------------------------------------------------------
    // Unreachable database → boot anyway
    // -------------------------------------------------------------------------

    public function test_guard_boots_anyway_when_the_database_is_unreachable(): void
    {
        // This is the nightly-shutdown case. The old guard threw here, which
        // meant every Octane worker restart between 00:00 and 10:00 UTC
        // crash-looped against a database that was down on purpose.
        $down = $this->queryException(
            '08006',
            'SQLSTATE[08006]: Connection failure: could not connect to server: Connection refused',
            'select 1',
        );

        DB::shouldReceive('selectOne')->once()->with('select 1')->andThrow($down);
        // The pivot read must NOT be attempted — there is nothing to ask.
        DB::shouldReceive('table')->never();

        $this->guard();

        $this->assertTrue(true, 'guardProjectUserPivot() returned instead of throwing.');
    }

    public function test_guard_boots_anyway_when_postgres_is_shutting_down(): void
    {
        // 57P03 cannot_connect_now — what Postgres returns while it is still
        // starting up, i.e. exactly when the 10:00 UTC restart is in flight.
        $starting = $this->queryException(
            '57P03',
            'SQLSTATE[57P03]: Cannot connect now: the database system is starting up',
            'select 1',
        );

        DB::shouldReceive('selectOne')->once()->with('select 1')->andThrow($starting);
        DB::shouldReceive('table')->never();

        $this->guard();

        $this->assertTrue(true, 'A starting-up server must not be fatal.');
    }

    // -------------------------------------------------------------------------
    // Wiring
    // -------------------------------------------------------------------------

    public function test_boot_runs_the_guard_only_outside_the_console(): void
    {
        // The guard is skipped for artisan (migrations must be allowed to
        // create the table) and runs under Octane, where
        // vendor/laravel/octane/bin/bootstrap.php sets
        // $_ENV['APP_RUNNING_IN_CONSOLE'] = false before boot.
        $source = file_get_contents(app_path('Providers/AppServiceProvider.php'));

        $this->assertStringContainsString(
            'if (! $this->app->runningInConsole()) {',
            $source,
        );
        $this->assertStringContainsString(
            '$this->guardProjectUserPivot();',
            $source,
            'boot() must delegate to the named method, not re-inline the guard.',
        );
    }

    private function builderThatThrows(\Throwable $e): MockInterface
    {
        $builder = \Mockery::mock(Builder::class);
        $builder->shouldReceive('limit')->andReturnSelf();
        $builder->shouldReceive('get')->andThrow($e);

        return $builder;
    }
}
