<?php

declare(strict_types=1);

namespace Tests\Unit\Jobs;

use App\Events\QueryStreamEvent;
use App\Jobs\StreamQueryFromFastApi;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Event;
use Tests\TestCase;

/**
 * What the browser actually receives from StreamQueryFromFastApi on the
 * paths that used to end without a terminal frame or with a leaky one:
 * an oversized `completed` (CHAT-5), a terminal whose broadcast throws
 * (CHAT-5), an upstream error body (CHAT-20) and a user Stop (CHAT-18).
 *
 * Uses the same openHttpStream/responseHeaders seam as
 * StreamQueryFromFastApiTest; no database.
 */
final class StreamQueryDeliveryTest extends TestCase
{
    private string $queryId = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';

    protected function setUp(): void
    {
        parent::setUp();

        config([
            'services.fastapi.internal_url' => 'http://fastapi:8000',
            'services.fastapi.service_key' => str_repeat('test-service-key-', 3).'pad',
            'services.fastapi.stream_timeout' => 270,
        ]);
    }

    private function job(string $sseBody, int $status = 200): TestableStreamQueryFromFastApi
    {
        $job = new TestableStreamQueryFromFastApi(
            $this->queryId,
            'ffffffff-0000-0000-0000-000000000000',
            'Show me the drill traces in 3D',
            'query.'.$this->queryId,
        );
        $job->fakeSseBody = $sseBody;
        $job->fakeStatus = $status;

        return $job;
    }

    /**
     * @param array<string, mixed> $completed
     */
    private function sse(array $completed): string
    {
        return implode("\n", [
            'event: delta',
            'data: {"token":"Five holes ","token_seq":0}',
            '',
            'event: completed',
            'data: '.json_encode($completed),
            '',
            '',
        ]);
    }

    public function test_an_oversized_completed_frame_is_slimmed_not_dropped(): void
    {
        config(['services.fastapi.completed_frame_budget_bytes' => 2_000]);
        Event::fake([QueryStreamEvent::class]);

        $this->job($this->sse([
            'text' => 'Five holes intersect the roll front [DATA-1].',
            'citations' => [['citation_id' => '[DATA-1]', 'source_chunk_id' => 'c1', 'citation_type' => 'DATA']],
            'confidence' => 0.8,
            'validation_state' => 'clean',
            'answer_run_id' => '11111111-2222-3333-4444-555555555555',
            'degraded_sources' => ['Document ranking (temporarily unavailable)'],
            'viz_payload' => ['chart_type' => 'drill_trace_3d', 'plotly_layout' => ['meta' => ['collars' => array_fill(0, 200, ['x' => 1.23456789, 'y' => 2.3456789])]]],
            'map_payload' => null,
        ]))->handle();

        Event::assertDispatched(QueryStreamEvent::class, function (QueryStreamEvent $e): bool {
            return $e->eventType === 'completed'
                && ($e->payload['payload_truncated'] ?? false) === true
                && $e->payload['text'] === 'Five holes intersect the roll front [DATA-1].'
                && count($e->payload['citations']) === 1
                && $e->payload['validation_state'] === 'clean'
                && $e->payload['answer_run_id'] === '11111111-2222-3333-4444-555555555555'
                // A partial answer must still say so after slimming.
                && $e->payload['degraded_sources'] === ['Document ranking (temporarily unavailable)']
                && ! array_key_exists('viz_payload', $e->payload)
                && $e->payload['truncated_fields'] === ['viz_payload'];
        });
    }

    public function test_a_frame_within_budget_is_broadcast_whole(): void
    {
        Event::fake([QueryStreamEvent::class]);

        $this->job($this->sse([
            'text' => 'ok',
            'citations' => [['citation_id' => '[DATA-1]']],
            'confidence' => 0.8,
            'viz_payload' => ['chart_type' => 'bar'],
        ]))->handle();

        Event::assertDispatched(QueryStreamEvent::class, fn (QueryStreamEvent $e): bool => $e->eventType === 'completed'
            && ! array_key_exists('payload_truncated', $e->payload)
            && $e->payload['viz_payload'] === ['chart_type' => 'bar']);
    }

    public function test_a_completed_frame_that_fails_to_broadcast_is_followed_by_a_recoverable_terminal(): void
    {
        // Before: the exception was logged and swallowed, the browser had
        // every delta and no terminal, and two minutes later the watchdog
        // blamed the realtime channel for an answer that existed.
        $seen = &$this->recordBroadcastsRef('completed');

        $this->job($this->sse(['text' => 'ok', 'citations' => [['citation_id' => '[DATA-1]']], 'confidence' => 0.8]))->handle();

        $fallback = array_values(array_filter($seen, fn (QueryStreamEvent $e): bool => $e->eventType === 'failed'));
        $this->assertCount(1, $fallback);
        $this->assertSame('DELIVERY_FAILED', $fallback[0]->payload['code']);
        $this->assertTrue($fallback[0]->payload['recoverable']);
    }

    public function test_an_upstream_error_body_never_reaches_the_browser(): void
    {
        Event::fake([QueryStreamEvent::class]);

        $this->job('Traceback: psycopg.errors at http://fastapi.georag.internal:8000/internal/queries', 502)->handle();

        Event::assertDispatched(QueryStreamEvent::class, function (QueryStreamEvent $e): bool {
            $wire = (string) json_encode($e->payload);

            return $e->eventType === 'failed'
                && $e->payload['code'] === 502
                && ! str_contains($wire, 'fastapi.georag.internal')
                && ! str_contains($wire, 'Traceback');
        });
    }

    public function test_stop_closes_the_stream_and_ends_with_a_cancelled_terminal(): void
    {
        Event::fake([QueryStreamEvent::class]);
        Cache::put(StreamQueryFromFastApi::cancelCacheKey($this->queryId), true, 60);

        $this->job($this->sse(['text' => 'never delivered', 'citations' => [['citation_id' => '[DATA-1]']], 'confidence' => 0.8]))->handle();

        Event::assertNotDispatched(QueryStreamEvent::class, fn (QueryStreamEvent $e): bool => $e->eventType === 'completed');
        Event::assertDispatched(QueryStreamEvent::class, fn (QueryStreamEvent $e): bool => $e->eventType === 'failed'
            && $e->payload['code'] === 'CANCELLED');
    }

    /**
     * @return list<QueryStreamEvent>
     */
    private function &recordBroadcastsRef(string $throwOn): array
    {
        $this->recorded = [];
        Event::listen(QueryStreamEvent::class, function (QueryStreamEvent $e) use ($throwOn): void {
            $this->recorded[] = $e;
            if ($e->eventType === $throwOn) {
                throw new \RuntimeException('Reverb 413 Request Entity Too Large');
            }
        });

        return $this->recorded;
    }

    /** @var list<QueryStreamEvent> */
    private array $recorded = [];
}
