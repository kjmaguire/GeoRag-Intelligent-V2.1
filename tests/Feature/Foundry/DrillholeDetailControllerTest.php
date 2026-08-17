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
 * Feature tests for GET /projects/{slug}/holes/{collarId}/detail → Foundry/DrillholeDetail.
 *
 * Extracted 2026-08-17 from the deleted LakehouseAndDrillholeDetailTest
 * (split out on restore — Lakehouse itself stays out of scope) as part of
 * the reader-core trim reversal, see plan addendum. Upgraded from an
 * inline `getDriverName() === 'sqlite'` skip to the `RequiresPostgres`
 * trait to match this session's established convention.
 *
 * DrillholeDetailController pins app.workspace_id via withWorkspaceRls()
 * so these tests run against the real DB and exercise the RLS path
 * end-to-end.
 */
final class DrillholeDetailControllerTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    /**
     * Seed the minimum (workspace, project, project_user) needed for the
     * DrillholeDetail authorization path. silver.workspaces + silver.projects
     * are raw-SQL phase0 tables; we INSERT them directly here rather than
     * via factories to keep cross-schema FKs straight.
     *
     * @return array{user: User, project: Project, workspace_id: string}
     */
    private function seedProjectMember(): array
    {
        $user = User::factory()->create();

        // silver.workspaces row (slug required)
        $workspaceId = (string) Str::uuid();
        $slug = 'dhd-test-'.substr($workspaceId, 0, 8);
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$workspaceId, 'Drillhole Detail Test Workspace', $slug],
        );

        $project = Project::factory()->create();

        // Project model's `project_id` lives in silver.projects via the
        // phase0 schema; the factory creates the Eloquent row but doesn't
        // join workspace_id. Backfill workspace_id on silver.projects.
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$workspaceId, $project->project_id],
        );

        // Project model only carries the inverse via User::projects(); attach
        // by inserting into the pivot directly.
        $user->projects()->syncWithoutDetaching([$project->project_id => ['role' => 'viewer']]);

        return ['user' => $user, 'project' => $project, 'workspace_id' => $workspaceId];
    }

    /**
     * Insert a minimal silver.collars row owned by the given project.
     * Returns the collar UUID.
     */
    private function seedCollar(string $projectId, string $workspaceId): string
    {
        $collarId = (string) Str::uuid();

        // workspace_id is added by phase0 raw SQL on prod but not on the test
        // DB. Introspect once + branch the INSERT accordingly.
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
                [$collarId, 'DHD-TEST-001', $projectId, $workspaceId],
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
                [$collarId, 'DHD-TEST-001', $projectId],
            );
        }

        return $collarId;
    }

    public function test_drillhole_detail_renders_for_real_collar(): void
    {
        ['user' => $user, 'project' => $project, 'workspace_id' => $workspaceId] = $this->seedProjectMember();
        $collarId = $this->seedCollar($project->project_id, $workspaceId);

        $url = '/projects/'.$project->slug.'/holes/'.$collarId.'/detail';
        $response = $this->actingAs($user)->get($url);

        $response->assertStatus(200);
        $response->assertInertia(fn (AssertableInertia $page) => $page
            ->component('Foundry/DrillholeDetail')
            ->has('collar')
            ->has('intervals')
            ->has('assays')
            ->has('structures')
            ->has('cross_sections'),
        );
    }

    public function test_drillhole_detail_404_on_unknown_collar(): void
    {
        ['user' => $user, 'project' => $project] = $this->seedProjectMember();

        $url = '/projects/'.$project->slug.'/holes/00000000-0000-0000-0000-000000000000/detail';
        $response = $this->actingAs($user)->get($url);

        $response->assertStatus(404);
    }

    public function test_drillhole_detail_404_when_collar_belongs_to_other_project(): void
    {
        ['user' => $user, 'project' => $projectA, 'workspace_id' => $workspaceIdA] = $this->seedProjectMember();
        $collarInA = $this->seedCollar($projectA->project_id, $workspaceIdA);

        ['project' => $projectB] = $this->seedProjectMember();
        // User has access to projectA (and now projectB? — re-seed gives a fresh
        // user). Re-attach for projectB so the auth gate passes:
        $user->projects()->syncWithoutDetaching([$projectB->project_id => ['role' => 'viewer']]);

        // Hit projectB's slug with a collar from projectA → controller filters
        // by (project_id, collar_id) → 404.
        $url = '/projects/'.$projectB->slug.'/holes/'.$collarInA.'/detail';
        $response = $this->actingAs($user)->get($url);

        $response->assertStatus(404);
    }
}
