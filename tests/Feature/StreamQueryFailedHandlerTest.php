<?php

namespace Tests\Feature;

use App\Events\QueryStreamEvent;
use App\Jobs\StreamQueryFromFastApi;
use App\Models\Project;
use App\Models\QueryAuditLog;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Event;
use Illuminate\Support\Str;
use Tests\TestCase;

/**
 * C8 — Terminal-failure hook (failed() method on the queue job).
 *
 * Exercises the path taken when Horizon SIGTERMs a stuck job at the
 * $timeout boundary, which does NOT run the handle() catch block.
 * Without failed(), the audit row stays unfinalised and the frontend
 * waits for its 5 min client timeout.
 */
class StreamQueryFailedHandlerTest extends TestCase
{
    use RefreshDatabase;

    protected function setUp(): void
    {
        parent::setUp();
        Project::getModel()->setTable('projects');
    }

    private function makeRow(array $overrides = []): QueryAuditLog
    {
        return QueryAuditLog::create(array_merge([
            'user_id' => null,
            'project_id' => (string) Str::uuid(),
            'query_id' => (string) Str::uuid(),
            'query_text' => 'Tell me about the project.',
            'ip_address' => '127.0.0.1',
            'llm_model' => 'qwen2.5:14b',
            'dispatched_at' => now()->subSeconds(4),
        ], $overrides));
    }

    public function test_failed_marks_audit_row_with_failed_prefix_and_elapsed_time(): void
    {
        Event::fake([QueryStreamEvent::class]);

        $row = $this->makeRow();

        $job = new StreamQueryFromFastApi(
            queryId: $row->query_id,
            projectId: $row->project_id,
            queryText: 'unused',
            channel: 'query.'.$row->query_id,
        );

        $job->failed(new \RuntimeException('fastapi stream vanished'));

        $row->refresh();

        $this->assertNotNull($row->response_text);
        $this->assertStringStartsWith('[FAILED', $row->response_text);
        $this->assertStringContainsString('fastapi stream vanished', $row->response_text);
        // Elapsed should be roughly 4000 ms (dispatched 4 s earlier).
        $this->assertGreaterThan(1_000, $row->response_time_ms);
        $this->assertLessThan(60_000, $row->response_time_ms);
    }

    public function test_failed_broadcasts_terminal_failed_event(): void
    {
        Event::fake([QueryStreamEvent::class]);

        $row = $this->makeRow();

        $job = new StreamQueryFromFastApi(
            queryId: $row->query_id,
            projectId: $row->project_id,
            queryText: 'unused',
            channel: 'query.'.$row->query_id,
        );

        $job->failed(new \RuntimeException('boom'));

        // The event must fire exactly once with event:'failed' and the
        // matching query_id so the frontend's failed-handler routes it
        // to the Retry affordance.
        Event::assertDispatched(
            QueryStreamEvent::class,
            function (QueryStreamEvent $e) use ($row) {
                $payload = $this->readEventData($e);

                return ($payload['event'] ?? null) === 'failed'
                    && ($payload['query_id'] ?? null) === $row->query_id;
            },
        );
    }

    public function test_failed_is_idempotent_on_audit_row(): void
    {
        Event::fake([QueryStreamEvent::class]);

        $row = $this->makeRow();

        $job = new StreamQueryFromFastApi(
            queryId: $row->query_id,
            projectId: $row->project_id,
            queryText: 'unused',
            channel: 'query.'.$row->query_id,
        );

        $job->failed(new \RuntimeException('first'));
        $firstText = $row->fresh()->response_text;

        $job->failed(new \RuntimeException('second — different message'));
        $secondText = $row->fresh()->response_text;

        $this->assertSame(
            $firstText,
            $secondText,
            'audit row must not be rewritten on a second failed() call',
        );
    }

    public function test_failed_with_missing_audit_row_does_not_throw(): void
    {
        Event::fake([QueryStreamEvent::class]);

        $job = new StreamQueryFromFastApi(
            queryId: (string) Str::uuid(), // no matching row exists
            projectId: (string) Str::uuid(),
            queryText: 'unused',
            channel: 'query.orphaned',
        );

        // Must not raise — audit-row absence is a warning, not a crash.
        $job->failed(new \RuntimeException('orphan'));

        $this->assertTrue(true);
    }

    /**
     * Reflect into the event's private broadcastData() return value.
     * QueryStreamEvent ships a payload through broadcastWith() on the wire,
     * but in tests we just check the constructor-captured $data.
     */
    private function readEventData(QueryStreamEvent $event): array
    {
        $reflector = new \ReflectionClass($event);
        foreach (['data', 'payload', 'message'] as $candidate) {
            if ($reflector->hasProperty($candidate)) {
                $prop = $reflector->getProperty($candidate);
                $prop->setAccessible(true);
                $value = $prop->getValue($event);
                if (is_array($value)) {
                    return $value;
                }
            }
        }

        // Fallback: let broadcastWith do the work.
        return method_exists($event, 'broadcastWith') ? $event->broadcastWith() : [];
    }
}
