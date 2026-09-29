<?php

declare(strict_types=1);

namespace Tests\Feature\Chat;

use App\Events\QueryStreamEvent;
use App\Jobs\StreamQueryFromFastApi;
use App\Models\ChatConversation;
use App\Models\ChatMessage;
use App\Models\Project;
use App\Models\QueryAuditLog;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\DB;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\Facades\Schema;
use Illuminate\Support\Str;
use Tests\Concerns\CreatesChatTablesOnSqlite;
use Tests\TestCase;
use Tests\Unit\Jobs\TestableStreamQueryFromFastApi;

/**
 * StreamQueryFromFastApi paths that touch the database: the multi-turn
 * history it sends (CHAT-2), what failed() may and may not overwrite
 * (CHAT-1 / CHAT-13 / CHAT-20), the stale-job guard (CHAT-14) and what the
 * completion write keeps for the result endpoint (CHAT-8).
 */
final class StreamQueryRelayTest extends TestCase
{
    use CreatesChatTablesOnSqlite;
    use RefreshDatabase;

    protected function setUp(): void
    {
        parent::setUp();
        Project::getModel()->setTable('projects');
        $this->createChatTablesIfMissing();

        config([
            'services.fastapi.internal_url' => 'http://fastapi:8000',
            'services.fastapi.service_key' => str_repeat('test-service-key-', 3).'pad',
            'services.fastapi.stream_timeout' => 270,
        ]);
    }

    /**
     * @param array<string, mixed> $overrides
     */
    private function auditRow(array $overrides = []): QueryAuditLog
    {
        return QueryAuditLog::create(array_merge([
            'user_id' => null,
            'project_id' => (string) Str::uuid(),
            'query_id' => (string) Str::uuid(),
            'query_text' => 'what about its grades?',
            'ip_address' => '127.0.0.1',
            'llm_model' => 'command-a-plus-05-2026',
            'dispatched_at' => now()->subSeconds(2),
        ], $overrides));
    }

    private function job(QueryAuditLog $row, ?string $conversationId = null, string $sse = ''): TestableStreamQueryFromFastApi
    {
        $job = new TestableStreamQueryFromFastApi(
            (string) $row->query_id,
            (string) $row->project_id,
            'what about its grades?',
            'query.'.$row->query_id,
            null,
            $conversationId,
        );
        $job->fakeSseBody = $sse;

        return $job;
    }

    private function completedSse(): string
    {
        return implode("\n", [
            'event: completed',
            'data: '.json_encode([
                'text' => 'PLS-22-08 averages 0.21% U3O8 [DATA-1].',
                'citations' => [['citation_id' => '[DATA-1]', 'source_chunk_id' => 'c1']],
                'confidence' => 0.8,
                'validation_state' => 'flagged',
                'answer_run_id' => '11111111-2222-3333-4444-555555555555',
                'refusal_payload' => null,
            ]),
            '',
            '',
        ]);
    }

    public function test_the_job_sends_the_thread_history_in_thread_order(): void
    {
        // CHAT-2 / LAR-2: the loader selected and ordered by `id`, which
        // chat_messages does not have. The query raised, the catch returned
        // [], and FastAPI never saw a single prior turn. Rows are written
        // in the same second (as a real sync does) and out of insertion
        // order, so only `position` can put them back.
        Event::fake([QueryStreamEvent::class]);
        $user = User::factory()->create();
        $conversation = ChatConversation::create([
            'conversation_id' => (string) Str::uuid(),
            'user_id' => $user->id,
            'title' => 'PLS grades',
        ]);
        $now = now();
        foreach ([[2, 'user', 'what about its grades?'], [0, 'user', 'tell me about PLS-22-08'], [1, 'assistant', 'PLS-22-08 is a vertical hole.']] as [$position, $role, $text]) {
            ChatMessage::create([
                'conversation_id' => $conversation->conversation_id,
                'role' => $role,
                'content' => $text,
                'metadata' => [],
                'position' => $position,
                'created_at' => $now,
            ]);
        }

        $job = $this->job($this->auditRow(), (string) $conversation->conversation_id, $this->completedSse());
        $job->handle();

        $history = $job->sentPayload['history'] ?? null;
        $this->assertIsArray($history, 'the FastAPI payload carried no history');
        $this->assertSame(
            ['tell me about PLS-22-08', 'PLS-22-08 is a vertical hole.', 'what about its grades?'],
            array_column($history, 'text'),
        );
        $this->assertSame([0, 1, 2], array_column($history, 'turn_index'));
    }

    public function test_completion_keeps_what_the_result_endpoint_needs(): void
    {
        Event::fake([QueryStreamEvent::class]);
        $row = $this->auditRow();

        $this->job($row, null, $this->completedSse())->handle();

        $row->refresh();
        $this->assertSame('PLS-22-08 averages 0.21% U3O8 [DATA-1].', $row->response_text);
        $this->assertSame('flagged', $row->metadata['validation_state'] ?? null);
        $this->assertSame('11111111-2222-3333-4444-555555555555', $row->metadata['answer_run_id'] ?? null);
    }

    public function test_failed_never_overwrites_a_successful_answer(): void
    {
        // CHAT-1 (b): a re-queued duplicate of a job that had already
        // finished used to run failed() later and replace the answer in
        // the audit row with a [FAILED marker.
        Event::fake([QueryStreamEvent::class]);
        $row = $this->auditRow(['response_text' => 'The answer.']);

        $this->job($row)->failed(new \RuntimeException('MaxAttemptsExceededException'));

        $this->assertSame('The answer.', $row->fresh()->response_text);
        Event::assertNotDispatched(QueryStreamEvent::class);
    }

    public function test_failed_does_not_repeat_a_terminal_handle_already_sent(): void
    {
        // CHAT-20: handle()'s catch broadcasts, writes an [error: marker,
        // and rethrows; failed() then sent a second terminal and replaced
        // the recorded cause with its own.
        Event::fake([QueryStreamEvent::class]);
        $row = $this->auditRow(['response_text' => '[error: INTERNAL — connection refused]']);

        $this->job($row)->failed(new \RuntimeException('connection refused'));

        $this->assertSame('[error: INTERNAL — connection refused]', $row->fresh()->response_text);
        Event::assertNotDispatched(QueryStreamEvent::class);
    }

    public function test_failed_broadcasts_a_terminal_even_when_the_database_is_the_failure(): void
    {
        // CHAT-13: the audit lookup ran first and unguarded, so a dead DB
        // threw before the terminal broadcast and the browser was left on
        // its watchdog.
        if (DB::connection()->getDriverName() !== 'sqlite') {
            $this->markTestSkipped('Simulates a dead database by dropping the audit table; SQLite-only.');
        }
        Event::fake([QueryStreamEvent::class]);
        $row = $this->auditRow();
        Schema::drop((new QueryAuditLog)->getTable());

        $this->job($row)->failed(new \RuntimeException('SQLSTATE[08006] connection failure'));

        Event::assertDispatched(QueryStreamEvent::class, fn (QueryStreamEvent $e): bool => $e->eventType === 'failed'
            && $e->payload['code'] === 'JOB_FAILED');
    }

    public function test_a_job_picked_up_after_the_browser_gave_up_does_not_call_fastapi(): void
    {
        // CHAT-14: with the llm supervisor down or backlogged the job ran
        // whenever it was popped, long after the watchdog, and billed an
        // LLM run nobody would see.
        Event::fake([QueryStreamEvent::class]);
        config(['services.fastapi.queue_stale_after' => 110]);
        $row = $this->auditRow(['dispatched_at' => now()->subSeconds(300)]);

        $job = $this->job($row, null, $this->completedSse());
        $job->handle();

        $this->assertNull($job->sentPayload, 'a stale job must not call FastAPI');
        Event::assertDispatched(QueryStreamEvent::class, fn (QueryStreamEvent $e): bool => $e->eventType === 'failed'
            && $e->payload['code'] === 'QUEUE_STALE');
        $this->assertStringStartsWith('[error: QUEUE_STALE', (string) $row->fresh()->response_text);
    }
}
