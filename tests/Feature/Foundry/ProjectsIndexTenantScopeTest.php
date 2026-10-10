<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Inertia\Testing\AssertableInertia;
use Tests\TestCase;

/**
 * GET /projects — the project picker lists the caller's projects and nobody
 * else's.
 *
 * ProjectsIndexController::show() builds the list from the caller's pivot rows
 * (`$user->projects()`), then re-reads `Project::whereIn('project_id', ...)`.
 * Drop that whereIn and the page lists every tenant's project names, slugs,
 * regions and commodities to anyone who can log in. FoundryRoutesSmokeTest
 * loads /projects with a user who has NO projects and asserts only the Inertia
 * component name, so that regression was invisible.
 *
 * Two users in two workspaces, each with a project, plus a third project
 * nobody here belongs to. The assertions go through the serialized page, which
 * is what a browser receives, not just the props the test picks.
 */
final class ProjectsIndexTenantScopeTest extends TestCase
{
    use RefreshDatabase;

    private User $alice;

    private User $bob;

    private Project $alices;

    private Project $bobs;

    private Project $orphan;

    private string $aliceWorkspace;

    private string $bobWorkspace;

    protected function setUp(): void
    {
        parent::setUp();

        $this->aliceWorkspace = (string) Str::uuid();
        $this->bobWorkspace = (string) Str::uuid();

        $this->alices = $this->projectIn($this->aliceWorkspace, 'Alice Lake Uranium');
        $this->bobs = $this->projectIn($this->bobWorkspace, 'Bob Ridge Gold');
        $this->orphan = $this->projectIn((string) Str::uuid(), 'Nobody Basin Lithium');

        $this->alice = User::factory()->create();
        $this->alice->projects()->attach($this->alices->project_id, ['role' => 'owner']);
        $this->bob = User::factory()->create();
        $this->bob->projects()->attach($this->bobs->project_id, ['role' => 'owner']);
    }

    private function projectIn(string $workspaceId, string $name): Project
    {
        $project = Project::create([
            'project_name' => $name,
            'crs_datum' => 'EPSG:32613',
            'orientation_reference' => 'BOH',
        ]);
        DB::table('silver.projects')
            ->where('project_id', $project->project_id)
            ->update(['workspace_id' => $workspaceId]);

        return $project;
    }

    public function test_a_users_list_contains_their_project_and_not_another_tenants(): void
    {
        $response = $this->actingAs($this->alice)->get('/projects');

        $response->assertOk();
        $response->assertInertia(fn (AssertableInertia $page) => $page
            ->component('Foundry/Projects')
            ->has('projects', 1)
            ->where('projects.0.project_id', $this->alices->project_id)
            ->where('projects.0.project_name', 'Alice Lake Uranium')
            ->where('empty', false)
            ->where('workspace_id', $this->aliceWorkspace),
        );
    }

    public function test_nothing_about_another_tenants_project_reaches_the_browser(): void
    {
        $body = (string) $this->actingAs($this->alice)->get('/projects')->getContent();

        foreach ([$this->bobs, $this->orphan] as $foreign) {
            $this->assertStringNotContainsString((string) $foreign->project_id, $body);
            $this->assertStringNotContainsString((string) $foreign->slug, $body);
            $this->assertStringNotContainsString((string) $foreign->project_name, $body);
        }
        $this->assertStringNotContainsString($this->bobWorkspace, $body);
        // Positive control: the page does carry the caller's own project, so
        // the absences above are not the page failing to render anything.
        $this->assertStringContainsString((string) $this->alices->project_id, $body);
    }

    public function test_each_user_sees_only_their_own_project(): void
    {
        $this->actingAs($this->bob)->get('/projects')->assertInertia(fn (AssertableInertia $page) => $page
            ->has('projects', 1)
            ->where('projects.0.project_id', $this->bobs->project_id)
            ->where('workspace_id', $this->bobWorkspace),
        );
    }

    public function test_a_user_with_no_membership_sees_an_empty_list_though_projects_exist(): void
    {
        $this->assertGreaterThan(0, Project::query()->count());
        $stranger = User::factory()->create();

        $this->actingAs($stranger)->get('/projects')->assertInertia(fn (AssertableInertia $page) => $page
            ->component('Foundry/Projects')
            ->has('projects', 0)
            ->where('empty', true)
            ->where('workspace_id', null),
        );
    }

    public function test_a_membership_gained_later_shows_up(): void
    {
        $this->bob->projects()->attach($this->alices->project_id, ['role' => 'viewer']);

        $this->actingAs($this->bob)->get('/projects')->assertInertia(fn (AssertableInertia $page) => $page
            ->has('projects', 2),
        );
    }
}
