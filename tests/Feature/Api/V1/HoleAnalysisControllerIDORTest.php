<?php

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * Module 10 Chunk 10.3 — IDOR regression tests for HoleAnalysisController.
 *
 * Route under test:
 *   GET /api/v1/projects/{projectId}/holes/{holeIdOrCollarId}/analysis
 *
 * The controller gates via:
 *   $userProjectIds = $request->user()->projects()->pluck('silver.projects.project_id');
 *   if (!$userProjectIds->contains($projectId)) { return 404; }
 *
 * A 404 (not 403) is the correct denial code here — existence-oracle defence is
 * maintained because the same 404 is returned whether the project doesn't exist
 * or the user isn't a member.
 *
 * Note: The analysis payload queries silver.surveys, silver.structures, and
 * silver.geochemistry via PostGIS-capable ST_Transform. These tests only need
 * to verify the project-membership gate (which fires before any DB lookup), so
 * they are safe to run under SQLite.
 */
class HoleAnalysisControllerIDORTest extends TestCase
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

        // projectB belongs exclusively to userB.
        $this->projectB = Project::factory()->create();
        $this->userB->projects()->attach($this->projectB->project_id, ['role' => 'owner']);

        $this->actingAs($this->userA, 'sanctum');
    }

    // -------------------------------------------------------------------------
    // IDOR: read analysis for a hole in another user's project → 404
    // -------------------------------------------------------------------------

    public function test_user_a_cannot_read_hole_analysis_in_user_b_project_by_hole_id(): void
    {
        $response = $this->getJson(
            "/api/v1/projects/{$this->projectB->project_id}/holes/DH-0001/analysis",
        );

        // Gate fires before any DB lookup — returns project_not_found 404.
        $response->assertNotFound()
            ->assertJsonPath('error', 'project_not_found');
    }

    public function test_user_a_cannot_read_hole_analysis_in_user_b_project_by_uuid(): void
    {
        $collarUuid = '00000000-1234-0000-0000-000000000099';

        $response = $this->getJson(
            "/api/v1/projects/{$this->projectB->project_id}/holes/{$collarUuid}/analysis",
        );

        $response->assertNotFound()
            ->assertJsonPath('error', 'project_not_found');
    }

    // -------------------------------------------------------------------------
    // Existence oracle: non-existent project → same 404 shape
    // -------------------------------------------------------------------------

    public function test_nonexistent_project_returns_same_404_shape(): void
    {
        $nonExistentProject = '00000000-0000-0000-0000-000000000000';

        $deniedResponse = $this->getJson(
            "/api/v1/projects/{$this->projectB->project_id}/holes/HOLE-1/analysis",
        );
        $notFoundResponse = $this->getJson(
            "/api/v1/projects/{$nonExistentProject}/holes/HOLE-1/analysis",
        );

        $deniedResponse->assertNotFound();
        $notFoundResponse->assertNotFound();

        // Both must carry the same error key (shape identical — no oracle).
        $this->assertSame(
            $notFoundResponse->json('error'),
            $deniedResponse->json('error'),
        );
    }

    // -------------------------------------------------------------------------
    // Sanity: user A can call analysis on a project they own
    // (returns 404 for the hole, not 404 for the project)
    // -------------------------------------------------------------------------

    public function test_user_a_can_reach_analysis_endpoint_on_own_project(): void
    {
        $this->skipIfSqlite('Analysis endpoint queries PostGIS-backed tables.');

        $projectA = Project::factory()->create();
        $this->userA->projects()->attach($projectA->project_id, ['role' => 'owner']);

        $response = $this->getJson(
            "/api/v1/projects/{$projectA->project_id}/holes/NONEXISTENT-9999/analysis",
        );

        // Project gate passes; hole lookup fails — hole_not_found, not project_not_found.
        $response->assertNotFound()
            ->assertJsonPath('error', 'hole_not_found');
    }
}
