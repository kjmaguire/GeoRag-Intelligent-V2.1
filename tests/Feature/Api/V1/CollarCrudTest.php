<?php

namespace Tests\Feature\Api\V1;

use App\Models\Collar;
use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * Full CRUD tests for the Collar resource, including project-scoping
 * security checks.
 *
 * IMPORTANT: After the A2-02 IDOR fix, CollarController requires the
 * authenticated user to have a pivot row for the parent project.
 * Tests that act as $this->user against $this->project must attach the pivot
 * in setUp (done below). Tests that act against a *different* project must
 * either attach or expect a 404.
 */
class CollarCrudTest extends TestCase
{
    use RefreshDatabase;

    private User $user;
    private Project $project;

    protected function setUp(): void
    {
        parent::setUp();

        // CollarController uses PostGIS ST_X/ST_Transform in selectRaw — SQLite
        // has no geometry column nor spatial functions, so these tests can only
        // run against a real Postgres test connection.
        $this->skipIfSqlite();

        Project::getModel()->setTable('projects');
        Collar::getModel()->setTable('collars');
        $this->user = User::factory()->create();
        $this->project = Project::factory()->create();

        // Attach the user to the project so the hasProjectAccess gate (A2-02 fix)
        // passes for all tests that operate against $this->project.
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);
    }

    public function test_create_collar_with_valid_data(): void
    {
        $response = $this->actingAs($this->user)
            ->postJson("/api/v1/projects/{$this->project->project_id}/collars", [
                'hole_id'     => 'DH-TEST-001',
                'easting'     => 512345.0,
                'northing'    => 6234567.0,
                'elevation'   => 450.0,
                'total_depth' => 350.0,
                'hole_type'   => 'Diamond',
                'azimuth'     => 135.0,
                'dip'         => -60.0,
                'status'      => 'Completed',
            ]);

        $response->assertCreated()
            ->assertJsonPath('data.hole_id', 'DH-TEST-001');
    }

    public function test_list_collars_scoped_to_project(): void
    {
        $otherProject = Project::factory()->create();

        // Create collar in our project
        Collar::factory()->create([
            'project_id' => $this->project->project_id,
            'hole_id'    => 'DH-OURS-001',
        ]);

        // Create collar in other project
        Collar::factory()->create([
            'project_id' => $otherProject->project_id,
            'hole_id'    => 'DH-OTHER-001',
        ]);

        $response = $this->actingAs($this->user)
            ->getJson("/api/v1/projects/{$this->project->project_id}/collars");

        $response->assertOk();

        $holeIds = collect($response->json('data'))->pluck('hole_id')->toArray();
        $this->assertContains('DH-OURS-001', $holeIds);
        $this->assertNotContains('DH-OTHER-001', $holeIds);
    }

    public function test_show_single_collar(): void
    {
        $collar = Collar::factory()->create([
            'project_id' => $this->project->project_id,
            'hole_id'    => 'DH-SHOW-001',
        ]);

        $this->actingAs($this->user)
            ->getJson("/api/v1/projects/{$this->project->project_id}/collars/{$collar->collar_id}")
            ->assertOk()
            ->assertJsonPath('data.hole_id', 'DH-SHOW-001');
    }

    public function test_delete_collar(): void
    {
        $collar = Collar::factory()->create([
            'project_id' => $this->project->project_id,
        ]);

        $this->actingAs($this->user)
            ->deleteJson("/api/v1/projects/{$this->project->project_id}/collars/{$collar->collar_id}")
            ->assertNoContent();

        $this->assertDatabaseMissing('collars', [
            'collar_id' => $collar->collar_id,
        ]);
    }

    public function test_show_collar_from_wrong_project_returns_404(): void
    {
        $otherProject = Project::factory()->create();
        $collar = Collar::factory()->create([
            'project_id' => $otherProject->project_id,
        ]);

        // The user has no pivot for otherProject, so the membership gate fires
        // before the collar lookup and returns 404.
        $this->actingAs($this->user)
            ->getJson("/api/v1/projects/{$otherProject->project_id}/collars/{$collar->collar_id}")
            ->assertNotFound();
    }

    public function test_delete_collar_from_wrong_project_returns_404(): void
    {
        $otherProject = Project::factory()->create();
        $collar = Collar::factory()->create([
            'project_id' => $otherProject->project_id,
        ]);

        // The user has no pivot for otherProject, so the membership gate fires
        // before the collar lookup and returns 404.
        $this->actingAs($this->user)
            ->deleteJson("/api/v1/projects/{$otherProject->project_id}/collars/{$collar->collar_id}")
            ->assertNotFound();

        // Collar should still exist
        $this->assertDatabaseHas('collars', [
            'collar_id' => $collar->collar_id,
        ]);
    }

    public function test_collars_return_401_without_auth(): void
    {
        $this->getJson("/api/v1/projects/{$this->project->project_id}/collars")
            ->assertUnauthorized();
    }
}
