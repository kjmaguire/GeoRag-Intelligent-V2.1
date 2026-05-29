<?php

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\UploadedFile;
use Illuminate\Support\Facades\Storage;
use Tests\TestCase;

/**
 * Module 10 Chunk 10.3 — IDOR regression tests for UploadController.
 *
 * Routes under test:
 *   POST /api/v1/projects/{project}/upload   (store)
 *   GET  /api/v1/upload/categories           (categories — no IDOR surface)
 *
 * The store action gates via user->hasProjectAccess($projectId) and returns 403
 * (not 404) when the user has no membership. This is the documented design: the
 * endpoint is about file upload into a project bucket, not a project lookup, so
 * the denial is "forbidden access" rather than "project not found".
 *
 * The categories endpoint is unauthenticated-adjacent (no project-scope) and has
 * no IDOR surface — any authenticated user may call it.
 *
 * SQLite safety: the membership gate fires before the Storage call, so the 403
 * path is safe under SQLite. The fake Storage::disk('s3') is faked via the
 * Storage::fake() call below to prevent real S3 calls in any environment.
 */
class UploadControllerIDORTest extends TestCase
{
    use RefreshDatabase;

    private User $userA;
    private User $userB;
    private Project $projectB;

    protected function setUp(): void
    {
        parent::setUp();

        Storage::fake('s3');

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
    // IDOR: store — user A uploads a file to user B's project → 403
    // -------------------------------------------------------------------------

    public function test_user_a_cannot_upload_to_user_b_project(): void
    {
        $this->actingAs($this->userA, 'sanctum');

        $file = UploadedFile::fake()->create('collars.csv', 10, 'text/csv');

        $response = $this->postJson(
            "/api/v1/projects/{$this->projectB->project_id}/upload",
            [
                'file'     => $file,
                'category' => 'collars',
            ]
        );

        // hasProjectAccess returns false → 403.
        $response->assertForbidden()
            ->assertJsonPath('error', 'forbidden');

        // Confirm nothing was written to the fake disk (no files at all on this disk).
        $this->assertEmpty(
            Storage::disk('s3')->allFiles(),
            'No file should have been uploaded to the s3 bucket.'
        );
    }

    // -------------------------------------------------------------------------
    // IDOR: cross-tenant upload response must not confirm project existence
    // -------------------------------------------------------------------------

    public function test_cross_tenant_upload_does_not_leak_project_details(): void
    {
        $this->actingAs($this->userA, 'sanctum');

        $file = UploadedFile::fake()->create('surveys.csv', 5, 'text/csv');

        $response = $this->postJson(
            "/api/v1/projects/{$this->projectB->project_id}/upload",
            [
                'file'     => $file,
                'category' => 'surveys',
            ]
        );

        $response->assertForbidden();

        // Body must not include any project-identifying fields.
        $body = $response->json();
        $this->assertArrayNotHasKey('project_id', $body);
        $this->assertArrayNotHasKey('project_name', $body);
    }

    // -------------------------------------------------------------------------
    // Sanity: categories endpoint is accessible to any authenticated user
    // -------------------------------------------------------------------------

    public function test_categories_returns_200_for_authenticated_user(): void
    {
        $this->actingAs($this->userA, 'sanctum');

        $response = $this->getJson('/api/v1/upload/categories');

        $response->assertOk()
            ->assertJsonStructure(['categories']);
    }

    // -------------------------------------------------------------------------
    // Auth gate: unauthenticated upload → 401
    // -------------------------------------------------------------------------

    public function test_unauthenticated_upload_returns_401(): void
    {
        $file = UploadedFile::fake()->create('data.csv', 10, 'text/csv');

        // Issue the request without acting as any user.
        $response = $this->postJson(
            "/api/v1/projects/{$this->projectB->project_id}/upload",
            [
                'file'     => $file,
                'category' => 'collars',
            ]
        );

        $response->assertUnauthorized();
    }

    // -------------------------------------------------------------------------
    // Sanity: user A can upload to their own project (gate passes)
    // -------------------------------------------------------------------------

    public function test_user_a_can_upload_to_own_project(): void
    {
        $this->actingAs($this->userA, 'sanctum');

        $projectA = Project::factory()->create();
        $this->userA->projects()->attach($projectA->project_id, ['role' => 'owner']);

        $file = UploadedFile::fake()->create('collars.csv', 10, 'text/csv');

        $response = $this->postJson(
            "/api/v1/projects/{$projectA->project_id}/upload",
            [
                'file'     => $file,
                'category' => 'collars',
            ]
        );

        // Gate passes; actual S3 upload is faked so it always succeeds.
        $response->assertCreated();
    }
}
