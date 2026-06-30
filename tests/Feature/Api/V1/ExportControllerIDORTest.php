<?php

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * Module 10 Chunk 10.3 — IDOR regression tests for ExportController.
 *
 * Routes under test:
 *   GET  /api/v1/projects/{project}/exports           (index)
 *   POST /api/v1/projects/{project}/exports           (store)
 *   GET  /api/v1/projects/{project}/exports/{export}  (show)
 *   GET  /api/v1/exports/{export}/download            (download — standalone)
 *
 * All project-nested routes use denyIfNoProjectAccess() which returns 403 on
 * access denied. The standalone download route first loads the export then calls
 * denyIfNoProjectAccess on its parent project, returning 403. Both 403 and 404
 * are acceptable per the IDOR pattern (403 = forbidden access to known project,
 * 404 = project itself not found per membership check).
 *
 * NOTE: Export tests that need the silver.exports table (PostgreSQL schema) are
 * skipped under SQLite because the `silver.` schema prefix is not available in
 * the SQLite test driver even after the TestCase schema-stripping hook.
 * The membership gate fires before any DB query on nested routes (403 response),
 * so those tests are SQLite-safe.
 */
class ExportControllerIDORTest extends TestCase
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
    // IDOR: index — list exports from another user's project
    // -------------------------------------------------------------------------

    public function test_user_a_cannot_list_exports_of_user_b_project(): void
    {
        $response = $this->getJson("/api/v1/projects/{$this->projectB->project_id}/exports");

        // denyIfNoProjectAccess returns 403 (access-denied guard fires first).
        $response->assertForbidden();
    }

    // -------------------------------------------------------------------------
    // IDOR: store — create an export job in another user's project
    // -------------------------------------------------------------------------

    public function test_user_a_cannot_create_export_in_user_b_project(): void
    {
        $response = $this->postJson("/api/v1/projects/{$this->projectB->project_id}/exports", [
            'export_type' => 'csv_collars',
        ]);

        $response->assertForbidden();
    }

    // -------------------------------------------------------------------------
    // IDOR: show — read a specific export in another user's project
    // -------------------------------------------------------------------------

    public function test_user_a_cannot_read_export_in_user_b_project(): void
    {
        $this->skipIfSqlite('silver.exports table requires PostgreSQL schema support.');

        $exportId = $this->seedExport($this->projectB->project_id);

        $response = $this->getJson(
            "/api/v1/projects/{$this->projectB->project_id}/exports/{$exportId}",
        );

        // Gate fires before the model lookup — returns 403.
        $response->assertForbidden();
    }

    // -------------------------------------------------------------------------
    // IDOR: download — download an export belonging to another user's project
    // The download route is not project-nested (clients can bookmark it), but
    // the controller loads the export then checks project membership.
    // -------------------------------------------------------------------------

    public function test_user_a_cannot_download_export_belonging_to_user_b_project(): void
    {
        $this->skipIfSqlite('silver.exports table requires PostgreSQL schema support.');

        $exportId = $this->seedExport($this->projectB->project_id, 'completed');

        $response = $this->getJson("/api/v1/exports/{$exportId}/download");

        // After loading the export, controller calls denyIfNoProjectAccess on its
        // parent project. UserA has no membership → 403.
        $response->assertForbidden();
    }

    // -------------------------------------------------------------------------
    // Download of a non-existent export → 404 (existence oracle)
    // -------------------------------------------------------------------------

    public function test_download_of_nonexistent_export_returns_404(): void
    {
        $this->skipIfSqlite('silver.exports table requires PostgreSQL schema support.');

        $nonExistentId = '00000000-0000-0000-0000-000000000000';

        $response = $this->getJson("/api/v1/exports/{$nonExistentId}/download");

        $response->assertNotFound();
    }

    // -------------------------------------------------------------------------
    // Sanity: user A can list exports on their own project
    // -------------------------------------------------------------------------

    public function test_user_a_can_list_exports_on_own_project(): void
    {
        $projectA = Project::factory()->create();
        $this->userA->projects()->attach($projectA->project_id, ['role' => 'owner']);

        $response = $this->getJson("/api/v1/projects/{$projectA->project_id}/exports");

        $response->assertOk();
    }

    // -------------------------------------------------------------------------
    // Private helpers
    // -------------------------------------------------------------------------

    /**
     * Insert a minimal export row directly into silver.exports and return the
     * export_id UUID. Used in tests that require a real export record (PostgreSQL
     * only — silver schema not available under SQLite).
     */
    private function seedExport(string $projectId, string $status = 'pending'): string
    {
        $exportId = (string) Str::uuid();

        DB::statement(
            "INSERT INTO silver.exports
                 (export_id, project_id, export_type, status, filters, created_at, updated_at)
             VALUES
                 (?, ?, 'csv', ?, '[]', NOW(), NOW())",
            [$exportId, $projectId, $status],
        );

        return $exportId;
    }
}
