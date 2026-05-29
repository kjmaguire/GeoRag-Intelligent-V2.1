<?php

namespace Tests\Unit\Models;

use App\Models\Project;
use App\Models\User;
use Illuminate\Database\QueryException;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Log;
use Tests\TestCase;

/**
 * Unit tests for User::hasProjectAccess() — A1-01 regression suite.
 *
 * Verifies that finding A1-01 from the 2026-04-22 security audit is closed:
 *   "hasProjectAccess() fails OPEN when project_user pivot is missing."
 *
 * These tests run under SQLite in-memory (the default phpunit.xml driver).
 *
 * For the fail-CLOSED tests we construct a QueryException whose code is
 * '42P01' (PostgreSQL "undefined_table") so that isMissingProjectUserPivot()
 * recognises it. PHP's PDOException does not parse the SQLSTATE from the
 * message string — the code must be passed to the RuntimeException parent
 * constructor explicitly or set via the protected $code property.
 */
class UserHasProjectAccessTest extends TestCase
{
    use RefreshDatabase;

    protected function setUp(): void
    {
        parent::setUp();

        Project::getModel()->setTable('projects');
    }

    // -------------------------------------------------------------------------
    // Happy path
    // -------------------------------------------------------------------------

    public function test_returns_true_when_pivot_row_exists(): void
    {
        $user    = User::factory()->create();
        $project = Project::factory()->create();
        $user->projects()->attach($project->project_id, ['role' => 'member']);

        $this->assertTrue($user->hasProjectAccess($project->project_id));
    }

    public function test_returns_false_when_no_pivot_row(): void
    {
        $user    = User::factory()->create();
        $project = Project::factory()->create();
        // Deliberately NOT attaching — no pivot row.

        $this->assertFalse($user->hasProjectAccess($project->project_id));
    }

    // -------------------------------------------------------------------------
    // Fail-CLOSED: pivot table missing
    // -------------------------------------------------------------------------

    public function test_returns_false_when_pivot_table_missing_query_exception(): void
    {
        Log::shouldReceive('critical')->once();

        $user = $this->makeUserThatThrowsMissingPivotException();

        $result = $user->hasProjectAccess('any-project-id');

        $this->assertFalse($result, 'hasProjectAccess must return false (fail-CLOSED) when pivot is missing.');
    }

    public function test_critical_log_fires_when_pivot_table_missing(): void
    {
        Log::shouldReceive('critical')
            ->once()
            ->withArgs(function (string $message, array $context): bool {
                return str_contains($message, 'project_user')
                    && array_key_exists('user_id', $context)
                    && array_key_exists('project_id', $context)
                    && array_key_exists('exception', $context);
            });

        $user = $this->makeUserThatThrowsMissingPivotException();
        $user->hasProjectAccess('some-project-uuid');
    }

    // -------------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------------

    /**
     * Build a QueryException whose SQLSTATE code is '42P01' and whose message
     * contains 'project_user'. isMissingProjectUserPivot() checks both of these
     * conditions so the catch-block in hasProjectAccess() will treat it as a
     * missing-pivot error and return false (fail-CLOSED).
     *
     * PHP's PDOException does NOT parse the SQLSTATE from the message string —
     * the $code property must be set explicitly. We do that via the RuntimeException
     * parent's third constructor argument (not available via PDOException directly),
     * so we use a Closure-based approach to set the protected $code property via
     * Closure::bind, avoiding the need for a custom subclass.
     */
    private function buildMissingPivotException(): QueryException
    {
        // Build a PDOException whose getCode() returns '42P01'.
        $pdoException = new \PDOException(
            'SQLSTATE[42P01]: Undefined table: 7 ERROR: relation "project_user" does not exist'
        );

        // PHP does not parse SQLSTATE from the message string for PDOException
        // constructed in userland code. We must set the protected $code property
        // manually. Closure::bind to internal classes is disallowed in PHP ≥ 8.4,
        // so we use ReflectionProperty instead.
        $refCode = new \ReflectionProperty(\Exception::class, 'code');
        $refCode->setAccessible(true); // no-op on PHP ≥ 8.1, silenced deprecation
        $refCode->setValue($pdoException, '42P01');

        return new QueryException(
            'pgsql',
            'select exists (select * from "project_user" where user_id = ?)',
            [],
            $pdoException,
        );
    }

    /**
     * Return a User partial mock whose projects() relation throws a
     * QueryException that looks exactly like a missing project_user pivot.
     *
     * The BelongsToMany::where() call returns an Eloquent Builder stub whose
     * exists() throws the prepared exception. This mirrors the real call chain:
     *   $this->projects()->where(...)->exists()
     *                     ^ BelongsToMany  ^ Builder
     */
    private function makeUserThatThrowsMissingPivotException(): User
    {
        $queryException = $this->buildMissingPivotException();

        // Builder stub: where() returns self, exists() throws.
        $builderStub = \Mockery::mock(\Illuminate\Database\Eloquent\Builder::class);
        $builderStub->shouldReceive('where')->andReturnSelf();
        $builderStub->shouldReceive('exists')->andThrow($queryException);

        // BelongsToMany stub: where() hands off to the builder stub.
        $relationStub = \Mockery::mock(\Illuminate\Database\Eloquent\Relations\BelongsToMany::class);
        $relationStub->shouldReceive('where')->andReturn($builderStub);

        // User partial mock: only projects() is overridden.
        /** @var User $user */
        $user = \Mockery::mock(User::class)->makePartial();
        $user->shouldReceive('projects')->andReturn($relationStub);

        return $user;
    }
}
