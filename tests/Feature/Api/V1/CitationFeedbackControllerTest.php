<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * POST /api/v1/citations/feedback (CitationFeedbackController).
 *
 * Database audit 2026-09-29 PG-7: FastAPI answers this proxy with 201
 * Created, and the controller checked `$response->ok()`, which is true for
 * 200 only — so a successful write came back to the browser as a 502.
 *
 * Postgres-only because the tenancy gate reads silver.answer_runs.
 */
final class CitationFeedbackControllerTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private User $user;

    private string $workspaceId;

    private string $answerRunId;

    protected function setUp(): void
    {
        parent::setUp();

        config([
            'services.fastapi.service_key' => 'test-service-key-must-be-at-least-32-bytes-long',
            'services.fastapi.internal_url' => 'http://fastapi.test',
        ]);

        $this->user = User::factory()->create();
        $this->workspaceId = (string) Str::uuid();

        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())',
            [$this->workspaceId, 'Citation Feedback Workspace', 'cite-fb-'.substr($this->workspaceId, 0, 8)],
        );

        $project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $project->project_id],
        );
        $this->user->projects()->syncWithoutDetaching([$project->project_id => ['role' => 'owner']]);

        $this->answerRunId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.answer_runs
                (answer_run_id, workspace_id, project_id, query_text, query_class,
                 workspace_data_version_at_query)
             VALUES (?::uuid, ?::uuid, ?::uuid, ?, ?, ?)',
            [$this->answerRunId, $this->workspaceId, $project->project_id, 'Deepest hole?', 'factual', 1],
        );
    }

    /**
     * @return array<string, string>
     */
    private function payload(): array
    {
        return [
            'workspace_id' => $this->workspaceId,
            'answer_run_id' => $this->answerRunId,
            'citation_item_id' => (string) Str::uuid(),
            'source_document_id' => (string) Str::uuid(),
            'verdict' => 'right',
        ];
    }

    public function test_fastapi_201_is_passed_through_as_201(): void
    {
        $body = [
            'feature_id' => (string) Str::uuid(),
            'workspace_id' => $this->workspaceId,
            'source_document_id' => (string) Str::uuid(),
            'verdict' => 'right',
            'recorded_at' => now()->toIso8601String(),
            'cumulative_feedback_for_source' => 1,
        ];
        Http::fake(['fastapi.test/*' => Http::response($body, 201)]);

        $this->actingAs($this->user)
            ->postJson('/api/v1/citations/feedback', $this->payload())
            ->assertCreated()
            ->assertJsonPath('cumulative_feedback_for_source', 1);

        Http::assertSent(fn ($request): bool => $request['submitted_by_user_id'] === $this->user->id
            && $request->hasHeader('X-Service-Key'));
    }

    public function test_fastapi_error_is_still_a_502(): void
    {
        Http::fake(['fastapi.test/*' => Http::response(['detail' => 'boom'], 500)]);

        $this->actingAs($this->user)
            ->postJson('/api/v1/citations/feedback', $this->payload())
            ->assertStatus(502)
            ->assertJsonPath('fastapi_status', 500)
            ->assertJsonMissingPath('fastapi_body');
    }

    public function test_an_unreachable_fastapi_does_not_leak_its_address(): void
    {
        Http::fake(['fastapi.test/*' => Http::failedConnection(
            'cURL error 7: Failed to connect to fastapi.internal.georag port 8000',
        )]);

        $response = $this->actingAs($this->user)
            ->postJson('/api/v1/citations/feedback', $this->payload())
            ->assertStatus(502);

        $this->assertStringNotContainsString('fastapi.internal.georag', $response->getContent());
        $this->assertStringNotContainsString('8000', $response->getContent());
    }
}
