<?php

declare(strict_types=1);

namespace Tests\Feature\Middleware;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Route;
use RuntimeException;
use Tests\TestCase;

/**
 * Which tenant a request gets bound to.
 *
 * The GUC write itself is Postgres-only and skipped here (SQLite has no
 * set_config and no RLS); what these cover is the resolution and the
 * fail-closed control flow, which is where a mistake would be dangerous.
 * Binding the WRONG workspace is worse than binding none: an unbound request
 * is visibly over-broad, a wrongly-bound one returns a confidently empty
 * page, or someone else's rows. And a request whose bind FAILED must not run
 * at all — see test_a_failed_bind_aborts_the_request_before_the_controller.
 *
 * The end-to-end assertion — that a route under the authenticated group
 * cannot serve a request with the GUC unset — needs a real Postgres and lives
 * with the tenancy suite the Tenant Isolation Auditor workflow runs.
 */
final class BindWorkspaceRlsContextTest extends TestCase
{
    use RefreshDatabase;

    protected function setUp(): void
    {
        parent::setUp();

        Project::getModel()->setTable('projects');

        // The middleware stashes what it resolved on the request, which is
        // the only observable it has on a non-Postgres driver.
        Route::middleware(['api'])->get(
            '/_test/rls/echo',
            fn (Request $r) => ['workspace_id' => $r->attributes->get('workspace_id')],
        );
        Route::middleware(['api'])->get(
            '/_test/rls/echo/{project}',
            fn (Request $r) => ['workspace_id' => $r->attributes->get('workspace_id')],
        );
        Route::middleware(['api'])->get(
            '/_test/rls/slug/{slug}',
            fn (Request $r) => ['workspace_id' => $r->attributes->get('workspace_id')],
        );
    }

    public function test_anonymous_requests_resolve_to_no_workspace(): void
    {
        $this->getJson('/_test/rls/echo')
            ->assertOk()
            ->assertJsonPath('workspace_id', null);
    }

    public function test_a_user_in_one_workspace_is_bound_to_it(): void
    {
        $user = User::factory()->create();
        $project = Project::factory()->create([
            'workspace_id' => 'b0000000-0000-0000-0000-0000000000ff',
        ]);
        $user->projects()->attach($project->project_id, ['role' => 'owner']);

        $this->actingAs($user)
            ->getJson('/_test/rls/echo')
            ->assertOk()
            ->assertJsonPath('workspace_id', 'b0000000-0000-0000-0000-0000000000ff');
    }

    /**
     * The middleware runs before the route's auth:sanctum, so a Bearer-token
     * caller is anonymous to the default `web` guard. Without the sanctum
     * fallback the token request bound '' (RLS disarmed on fail-open tables).
     */
    public function test_a_bearer_token_caller_is_bound_like_a_session_caller(): void
    {
        $user = User::factory()->create();
        $project = Project::factory()->create([
            'workspace_id' => 'b0000000-0000-0000-0000-0000000000ff',
        ]);
        $user->projects()->attach($project->project_id, ['role' => 'owner']);
        $token = $user->createToken('test')->plainTextToken;

        $this->withHeader('Authorization', 'Bearer '.$token)
            ->getJson('/_test/rls/echo')
            ->assertOk()
            ->assertJsonPath('workspace_id', 'b0000000-0000-0000-0000-0000000000ff');
    }

    public function test_a_user_in_several_workspaces_is_bound_to_none(): void
    {
        $user = User::factory()->create();
        foreach (['b0000000-0000-0000-0000-0000000000ff', 'c0000000-0000-0000-0000-0000000000ff'] as $ws) {
            $project = Project::factory()->create(['workspace_id' => $ws]);
            $user->projects()->attach($project->project_id, ['role' => 'owner']);
        }

        // Picking one would silently scope half their work to the wrong
        // tenant. Refusing to guess leaves the existing per-query filters in
        // charge, which is where they already were.
        $this->actingAs($user)
            ->getJson('/_test/rls/echo')
            ->assertOk()
            ->assertJsonPath('workspace_id', null);
    }

    public function test_the_route_project_decides_when_the_user_belongs_to_it(): void
    {
        $user = User::factory()->create();
        $mine = Project::factory()->create([
            'workspace_id' => 'b0000000-0000-0000-0000-0000000000ff',
        ]);
        $other = Project::factory()->create([
            'workspace_id' => 'c0000000-0000-0000-0000-0000000000ff',
        ]);
        $user->projects()->attach($mine->project_id, ['role' => 'owner']);
        $user->projects()->attach($other->project_id, ['role' => 'owner']);

        $this->actingAs($user)
            ->getJson("/_test/rls/echo/{$other->project_id}")
            ->assertOk()
            ->assertJsonPath('workspace_id', 'c0000000-0000-0000-0000-0000000000ff');
    }

    public function test_a_project_the_user_does_not_belong_to_binds_nothing(): void
    {
        $user = User::factory()->create();
        $mine = Project::factory()->create([
            'workspace_id' => 'b0000000-0000-0000-0000-0000000000ff',
        ]);
        $user->projects()->attach($mine->project_id, ['role' => 'owner']);

        $stranger = Project::factory()->create([
            'workspace_id' => 'd0000000-0000-0000-0000-0000000000ff',
        ]);

        // Reading the workspace straight off the route's project would bind
        // this user into a tenant they have no membership in — handing RLS
        // the wrong answer with full confidence.
        $this->actingAs($user)
            ->getJson("/_test/rls/echo/{$stranger->project_id}")
            ->assertOk()
            ->assertJsonPath('workspace_id', null);
    }

    /**
     * SEC-8: every Foundry page is /projects/{slug}/..., and the middleware
     * only read project/project_id/projectId — so for a user in two
     * workspaces every Foundry page bound nothing (fail-open on most
     * policies) while the docblock said "that covers the Foundry pages".
     */
    public function test_a_slug_route_binds_the_workspace_of_that_project(): void
    {
        $user = User::factory()->create();
        $mine = Project::factory()->create([
            'workspace_id' => 'b0000000-0000-0000-0000-0000000000ff',
        ]);
        $other = Project::factory()->create([
            'workspace_id' => 'c0000000-0000-0000-0000-0000000000ff',
        ]);
        $user->projects()->attach($mine->project_id, ['role' => 'owner']);
        $user->projects()->attach($other->project_id, ['role' => 'member']);

        $this->actingAs($user)
            ->getJson("/_test/rls/slug/{$other->slug}")
            ->assertOk()
            ->assertJsonPath('workspace_id', 'c0000000-0000-0000-0000-0000000000ff');
    }

    public function test_a_slug_for_a_project_the_user_does_not_belong_to_binds_nothing(): void
    {
        $user = User::factory()->create();
        foreach (['b0000000-0000-0000-0000-0000000000ff', 'c0000000-0000-0000-0000-0000000000ff'] as $ws) {
            $project = Project::factory()->create(['workspace_id' => $ws]);
            $user->projects()->attach($project->project_id, ['role' => 'owner']);
        }
        $stranger = Project::factory()->create([
            'workspace_id' => 'd0000000-0000-0000-0000-0000000000ff',
        ]);

        $this->actingAs($user)
            ->getJson("/_test/rls/slug/{$stranger->slug}")
            ->assertOk()
            ->assertJsonPath('workspace_id', null);
    }

    /**
     * SEC-2: behind a transaction pooler the session-scoped bind is unsound
     * (proved live in tests/Feature/Tenancy/PooledRlsBindingTest.php). The
     * middleware used to Log::critical and carry on; it now refuses.
     */
    public function test_a_pooled_connection_is_refused_before_the_controller(): void
    {
        if (DB::connection()->getDriverName() === 'pgsql') {
            $this->markTestSkipped('Covered on a real Postgres by PooledRlsBindingTest.');
        }

        $default = (string) config('database.default');
        config()->set("database.connections.{$default}.driver", 'pgsql');
        config()->set("database.connections.{$default}.pooled", true);

        $reached = false;
        Route::middleware(['api'])->get('/_test/rls/pooled', function () use (&$reached): array {
            $reached = true;

            return ['ok' => true];
        });

        $this->getJson('/_test/rls/pooled')->assertStatus(503);
        $this->assertFalse($reached, 'The controller ran behind a pooler with a session-scoped RLS bind.');
    }

    public function test_the_pooler_port_alone_is_also_refused(): void
    {
        if (DB::connection()->getDriverName() === 'pgsql') {
            $this->markTestSkipped('Would repoint the live connection.');
        }

        $default = (string) config('database.default');
        config()->set("database.connections.{$default}.driver", 'pgsql');
        config()->set("database.connections.{$default}.port", '6432');

        $this->getJson('/_test/rls/echo')->assertStatus(503);
    }

    public function test_an_unpooled_connection_is_served(): void
    {
        config()->set('database.connections.'.config('database.default').'.pooled', false);

        $this->getJson('/_test/rls/echo')->assertOk();
    }

    /**
     * The fail-closed contract: a request whose GUC bind FAILS must never
     * reach a controller. On the fail-open policy shape that still covers
     * most of the cluster, an unarmed request sees every workspace's rows —
     * so "log a warning and carry on" was itself the vulnerability (verified
     * live 2026-08-24: 16 of 18 populated RLS tables returned every row to
     * georag_app with the GUC unset).
     */
    public function test_a_failed_bind_aborts_the_request_before_the_controller(): void
    {
        if (DB::connection()->getDriverName() === 'pgsql') {
            // On a real Postgres, the middleware's fail-closed disconnect
            // would sever the connection RefreshDatabase's transaction rides
            // on. The live-Postgres behaviour is pinned by the tenancy suite
            // (FailClosedRlsPolicyTest); this test is about the middleware's
            // control flow, which SQLite exercises fine.
            $this->markTestSkipped('Simulated bind failure is SQLite-suite-only.');
        }

        // Make isPostgres() report true while the live connection stays
        // SQLite, so bind() gets past its driver guard.
        $default = (string) config('database.default');
        config()->set("database.connections.{$default}.driver", 'pgsql');

        // The statement cannot be made to fail organically: the suite's
        // SQLite compatibility hook (tests/TestCase.php) rewrites
        // `SELECT set_config(...)` into a harmless SELECT 1. Throw at the
        // facade seam instead — the same failure surface an unreachable
        // Postgres presents. Partial mock, so everything else (connection
        // resolution, the RefreshDatabase transaction) passes through.
        DB::partialMock()
            ->shouldReceive('statement')
            ->andThrow(new RuntimeException('simulated: could not reach Postgres'));

        $reached = false;
        Route::middleware(['api'])->get('/_test/rls/failed-bind', function () use (&$reached): array {
            $reached = true;

            return ['ok' => true];
        });

        $this->getJson('/_test/rls/failed-bind')->assertStatus(503);

        $this->assertFalse(
            $reached,
            'The controller ran after the RLS bind failed — the request proceeded without tenant isolation.',
        );
    }
}
