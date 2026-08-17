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
 * Feature tests for GET /projects/{slug}/compare → Foundry/HoleCompare.
 *
 * Restored 2026-08-17 as part of the reader-core trim reversal (see plan
 * addendum) — no pre-existing test to adapt, modeled on
 * DrillholeDetailControllerTest's seed pattern. The restored controller now
 * wraps its silver.collars/silver.lithology_logs queries in
 * withWorkspaceRls() (it never did before deletion); these tests confirm
 * the project-scoping the wrap is meant to enforce is actually correct.
 */
final class HoleCompareControllerTest extends TestCase
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

    public function test_compare_renders_with_pickable_list(): void
    {
        ['user' => $user, 'project' => $project, 'workspace_id' => $workspaceId] = $this->seedProjectMember();
        $this->seedCollar($project->project_id, $workspaceId, 'HC-TEST-001');
        $this->seedCollar($project->project_id, $workspaceId, 'HC-TEST-002');

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/compare');

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page
            ->component('Foundry/HoleCompare')
            ->where('empty', false)
            ->has('pickable', 2),
        );
    }

    public function test_compare_hydrates_left_and_right_when_selected(): void
    {
        ['user' => $user, 'project' => $project, 'workspace_id' => $workspaceId] = $this->seedProjectMember();
        $this->seedCollar($project->project_id, $workspaceId, 'HC-LEFT');
        $this->seedCollar($project->project_id, $workspaceId, 'HC-RIGHT');

        $url = '/projects/'.$project->slug.'/compare?left=HC-LEFT&right=HC-RIGHT';
        $response = $this->actingAs($user)->get($url);

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page
            ->where('left.hole_id', 'HC-LEFT')
            ->where('right.hole_id', 'HC-RIGHT'),
        );
    }

    public function test_compare_shows_empty_state_when_project_has_no_collars(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectMember();

        $response = $this->actingAs($user)->get('/projects/'.$project->slug.'/compare');

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page
            ->where('empty', true)
            ->where('pickable', []),
        );
    }

    /**
     * Confirms the project-scoping the restored withWorkspaceRls() wrap is
     * meant to enforce actually holds: a hole belonging to a different
     * project/workspace never appears in this project's pickable list or
     * hydrates via left/right, even when queried by its exact hole_id.
     */
    public function test_compare_excludes_holes_from_other_project(): void
    {
        ['user' => $user, 'project' => $projectA, 'workspace_id' => $workspaceIdA] = $this->seedProjectMember();
        $this->seedCollar($projectA->project_id, $workspaceIdA, 'HC-IN-A');

        ['project' => $projectB, 'workspace_id' => $workspaceIdB] = $this->seedProjectMember();
        $this->seedCollar($projectB->project_id, $workspaceIdB, 'HC-IN-B');

        $url = '/projects/'.$projectA->slug.'/compare?left=HC-IN-A&right=HC-IN-B';
        $response = $this->actingAs($user)->get($url);

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page
            ->where('left.hole_id', 'HC-IN-A')
            // HC-IN-B belongs to projectB — hydrate() scopes by projectA's
            // project_id, so it must not resolve even though the hole_id
            // is correct.
            ->where('right', null)
            ->has('pickable', 1),
        );
    }
}
