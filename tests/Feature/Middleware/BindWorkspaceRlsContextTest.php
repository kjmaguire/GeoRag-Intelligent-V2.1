<?php

declare(strict_types=1);

namespace Tests\Feature\Middleware;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\Request;
use Illuminate\Support\Facades\Route;
use Tests\TestCase;

/**
 * Which tenant a request gets bound to.
 *
 * The GUC write itself is Postgres-only and skipped here (SQLite has no
 * set_config and no RLS); what these cover is the resolution, which is where
 * a mistake would be dangerous. Binding the WRONG workspace is worse than
 * binding none: an unbound request is visibly over-broad, a wrongly-bound one
 * returns a confidently empty page, or someone else's rows.
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
}
