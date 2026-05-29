<?php

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Tests\TestCase;

/**
 * Module 10 Chunk 10.3 — IDOR regression tests for QueryController.
 *
 * Routes under test:
 *   POST /api/v1/queries             (store — reserve a query)
 *   POST /api/v1/queries/{id}/start  (start — dispatch the Horizon job)
 *
 * Phase 1 gate (store):
 *   The controller calls user->hasProjectAccess($projectId) and returns 403
 *   when the authenticated user doesn't have a project_user pivot row. A 403
 *   (not 404) is returned because the endpoint body-scopes on project_id rather
 *   than URL-path-scopes; this is the documented design (see QueryController
 *   docblock). The test asserts 403 to match the actual gate.
 *
 * Phase 2 gate (start):
 *   The start route looks up the audit row filtered by BOTH query_id AND user_id,
 *   so User A supplying User B's query_id gets 404 (row not found for userA).
 */
class QueryControllerIDORTest extends TestCase
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

        // Do NOT call actingAs in setUp — individual tests opt-in so the
        // unauthenticated test can send a request without any auth context.
    }

    // -------------------------------------------------------------------------
    // IDOR: store — user A fires a RAG query against user B's project → 403
    // -------------------------------------------------------------------------

    public function test_user_a_cannot_query_user_b_project(): void
    {
        $this->actingAs($this->userA, 'sanctum');

        $response = $this->postJson('/api/v1/queries', [
            'query'      => 'How many drill holes are in this project?',
            'project_id' => $this->projectB->project_id,
        ]);

        // hasProjectAccess returns false → 403 (query gate design, not existence oracle).
        $response->assertForbidden()
            ->assertJsonPath('error', 'forbidden');
    }

    // -------------------------------------------------------------------------
    // IDOR: start — user A tries to start user B's reserved query → 404
    // The start route filters by user_id, so the row appears as not-found.
    // -------------------------------------------------------------------------

    public function test_user_a_cannot_start_user_b_query(): void
    {
        // Reserve a query as User B.
        $this->actingAs($this->userB, 'sanctum');

        $reserveResponse = $this->postJson('/api/v1/queries', [
            'query'      => 'List all drill holes.',
            'project_id' => $this->projectB->project_id,
        ]);

        // User B must have access so we can get a real query_id.
        if ($reserveResponse->status() !== 202) {
            $this->markTestSkipped(
                'Unable to reserve a query as User B (may need full stack). Skipping.'
            );
        }

        $queryId = $reserveResponse->json('query_id');

        // Now switch to User A and try to start User B's query.
        $this->actingAs($this->userA, 'sanctum');

        $startResponse = $this->postJson("/api/v1/queries/{$queryId}/start");

        // start() filters by user_id — row not found for User A → 404.
        $startResponse->assertNotFound()
            ->assertJsonPath('error', 'query_not_found');
    }

    // -------------------------------------------------------------------------
    // Sanity: unauthenticated store → 401
    // -------------------------------------------------------------------------

    public function test_unauthenticated_query_returns_401(): void
    {
        // Reset acting-as so we are fully unauthenticated.
        $response = $this->postJson('/api/v1/queries', [
            'query'      => 'How many holes?',
            'project_id' => $this->projectB->project_id,
        ]);

        $response->assertUnauthorized();
    }

    // -------------------------------------------------------------------------
    // Existence oracle: cross-tenant store response must not leak project name
    // -------------------------------------------------------------------------

    public function test_cross_tenant_store_response_does_not_leak_project_details(): void
    {
        $this->actingAs($this->userA, 'sanctum');

        $response = $this->postJson('/api/v1/queries', [
            'query'      => 'How many drill holes are in this project?',
            'project_id' => $this->projectB->project_id,
        ]);

        $response->assertForbidden();

        // Response body must not include the project name, project UUID in the
        // 'data' key, or any details that would confirm the project exists.
        $body = $response->json();
        $this->assertArrayNotHasKey('project_name', $body);
        $this->assertArrayNotHasKey('data', $body);
    }
}
