<?php

namespace Tests\Feature\Api\V1;

use App\Models\Collar;
use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Schema;
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
    //
    // Creating a project requires a tenant to create it IN. A brand-new
    // account has no project memberships, so it has no workspace, so it
    // cannot create anything — which is the point: registration used to be
    // open, and store() used to fall back to a hardcoded workspace UUID, so
    // a stranger's second API call put them inside the production tenant.
    // An administrator bootstrapping a fresh deployment is the exception.

    /** Acting user who is allowed to create projects (fresh deployment). */
    private function actingAsAdmin(): User
    {
        $admin = User::factory()->create(['is_admin' => true]);
        $this->actingAs($admin);

        return $admin;
    }

    public function test_store_is_forbidden_for_a_user_with_no_workspace(): void
    {
        // $this->user from setUp() has no project memberships.
        $response = $this->postJson('/api/v1/projects', [
            'project_name' => 'Stranger Danger',
        ]);

        $response->assertForbidden();
        $this->assertDatabaseMissing('projects', ['project_name' => 'Stranger Danger']);
    }

    public function test_store_uses_the_creators_own_workspace(): void
    {
        $existing = Project::factory()->create([
            'workspace_id' => 'b0000000-0000-0000-0000-0000000000ff',
        ]);
        $this->user->projects()->attach($existing->project_id, ['role' => 'owner']);

        $response = $this->postJson('/api/v1/projects', [
            'project_name' => 'Second Property',
            'orientation_reference' => 'BOH',
        ]);

        $response->assertCreated();
        $this->assertDatabaseHas('projects', [
            'project_name' => 'Second Property',
            'workspace_id' => 'b0000000-0000-0000-0000-0000000000ff',
        ]);
    }

    public function test_store_refuses_a_workspace_the_creator_does_not_belong_to(): void
    {
        $mine = Project::factory()->create([
            'workspace_id' => 'b0000000-0000-0000-0000-0000000000ff',
        ]);
        $this->user->projects()->attach($mine->project_id, ['role' => 'owner']);

        $response = $this->postJson('/api/v1/projects', [
            'project_name' => 'Somebody Elses Tenant',
            'workspace_id' => 'a0000000-0000-0000-0000-000000000001',
        ]);

        $response->assertUnprocessable();
        $this->assertDatabaseMissing('projects', ['project_name' => 'Somebody Elses Tenant']);
    }

    public function test_store_is_ambiguous_when_the_creator_belongs_to_several_workspaces(): void
    {
        foreach (['b0000000-0000-0000-0000-0000000000ff', 'c0000000-0000-0000-0000-0000000000ff'] as $ws) {
            $p = Project::factory()->create(['workspace_id' => $ws]);
            $this->user->projects()->attach($p->project_id, ['role' => 'owner']);
        }

        // Picking one would put half this user's work in the wrong tenant.
        $this->postJson('/api/v1/projects', [
            'project_name' => 'Ambiguous',
            'orientation_reference' => 'BOH',
        ])->assertUnprocessable();

        $this->postJson('/api/v1/projects', [
            'project_name' => 'Ambiguous',
            'orientation_reference' => 'BOH',
            'workspace_id' => 'c0000000-0000-0000-0000-0000000000ff',
        ])->assertCreated();
    }

    public function test_store_creates_project_and_returns_201(): void
    {
        $this->actingAsAdmin();

        $payload = [
            'project_name' => 'Goldfields North',
            'crs_datum' => 'EPSG:32654',
            'company' => 'Apex Mining',
            'commodity' => 'Gold',
            'region' => 'Western Australia',
            'magnetic_declination' => -2.5,
            'orientation_reference' => 'BOH',
        ];

        $response = $this->postJson('/api/v1/projects', $payload);

        $response->assertCreated()
            ->assertJsonPath('data.project_name', 'Goldfields North')
            ->assertJsonPath('data.collar_count', 0);

        $this->assertDatabaseHas('projects', ['project_name' => 'Goldfields North']);
    }

    public function test_store_returns_422_when_project_name_is_missing(): void
    {
        $this->actingAsAdmin();

        $response = $this->postJson('/api/v1/projects', [
            'company' => 'Apex Mining',
        ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['project_name']);
    }

    public function test_store_returns_422_when_magnetic_declination_is_out_of_range(): void
    {
        $this->actingAsAdmin();

        $response = $this->postJson('/api/v1/projects', [
            'project_name' => 'Test Project',
            'magnetic_declination' => 999,
        ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['magnetic_declination']);
    }

    public function test_store_returns_422_when_orientation_reference_is_invalid(): void
    {
        $this->actingAsAdmin();

        $response = $this->postJson('/api/v1/projects', [
            'project_name' => 'Test Project',
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

    /**
     * Regression for 2026-08-17: silver.mineral_claims does not exist in the
     * live (canadacentral) database at all — it was only ever created by an
     * out-of-band raw-SQL bootstrap script, never a tracked migration, and
     * the freshly-provisioned Azure Postgres server never got it. destroy()
     * unconditionally ran `DELETE FROM silver.mineral_claims`, which threw a
     * "relation does not exist" error on every call, rolling back the whole
     * transaction — project deletion was 100% broken for every project.
     *
     * The test-DB parity migration (2026_06_29_020000_provision_project_
     * delete_tables_for_test_db.php) always stubs a dummy mineral_claims
     * table, which is why this bug was invisible to the SQLite suite until
     * now — dropping that stub table here reproduces the exact live gap.
     */
    public function test_destroy_succeeds_when_a_listed_cleanup_table_does_not_exist(): void
    {
        Schema::dropIfExists('mineral_claims');

        $project = Project::factory()->create();
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);

        $response = $this->deleteJson("/api/v1/projects/{$project->project_id}");

        $response->assertNoContent();
        $this->assertDatabaseMissing('projects', ['project_id' => $project->project_id]);
    }
}
