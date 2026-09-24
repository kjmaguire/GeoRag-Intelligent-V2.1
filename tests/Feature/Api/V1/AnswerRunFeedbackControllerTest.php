<?php

declare(strict_types=1);

namespace Tests\Feature\Api\V1;

use App\Models\Project;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Http\Client\ConnectionException;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Http;
use Illuminate\Support\Str;
use Tests\Concerns\RequiresPostgres;
use Tests\TestCase;

/**
 * Feature tests for POST /api/v1/answer-runs/{id}/feedback
 * (AnswerRunFeedbackController — built 2026-09-24, §10p).
 *
 * silver.answer_runs and silver.message_feedback are both raw-SQL
 * Postgres-only migrations (UUID / TIMESTAMPTZ) — see
 * ProjectControllerDeleteOrderingTest for the same RequiresPostgres +
 * manual DB::statement seeding pattern this test follows.
 */
final class AnswerRunFeedbackControllerTest extends TestCase
{
    use RefreshDatabase;
    use RequiresPostgres;

    private User $user;

    private Project $project;

    private string $workspaceId;

    private string $answerRunId;

    protected function setUp(): void
    {
        parent::setUp();

        config(['services.fastapi.service_key' => 'test-service-key-must-be-at-least-32-bytes-long']);

        $this->user = User::factory()->create();
        $this->workspaceId = (string) Str::uuid();
        $slug = 'feedback-test-'.substr($this->workspaceId, 0, 8);

        DB::statement(
            'INSERT INTO silver.workspaces (workspace_id, name, slug, created_at, updated_at)
             VALUES (?::uuid, ?, ?, NOW(), NOW())
             ON CONFLICT (workspace_id) DO NOTHING',
            [$this->workspaceId, 'Feedback Test Workspace', $slug],
        );

        $this->project = Project::factory()->create();
        DB::statement(
            'UPDATE silver.projects SET workspace_id = ?::uuid WHERE project_id = ?::uuid',
            [$this->workspaceId, $this->project->project_id],
        );
        $this->user->projects()->syncWithoutDetaching([
            $this->project->project_id => ['role' => 'owner'],
        ]);

        $this->answerRunId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.answer_runs
                (answer_run_id, workspace_id, project_id, query_text, query_class,
                 workspace_data_version_at_query)
             VALUES (?::uuid, ?::uuid, ?::uuid, ?, ?, ?)',
            [$this->answerRunId, $this->workspaceId, $this->project->project_id, 'How many drill holes?', 'factual', 1],
        );
    }

    private function feedbackUrl(?string $answerRunId = null): string
    {
        return '/api/v1/answer-runs/'.($answerRunId ?? $this->answerRunId).'/feedback';
    }

    public function test_unauthenticated_request_is_rejected(): void
    {
        $response = $this->postJson($this->feedbackUrl(), ['polarity' => 'up']);

        $response->assertStatus(401);
    }

    public function test_thumbs_up_proxies_to_fastapi_and_returns_201(): void
    {
        Http::fake([
            '*/v1/answer_runs/*/feedback' => Http::response([
                'feedback_id' => (string) Str::uuid(),
                'answer_run_id' => $this->answerRunId,
                'workspace_id' => $this->workspaceId,
                'user_id' => $this->user->id,
                'polarity' => 'up',
                'category' => null,
                'note' => null,
                'created_at' => now()->toIso8601String(),
            ], 201),
        ]);

        $response = $this->actingAs($this->user, 'sanctum')
            ->postJson($this->feedbackUrl(), ['polarity' => 'up']);

        $response->assertStatus(201);
        $response->assertJsonPath('polarity', 'up');

        Http::assertSent(function ($request) {
            return str_contains($request->url(), '/v1/answer_runs/'.$this->answerRunId.'/feedback')
                && $request->hasHeader('Authorization')
                && $request->hasHeader('X-Service-Key')
                && $request['polarity'] === 'up'
                && $request['category'] === null;
        });
    }

    public function test_thumbs_down_requires_a_category(): void
    {
        $response = $this->actingAs($this->user, 'sanctum')
            ->postJson($this->feedbackUrl(), ['polarity' => 'down']);

        $response->assertStatus(422);
        $response->assertJsonValidationErrors('category');
    }

    public function test_thumbs_down_rejects_an_unknown_category(): void
    {
        $response = $this->actingAs($this->user, 'sanctum')
            ->postJson($this->feedbackUrl(), ['polarity' => 'down', 'category' => 'not_a_real_category']);

        $response->assertStatus(422);
        $response->assertJsonValidationErrors('category');
    }

    public function test_thumbs_down_with_valid_category_and_note_proxies_to_fastapi(): void
    {
        Http::fake([
            '*/v1/answer_runs/*/feedback' => Http::response([
                'feedback_id' => (string) Str::uuid(),
                'answer_run_id' => $this->answerRunId,
                'workspace_id' => $this->workspaceId,
                'user_id' => $this->user->id,
                'polarity' => 'down',
                'category' => 'citation_issue',
                'note' => 'Wrong source cited.',
                'created_at' => now()->toIso8601String(),
            ], 201),
        ]);

        $response = $this->actingAs($this->user, 'sanctum')->postJson($this->feedbackUrl(), [
            'polarity' => 'down',
            'category' => 'citation_issue',
            'note' => 'Wrong source cited.',
        ]);

        $response->assertStatus(201);
        $response->assertJsonPath('category', 'citation_issue');
    }

    public function test_note_over_2000_characters_is_rejected(): void
    {
        $response = $this->actingAs($this->user, 'sanctum')->postJson($this->feedbackUrl(), [
            'polarity' => 'up',
            'note' => str_repeat('x', 2001),
        ]);

        $response->assertStatus(422);
        $response->assertJsonValidationErrors('note');
    }

    public function test_invalid_polarity_is_rejected(): void
    {
        $response = $this->actingAs($this->user, 'sanctum')
            ->postJson($this->feedbackUrl(), ['polarity' => 'sideways']);

        $response->assertStatus(422);
        $response->assertJsonValidationErrors('polarity');
    }

    public function test_answer_run_from_a_project_the_user_cannot_access_returns_404(): void
    {
        // A second workspace/project/answer_run the acting user has no
        // membership on — same cross-tenant shape as
        // CitationFeedbackController's and EvidenceController's tests.
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
        $otherAnswerRunId = (string) Str::uuid();
        DB::statement(
            'INSERT INTO silver.answer_runs
                (answer_run_id, workspace_id, project_id, query_text, query_class,
                 workspace_data_version_at_query)
             VALUES (?::uuid, ?::uuid, ?::uuid, ?, ?, ?)',
            [$otherAnswerRunId, $otherWorkspaceId, $otherProject->project_id, 'Other project query', 'factual', 1],
        );

        $response = $this->actingAs($this->user, 'sanctum')
            ->postJson($this->feedbackUrl($otherAnswerRunId), ['polarity' => 'up']);

        $response->assertStatus(404);
    }

    public function test_nonexistent_answer_run_returns_404(): void
    {
        $response = $this->actingAs($this->user, 'sanctum')
            ->postJson($this->feedbackUrl((string) Str::uuid()), ['polarity' => 'up']);

        $response->assertStatus(404);
    }

    public function test_fastapi_non_2xx_is_proxied(): void
    {
        Http::fake([
            '*/v1/answer_runs/*/feedback' => Http::response(['detail' => 'feedback_constraint_violation'], 400),
        ]);

        $response = $this->actingAs($this->user, 'sanctum')
            ->postJson($this->feedbackUrl(), ['polarity' => 'up']);

        $response->assertStatus(400);
    }

    public function test_fastapi_unreachable_returns_502(): void
    {
        Http::fake(function () {
            throw new ConnectionException('connection refused');
        });

        $response = $this->actingAs($this->user, 'sanctum')
            ->postJson($this->feedbackUrl(), ['polarity' => 'up']);

        $response->assertStatus(502);
    }
}
