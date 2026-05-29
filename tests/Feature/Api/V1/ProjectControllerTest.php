<?php

namespace Tests\Feature\Api\V1;

use App\Models\Collar;
use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * Feature tests for ProjectController.
 *
 * The test suite uses SQLite in-memory (see phpunit.xml). Because the models
 * reference the 'silver' schema prefix and SQLite doesn't support schemas,
 * each test configures the model table names via a shared helper that strips
 * the schema prefix when running under SQLite.
 *
 * Note: tests assert HTTP contracts (status codes, response shape) — they do
 * NOT test geological domain logic, which lives in FastAPI.
 *
 * IMPORTANT: After the A2-01 IDOR fix, show/update/destroy require the
 * authenticated user to have a pivot row in project_user for the target
 * project. Tests that exercise these methods on a project the user owns
 * must call $this->user->projects()->attach(...) so the gate passes.
 */
class ProjectControllerTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    protected function setUp(): void
    {
        parent::setUp();

        // Strip the schema prefix so SQLite can find the table.
        // In a real Postgres test environment this override is unnecessary.
        Project::getModel()->setTable('projects');
        Collar::getModel()->setTable('collars');

        $this->user = User::factory()->create();
        $this->actingAs($this->user);
    }

    // -------------------------------------------------------------------------
    // index
    // -------------------------------------------------------------------------

    public function test_index_returns_paginated_projects(): void
    {
        $projects = Project::factory()->count(3)->create();
        foreach ($projects as $project) {
            $this->user->projects()->attach($project->project_id, ['role' => 'owner']);
        }

        $response = $this->getJson('/api/v1/projects');

        $response->assertOk()
            ->assertJsonStructure([
                'data' => [
                    '*' => [
                        'project_id',
                        'project_name',
                        'collar_count',
                        'created_at',
                        'updated_at',
                    ],
                ],
                'meta' => ['current_page', 'total'],
            ]);
    }

    public function test_index_returns_empty_list_when_no_projects_exist(): void
    {
        $response = $this->getJson('/api/v1/projects');

        $response->assertOk()
            ->assertJson(['data' => []]);
    }

    // -------------------------------------------------------------------------
    // store
    // -------------------------------------------------------------------------

    public function test_store_creates_project_and_returns_201(): void
    {
        $payload = [
            'project_name'           => 'Goldfields North',
            'crs_datum'              => 'EPSG:32654',
            'company'                => 'Apex Mining',
            'commodity'              => 'Gold',
            'region'                 => 'Western Australia',
            'magnetic_declination'   => -2.5,
            'orientation_reference'  => 'BOH',
        ];

        $response = $this->postJson('/api/v1/projects', $payload);

        $response->assertCreated()
            ->assertJsonPath('data.project_name', 'Goldfields North')
            ->assertJsonPath('data.collar_count', 0);

        $this->assertDatabaseHas('projects', ['project_name' => 'Goldfields North']);
    }

    public function test_store_returns_422_when_project_name_is_missing(): void
    {
        $response = $this->postJson('/api/v1/projects', [
            'company' => 'Apex Mining',
        ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['project_name']);
    }

    public function test_store_returns_422_when_magnetic_declination_is_out_of_range(): void
    {
        $response = $this->postJson('/api/v1/projects', [
            'project_name'         => 'Test Project',
            'magnetic_declination' => 999,
        ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['magnetic_declination']);
    }

    public function test_store_returns_422_when_orientation_reference_is_invalid(): void
    {
        $response = $this->postJson('/api/v1/projects', [
            'project_name'          => 'Test Project',
            'orientation_reference' => 'INVALID',
        ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['orientation_reference']);
    }

    // -------------------------------------------------------------------------
    // show
    // -------------------------------------------------------------------------

    public function test_show_returns_project_with_collar_count(): void
    {
        $project = Project::factory()->create(['project_name' => 'Show Test Project']);
        // Attach user so the hasProjectAccess gate passes (A2-01 fix).
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);
        Collar::factory()->count(4)->create(['project_id' => $project->project_id]);

        $response = $this->getJson("/api/v1/projects/{$project->project_id}");

        $response->assertOk()
            ->assertJsonPath('data.project_id', $project->project_id)
            ->assertJsonPath('data.collar_count', 4);
    }

    public function test_show_returns_404_for_nonexistent_project(): void
    {
        $response = $this->getJson('/api/v1/projects/00000000-0000-0000-0000-000000000000');

        $response->assertNotFound();
    }

    // -------------------------------------------------------------------------
    // update
    // -------------------------------------------------------------------------

    public function test_update_modifies_project_and_returns_200(): void
    {
        $project = Project::factory()->create(['project_name' => 'Original Name']);
        // Attach user so the hasProjectAccess gate passes (A2-01 fix).
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);

        $response = $this->patchJson("/api/v1/projects/{$project->project_id}", [
            'project_name' => 'Renamed Project',
        ]);

        $response->assertOk()
            ->assertJsonPath('data.project_name', 'Renamed Project');

        $this->assertDatabaseHas('projects', ['project_name' => 'Renamed Project']);
    }

    public function test_update_returns_404_for_nonexistent_project(): void
    {
        $response = $this->patchJson('/api/v1/projects/00000000-0000-0000-0000-000000000000', [
            'project_name' => 'Ghost Project',
        ]);

        $response->assertNotFound();
    }

    // -------------------------------------------------------------------------
    // destroy
    // -------------------------------------------------------------------------

    public function test_destroy_deletes_project_and_returns_204(): void
    {
        $project = Project::factory()->create();
        // Attach user so the hasProjectAccess gate passes (A2-01 fix).
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);

        $response = $this->deleteJson("/api/v1/projects/{$project->project_id}");

        $response->assertNoContent();
        $this->assertDatabaseMissing('projects', ['project_id' => $project->project_id]);
    }

    public function test_destroy_returns_404_for_nonexistent_project(): void
    {
        $response = $this->deleteJson('/api/v1/projects/00000000-0000-0000-0000-000000000000');

        $response->assertNotFound();
    }
}
