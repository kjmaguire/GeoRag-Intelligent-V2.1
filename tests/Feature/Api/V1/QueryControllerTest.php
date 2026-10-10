<?php

namespace Tests\Feature\Api\V1;

use App\Jobs\StreamQueryFromFastApi;
use App\Models\Project;
use App\Models\QueryAuditLog;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Queue;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * Feature tests for QueryController.
 *
 * The controller dispatches a StreamQueryFromFastApi job and returns 202
 * immediately. These tests verify the HTTP contract — they do not exercise
 * the FastAPI streaming (that is covered by integration tests). FastAPI calls
 * are never made; the queue is faked.
 */
class QueryControllerTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    protected function setUp(): void
    {
        parent::setUp();

        Project::getModel()->setTable('projects');

        $this->user = User::factory()->create();
        $this->actingAs($this->user);
    }

    public function test_store_reserves_query_and_start_dispatches_job(): void
    {
        Queue::fake();

        $project = Project::factory()->create();
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);

        // Phase 1 — reserve: returns 202 + query_id, but must NOT dispatch yet.
        $reserve = $this->postJson('/api/v1/queries', [
            'query' => 'What is the average gold grade in the northern zone?',
            'project_id' => $project->project_id,
        ]);

        $reserve->assertAccepted()
            ->assertJsonStructure(['query_id', 'channel', 'message']);
        $this->assertStringStartsWith('query.', $reserve->json('channel'));
        Queue::assertNotPushed(StreamQueryFromFastApi::class);

        // Phase 2 — start: now the job dispatches.
        $queryId = $reserve->json('query_id');
        $this->postJson("/api/v1/queries/{$queryId}/start")->assertAccepted();
        Queue::assertPushed(StreamQueryFromFastApi::class, 1);
    }

    public function test_store_returns_422_when_query_is_missing(): void
    {
        $project = Project::factory()->create();

        $response = $this->postJson('/api/v1/queries', [
            'project_id' => $project->project_id,
        ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['query']);
    }

    public function test_a_missing_project_answers_like_one_the_caller_cannot_access(): void
    {
        // 422 "does not exist" for a missing project beside 403 for someone
        // else's was an existence oracle over project ids.
        $missing = $this->postJson('/api/v1/queries', [
            'query' => 'What is the average gold grade?',
            'project_id' => '00000000-0000-0000-0000-000000000000',
        ]);

        $foreign = $this->postJson('/api/v1/queries', [
            'query' => 'What is the average gold grade?',
            'project_id' => Project::factory()->create()->project_id,
        ]);

        $missing->assertForbidden();
        $foreign->assertForbidden();
        $this->assertSame($foreign->json(), $missing->json());
    }

    public function test_store_returns_422_when_project_id_is_not_a_uuid(): void
    {
        $response = $this->postJson('/api/v1/queries', [
            'query' => 'Show me the lithology summary.',
            'project_id' => 'not-a-uuid',
        ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['project_id']);
    }

    public function test_store_returns_422_when_query_exceeds_max_length(): void
    {
        $project = Project::factory()->create();

        $response = $this->postJson('/api/v1/queries', [
            'query' => str_repeat('a', 2001),
            'project_id' => $project->project_id,
        ]);

        $response->assertUnprocessable()
            ->assertJsonValidationErrors(['query']);
    }

    public function test_query_id_in_response_matches_channel_suffix(): void
    {
        Queue::fake();

        $project = Project::factory()->create();
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);

        $response = $this->postJson('/api/v1/queries', [
            'query' => 'Summarise drill results for hole DH-001.',
            'project_id' => $project->project_id,
        ]);

        $response->assertAccepted();

        $queryId = $response->json('query_id');
        $channel = $response->json('channel');

        $this->assertSame("query.{$queryId}", $channel);
    }

    public function test_store_populates_the_audit_rows_workspace_id(): void
    {
        Queue::fake();
        $project = Project::factory()->create();
        $workspaceId = (string) Str::uuid();
        DB::table('silver.projects')
            ->where('project_id', $project->project_id)
            ->update(['workspace_id' => $workspaceId]);
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);

        $queryId = $this->postJson('/api/v1/queries', [
            'query' => 'Which holes intersect the PLS zone?',
            'project_id' => $project->project_id,
        ])->assertAccepted()->json('query_id');

        $row = QueryAuditLog::where('query_id', $queryId)->firstOrFail();
        $this->assertSame($workspaceId, $row->workspace_id);
        $this->assertSame($project->project_id, $row->project_id);
    }

    public function test_start_is_refused_once_project_access_has_been_revoked(): void
    {
        Queue::fake();
        $project = Project::factory()->create();
        $this->user->projects()->attach($project->project_id, ['role' => 'owner']);

        $queryId = $this->postJson('/api/v1/queries', [
            'query' => 'Summarise the 2023 campaign.',
            'project_id' => $project->project_id,
        ])->assertAccepted()->json('query_id');

        // The user still owns the audit row but is no longer a member.
        $this->user->projects()->detach($project->project_id);

        $this->postJson("/api/v1/queries/{$queryId}/start")
            ->assertNotFound()
            ->assertJsonPath('error', 'query_not_found');

        Queue::assertNotPushed(StreamQueryFromFastApi::class);
        $this->assertNull(
            QueryAuditLog::where('query_id', $queryId)->firstOrFail()->dispatched_at,
            'a refused start must not stamp dispatched_at',
        );
    }
}
