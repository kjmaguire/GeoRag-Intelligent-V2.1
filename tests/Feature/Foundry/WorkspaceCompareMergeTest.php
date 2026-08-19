<?php

declare(strict_types=1);

namespace Tests\Feature\Foundry;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Inertia\Testing\AssertableInertia;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Guards the 2026-08-19 merge of the standalone Map and Compare pages into
 * the project Workspace.
 *
 * MapController + Foundry/Map.tsx and HoleCompareController +
 * Foundry/HoleCompare.tsx were deleted; /projects/{slug}/map and
 * /projects/{slug}/compare are now named 302 redirects into
 * /projects/{slug}/workspace (compare carrying ?mode=compare, which
 * Workspace.tsx's initialMode() reads).
 *
 * Two things are worth testing about a merge like this and they are not the
 * same thing:
 *
 *   1. The old entry points still resolve. A merge that leaves bookmarks
 *      and route() callers 404ing is a regression no matter how good the
 *      new surface is — hence the redirect assertions, including that
 *      ?mode=compare survives (without it the redirect silently lands on
 *      MAP and Compare looks deleted rather than moved).
 *   2. The capability the deleted page provided still works, and is still
 *      correctly tenancy-scoped. Compare's data now comes from
 *      WorkspaceController::holePayload() instead of
 *      HoleCompareController::hydrate(). The cross-project test below is
 *      the direct descendant of the old
 *      test_compare_excludes_holes_from_other_project: same invariant, new
 *      owner. It matters because the panel now passes a user-picked hole_id
 *      straight to that endpoint, so the endpoint — not a server-rendered
 *      prop — is the only thing standing between a user and another
 *      project's hole.
 */
final class WorkspaceCompareMergeTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    /**
     * @return array{user: User, project: Project, workspace_id: string}
     */
    private function seedProjectMember(): array
    {
        $user = User::factory()->create();

        $workspaceId = (string) Str::uuid();
        $slug = 'hc-test-'.substr($workspaceId, 0, 8);
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$workspaceId, 'Hole Compare Test Workspace', $slug],
        );

        $project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$workspaceId, $project->project_id],
        );
        $user->projects()->syncWithoutDetaching([$project->project_id => ['role' => 'viewer']]);

        return ['user' => $user, 'project' => $project, 'workspace_id' => $workspaceId];
    }

    private function seedCollar(string $projectId, string $workspaceId, string $holeId): string
    {
        $collarId = (string) Str::uuid();

        $hasWorkspaceCol = DB::table('information_schema.columns')
            ->where('table_schema', 'silver')
            ->where('table_name', 'collars')
            ->where('column_name', 'workspace_id')
            ->exists();

        if ($hasWorkspaceCol) {
            DB::statement(
                "INSERT INTO silver.collars (
                    collar_id, hole_id, project_id, workspace_id,
                    easting, northing, elevation, total_depth, azimuth, dip,
                    hole_type, status, geom
                 ) VALUES (
                    ?::uuid, ?, ?::uuid, ?::uuid,
                    500000, 4500000, 1000, 150, 180, -60,
                    'DDH', 'completed',
                    ST_SetSRID(ST_MakePoint(500000, 4500000), 32613)
                 )",
                [$collarId, $holeId, $projectId, $workspaceId],
            );
        } else {
            DB::statement(
                "INSERT INTO silver.collars (
                    collar_id, hole_id, project_id,
                    easting, northing, elevation, total_depth, azimuth, dip,
                    hole_type, status, geom
                 ) VALUES (
                    ?::uuid, ?, ?::uuid,
                    500000, 4500000, 1000, 150, 180, -60,
                    'DDH', 'completed',
                    ST_SetSRID(ST_MakePoint(500000, 4500000), 32613)
                 )",
                [$collarId, $holeId, $projectId],
            );
        }

        return $collarId;
    }

    public function test_legacy_compare_path_redirects_into_workspace_compare_mode(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectMember();

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/compare');

        $response->assertStatus(302);
        // ?mode=compare is load-bearing, not decoration — see initialMode().
        $response->assertRedirect('/projects/'.$project->slug.'/workspace?mode=compare');
    }

    public function test_legacy_map_path_redirects_into_workspace(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectMember();

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/map');

        $response->assertStatus(302);
        // No ?mode= — MAP is the workspace's default mode.
        $response->assertRedirect('/projects/'.$project->slug.'/workspace');
    }

    public function test_legacy_route_names_still_resolve(): void
    {
        // route('foundry.map') / route('foundry.compare') are still called
        // from app code and may be in users' bookmarks; the merge kept the
        // names deliberately. If either name is ever dropped this fails
        // loudly rather than at request time in production.
        ['project' => $project] = $this->seedProjectMember();

        $this->assertSame(
            url('/projects/'.$project->slug.'/map'),
            route('foundry.map', ['slug' => $project->slug]),
        );
        $this->assertSame(
            url('/projects/'.$project->slug.'/compare'),
            route('foundry.compare', ['slug' => $project->slug]),
        );
    }

    public function test_workspace_renders_for_a_project_member(): void
    {
        ['user' => $user, 'project' => $project, 'workspace_id' => $workspaceId] = $this->seedProjectMember();
        $this->seedCollar($project->project_id, $workspaceId, 'HC-TEST-001');
        $this->seedCollar($project->project_id, $workspaceId, 'HC-TEST-002');

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/workspace');

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page
            ->component('Foundry/Workspace')
            ->where('empty', false)
            // COMPARE mode builds its two dropdowns from this prop rather
            // than from the separate 200-row `pickable` query the deleted
            // controller ran, so the holes must actually be in here.
            ->has('collars', 2),
        );
    }

    public function test_hole_payload_serves_an_in_project_hole(): void
    {
        ['user' => $user, 'project' => $project, 'workspace_id' => $workspaceId] = $this->seedProjectMember();
        $this->seedCollar($project->project_id, $workspaceId, 'HC-LEFT');

        $response = $this->actingAs($user)
            ->getJson('/projects/'.$project->slug.'/holes/HC-LEFT/payload');

        $response->assertStatus(200);
        $response->assertJsonPath('hole_id', 'HC-LEFT');
    }

    /**
     * Direct descendant of the deleted
     * test_compare_excludes_holes_from_other_project. Same invariant — a
     * hole belonging to another project must not resolve even when named
     * exactly — now enforced against the endpoint that actually feeds the
     * comparison.
     */
    public function test_hole_payload_refuses_a_hole_from_another_project(): void
    {
        ['user' => $user, 'project' => $projectA, 'workspace_id' => $workspaceIdA] = $this->seedProjectMember();
        $this->seedCollar($projectA->project_id, $workspaceIdA, 'HC-IN-A');

        ['project' => $projectB, 'workspace_id' => $workspaceIdB] = $this->seedProjectMember();
        $this->seedCollar($projectB->project_id, $workspaceIdB, 'HC-IN-B');

        // Asked for under projectA's slug, by its exact hole_id.
        $response = $this->actingAs($user)
            ->getJson('/projects/'.$projectA->slug.'/holes/HC-IN-B/payload');

        $response->assertStatus(404);
        $response->assertJsonPath('error', 'hole_not_found');
    }
}
