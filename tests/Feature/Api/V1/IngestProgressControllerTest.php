<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Str;
use Laravel\Sanctum\Sanctum;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * IngestProgressController — per-run polling endpoint feature tests.
 *
 * Locks the spec's T11 contract: cross-workspace run_ids return 404, not
 * 403 — so an attacker can't fingerprint which runs exist outside their
 * own workspace.
 *
 * Postgres-only: silver.ingest_progress + silver.projects. Run with:
 *   php artisan test -c phpunit.pgsql.xml --filter=IngestProgressControllerTest
 */
final class IngestProgressControllerTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private User $user;

    private Project $project;

    private string $workspaceId;

    protected function setUp(): void
    {
        parent::setUp();

        $this->user = User::factory()->create();
        $this->workspaceId = (string) Str::uuid();
        $slug = 'ipc-'.substr($this->workspaceId, 0, 8);

        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$this->workspaceId, 'Ingest Progress Controller', $slug],
        );

        $this->project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $this->project->project_id],
        );
        $this->user->projects()->syncWithoutDetaching([
            $this->project->project_id => ['role' => 'owner'],
        ]);
    }

    private function insertRun(string $projectId, string $status = 'started'): string
    {
        $runId = (string) Str::uuid();
        DB::table('silver.ingest_progress')->insert([
            'progress_id' => $runId,
            'run_id' => $runId,
            'workspace_id' => $this->workspaceId,
            'project_id' => $projectId,
            'minio_key' => 'reports/'.$projectId.'/test.pdf',
            'filename' => 'test.pdf',
            'current_step' => 'parse',
            'current_stage' => 'parse',
            'step_index' => 2,
            'total_steps' => 5,
            'status' => $status,
            'attempt_number' => 1,
            'triggered_by' => 'upload',
            'started_at' => now(),
            'updated_at' => now(),
        ]);

        return $runId;
    }

    public function test_returns_run_for_authenticated_user_with_access(): void
    {
        Sanctum::actingAs($this->user);
        $runId = $this->insertRun($this->project->project_id);

        $response = $this->getJson("/api/v1/ingest-progress/{$runId}");

        $response->assertOk()
            ->assertJsonStructure([
                'run_id', 'project_id', 'minio_key', 'filename',
                'status', 'current_stage', 'current_step',
                'step_index', 'total_steps', 'attempt_number',
                'started_at', 'completed_at', 'failed_at',
                'error', 'report_id',
            ])
            ->assertJson([
                'run_id' => $runId,
                'project_id' => $this->project->project_id,
                'status' => 'started',
                'current_stage' => 'parse',
            ]);
    }

    /** T11 — cross-workspace access returns 404, not 403. */
    public function test_cross_workspace_run_returns_404_not_403(): void
    {
        // Create a separate workspace + project the test user has NO access to.
        $otherWorkspaceId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())',
            [$otherWorkspaceId, 'Other Workspace', 'other-'.substr($otherWorkspaceId, 0, 8)],
        );
        $otherProject = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$otherWorkspaceId, $otherProject->project_id],
        );

        // Insert a run owned by the other workspace.
        $otherRunId = (string) Str::uuid();
        DB::table('silver.ingest_progress')->insert([
            'progress_id' => $otherRunId,
            'run_id' => $otherRunId,
            'workspace_id' => $otherWorkspaceId,
            'project_id' => $otherProject->project_id,
            'minio_key' => 'reports/'.$otherProject->project_id.'/secret.pdf',
            'filename' => 'secret.pdf',
            'current_step' => 'parse',
            'current_stage' => 'parse',
            'step_index' => 2,
            'total_steps' => 5,
            'status' => 'started',
            'attempt_number' => 1,
            'triggered_by' => 'upload',
            'started_at' => now(),
            'updated_at' => now(),
        ]);

        Sanctum::actingAs($this->user);

        $response = $this->getJson("/api/v1/ingest-progress/{$otherRunId}");

        // T11 — must be 404, not 403. Even confirming the run exists is
        // an info leak (workspace fingerprinting).
        $response->assertNotFound();
    }

    public function test_unknown_run_id_returns_404(): void
    {
        Sanctum::actingAs($this->user);
        $unknown = (string) Str::uuid();

        $this->getJson("/api/v1/ingest-progress/{$unknown}")->assertNotFound();
    }

    public function test_malformed_run_id_returns_404(): void
    {
        Sanctum::actingAs($this->user);

        $this->getJson('/api/v1/ingest-progress/not-a-uuid')->assertNotFound();
    }

    public function test_unauthenticated_request_is_rejected(): void
    {
        $runId = $this->insertRun($this->project->project_id);

        $this->getJson("/api/v1/ingest-progress/{$runId}")->assertUnauthorized();
    }
}
