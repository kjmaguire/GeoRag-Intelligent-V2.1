<?php

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * IDOR regression tests for ProjectController::show / update / destroy.
 *
 * Verifies finding A2-01 from the 2026-04-22 security audit:
 *   "show/update/destroy use findOrFail($projectId) with no membership gate.
 *    Any authenticated user can read/update/delete any project by UUID."
 *
 * Pattern: User A authenticates, then attempts operations against a project
 * that belongs exclusively to User B (no pivot row for A). All must return
 * 404, not 200/204/403, so we do not leak UUID existence.
 *
 * SQLite compatibility: these tests do not exercise PostGIS functions, so
 * they run under the default SQLite in-memory driver.
 */
class ProjectControllerIDORTest extends TestCase
{
    use RefreshDatabase;

    private User $userA;
    private User $userB;
    private Project $projectB;

    protected function setUp(): void
    {
        parent::setUp();

        Project::getModel()->setTable('projects');

        $this->userA = User::factory()->create();
        $this->userB = User::factory()->create();

        // projectB is owned by userB only — no pivot row for userA.
        $this->projectB = Project::factory()->create();
        $this->userB->projects()->attach($this->projectB->project_id, ['role' => 'owner']);

        $this->actingAs($this->userA);
    }

    // -------------------------------------------------------------------------
    // IDOR: cross-tenant reads (should return 404, not data)
    // -------------------------------------------------------------------------

    public function test_user_a_cannot_read_user_b_project(): void
    {
        $response = $this->getJson("/api/v1/projects/{$this->projectB->project_id}");

        $response->assertNotFound()
            ->assertJsonPath('message', 'Project not found.');
    }

    public function test_user_a_cannot_update_user_b_project(): void
    {
        $response = $this->patchJson("/api/v1/projects/{$this->projectB->project_id}", [
            'project_name' => 'Hijacked Name',
        ]);

        $response->assertNotFound()
            ->assertJsonPath('message', 'Project not found.');

        // Confirm the project was NOT modified.
        $this->assertDatabaseMissing('projects', ['project_name' => 'Hijacked Name']);
    }

    public function test_user_a_cannot_delete_user_b_project(): void
    {
        $response = $this->deleteJson("/api/v1/projects/{$this->projectB->project_id}");

        $response->assertNotFound()
            ->assertJsonPath('message', 'Project not found.');

        // Confirm the project was NOT deleted.
        $this->assertDatabaseHas('projects', ['project_id' => $this->projectB->project_id]);
    }

    // -------------------------------------------------------------------------
    // Sanity: user A can access their own project
    // -------------------------------------------------------------------------

    public function test_user_a_can_read_own_project(): void
    {
        $projectA = Project::factory()->create();
        $this->userA->projects()->attach($projectA->project_id, ['role' => 'owner']);

        $response = $this->getJson("/api/v1/projects/{$projectA->project_id}");

        $response->assertOk()
            ->assertJsonPath('data.project_id', $projectA->project_id);
    }

    // -------------------------------------------------------------------------
    // Existence oracle: 404 response shape must match ModelNotFoundException
    // -------------------------------------------------------------------------

    public function test_idor_deny_response_shape_matches_not_found(): void
    {
        // The 404 from access denial must be indistinguishable from the 404
        // returned when a project simply doesn't exist.
        $nonExistentUuid = '00000000-0000-0000-0000-000000000000';

        $deniedResponse  = $this->getJson("/api/v1/projects/{$this->projectB->project_id}");
        $notFoundResponse = $this->getJson("/api/v1/projects/{$nonExistentUuid}");

        // Both must be 404.
        $deniedResponse->assertNotFound();
        $notFoundResponse->assertNotFound();

        // Both must carry the same message key (shape identical — no oracle).
        $this->assertSame(
            $notFoundResponse->json('message'),
            $deniedResponse->json('message'),
        );
    }
}
