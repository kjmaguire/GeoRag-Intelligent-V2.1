<?php

declare(strict_types=1);

namespace Tests\Feature\Chat;

use App\Jobs\StreamQueryFromFastApi;
use App\Models\Project;
use App\Models\QueryAuditLog;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Queue;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * The query endpoints around a stream that is already running:
 * GET /queries/{id}/result (CHAT-8 recovery), POST /queries/{id}/cancel
 * (CHAT-18), and /start's ordering around dispatched_at (LAR-13).
 */
final class QueryRecoveryAndControlTest extends TestCase
{
    use RefreshDatabase;

    private User $user;

    private Project $project;

    protected function setUp(): void
    {
        parent::setUp();
        Project::getModel()->setTable('projects');

        $this->user = User::factory()->create();
        $this->project = Project::factory()->create();
        $this->user->projects()->attach($this->project->project_id, ['role' => 'owner']);
        $this->actingAs($this->user);
    }

    /**
     * @param array<string, mixed> $overrides
     */
    private function row(array $overrides = []): QueryAuditLog
    {
        return QueryAuditLog::create(array_merge([
            'user_id' => $this->user->id,
            'project_id' => $this->project->project_id,
            'query_id' => (string) Str::uuid(),
            'query_text' => 'how deep is PLS-22-08?',
            'ip_address' => '127.0.0.1',
            'llm_model' => 'command-a-plus-05-2026',
            'dispatched_at' => now(),
        ], $overrides));
    }

    public function test_result_serves_a_finished_answer_with_its_verdicts(): void
    {
        $row = $this->row([
            'response_text' => 'PLS-22-08 is 412 m deep [DATA-1].',
            'citations' => [['citation_id' => '[DATA-1]', 'source_chunk_id' => 'c1']],
            'confidence' => 0.9,
        ]);
        $row->metadata = ['validation_state' => 'flagged', 'answer_run_id' => 'r-1'];
        $row->save();

        $this->getJson("/api/v1/queries/{$row->query_id}/result")
            ->assertOk()
            ->assertJson([
                'status' => 'completed',
                'text' => 'PLS-22-08 is 412 m deep [DATA-1].',
                'validation_state' => 'flagged',
                'answer_run_id' => 'r-1',
                'confidence' => 0.9,
            ])
            ->assertJsonPath('citations.0.citation_id', '[DATA-1]');
    }

    public function test_result_reports_running_and_failed_without_leaking_the_marker(): void
    {
        $running = $this->row();
        $this->getJson("/api/v1/queries/{$running->query_id}/result")->assertOk()->assertJson(['status' => 'running']);

        $failed = $this->row(['response_text' => '[error: TIMEOUT — asyncpg pool at 10.0.3.4 exhausted]']);
        $response = $this->getJson("/api/v1/queries/{$failed->query_id}/result")
            ->assertOk()
            ->assertJson(['status' => 'failed', 'code' => 'TIMEOUT']);
        $this->assertStringNotContainsString('10.0.3.4', (string) $response->getContent());
    }

    public function test_result_is_owner_only(): void
    {
        $row = $this->row(['response_text' => 'secret answer']);
        $this->actingAs(User::factory()->create());

        $this->getJson("/api/v1/queries/{$row->query_id}/result")->assertNotFound();
    }

    public function test_cancel_sets_the_flag_the_job_polls(): void
    {
        $row = $this->row();

        $this->postJson("/api/v1/queries/{$row->query_id}/cancel")->assertAccepted();

        $this->assertTrue(Cache::has(StreamQueryFromFastApi::cancelCacheKey((string) $row->query_id)));
    }

    public function test_cancel_is_owner_only(): void
    {
        $row = $this->row();
        $this->actingAs(User::factory()->create());

        $this->postJson("/api/v1/queries/{$row->query_id}/cancel")->assertNotFound();
        $this->assertFalse(Cache::has(StreamQueryFromFastApi::cancelCacheKey((string) $row->query_id)));
    }

    public function test_a_malformed_conversation_id_starts_the_query_single_shot(): void
    {
        // LAR-13: a non-UUID id raised inside the ownership query AFTER
        // dispatched_at was committed — a 500, then 409 on every retry.
        Queue::fake();
        $row = $this->row(['dispatched_at' => null]);

        $this->postJson("/api/v1/queries/{$row->query_id}/start", ['conversation_id' => 'not-a-uuid'])
            ->assertAccepted();

        Queue::assertPushed(StreamQueryFromFastApi::class, 1);
    }

    public function test_a_failed_dispatch_can_be_retried(): void
    {
        // LAR-13: if the push fails the stamp is undone, so the client's
        // retry dispatches instead of getting 409 already_dispatched.
        $row = $this->row(['dispatched_at' => null]);
        Queue::shouldReceive('connection')->andThrow(new \RuntimeException('READONLY You can\'t write against a read only replica.'));

        $this->postJson("/api/v1/queries/{$row->query_id}/start")->assertStatus(503);
        $this->assertNull($row->fresh()->dispatched_at);
    }
}
