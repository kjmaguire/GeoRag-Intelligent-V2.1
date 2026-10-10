<?php

declare(strict_types=1);

namespace Tests\Unit\Jobs;

use App\Events\QueryStreamEvent;
use Illuminate\Support\Facades\Event;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\Attributes\Test;
use Tests\TestCase;

/**
 * CHAT-22 — the SSE vocabulary is a contract in three places: the FastAPI
 * producer, this repo's relay job, and the React consumer. Change it in one
 * and the chat breaks silently, usually as a stream that never terminates
 * because the browser waits for a terminal frame it will never see.
 *
 *     status · bind · delta · citation · completed · failed
 *
 * This file used to assert that a COMMENT carrying those six names was present
 * in four source files, and regexed the job's source for the names it
 * constructs events with. Neither looks at behaviour: FastAPI could add, drop
 * or rename a frame while the comment stayed, and the relay forwards whatever
 * name FastAPI sends (`dispatchSseEvent($eventType ?? 'delta', ...)`), so a
 * source regex saw only the handful of literals the job itself writes.
 *
 * What is pinned now is what the relay DOES, with SSE bytes shaped like
 * FastAPI's driven through the real job (`TestableStreamQueryFromFastApi`) and
 * the events it broadcasts captured:
 *
 *   - every vocabulary name reaches the browser channel under its own name
 *   - on every path where the job speaks for itself (upstream error, a stream
 *     that ends early) it says so with a vocabulary name and a terminal frame
 *
 * The other two sides are pinned where they live: the set of frames FastAPI
 * can emit, and React's typed list, in
 * src/fastapi/tests/test_sse_vocabulary_contract.py (derived from code, not
 * comments), and the consumer's handling of each frame in
 * resources/js/Pages/Foundry/__tests__/Chat.test.tsx.
 */
final class SseVocabularyContractTest extends TestCase
{
    private const QUERY_ID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';

    /** @var list<string> */
    private const NAMES = ['status', 'bind', 'delta', 'citation', 'completed', 'failed'];

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
            self::QUERY_ID,
            'ffffffff-0000-0000-0000-000000000000',
            'How deep is PLS-22-08?',
            'query.'.self::QUERY_ID,
        );
        $job->fakeSseBody = $sseBody;
        $job->fakeStatus = $status;

        return $job;
    }

    /**
     * One SSE frame exactly as src/fastapi/app/routers/queries.py::_sse_event
     * writes it: `event: <name>`, `data: <json>`, a blank line.
     *
     * @param array<string, mixed> $data
     */
    private static function frame(string $name, array $data): string
    {
        return "event: {$name}\ndata: ".json_encode($data)."\n\n";
    }

    /**
     * @return array<string, array<string, mixed>>
     */
    private static function payloads(): array
    {
        return [
            'status' => ['message' => 'Searching documents…'],
            'bind' => ['citations' => [['citation_id' => '[DATA-1]', 'source_chunk_id' => 'c1']]],
            'delta' => ['token' => 'It is ', 'token_seq' => 0],
            'citation' => ['citation_id' => '[DATA-1]', 'source_chunk_id' => 'c1', 'citation_type' => 'DATA'],
            'completed' => [
                'text' => 'It is 412 m deep [DATA-1].',
                'citations' => [['citation_id' => '[DATA-1]', 'source_chunk_id' => 'c1', 'citation_type' => 'DATA']],
                'confidence' => 0.9,
                'validation_state' => 'clean',
            ],
            'failed' => ['error' => 'The query timed out.', 'code' => 'TIMEOUT'],
        ];
    }

    /**
     * @return list<string> the eventType of every QueryStreamEvent the job broadcast, in order
     */
    private function relayed(TestableStreamQueryFromFastApi $job): array
    {
        Event::fake([QueryStreamEvent::class]);
        $job->handle();

        $seen = [];
        foreach (Event::dispatched(QueryStreamEvent::class) as [$event]) {
            $seen[] = $event->eventType;
        }

        $this->assertNotSame([], $seen, 'the job broadcast nothing at all');

        return $seen;
    }

    /**
     * @return array<string, array{0: string}>
     */
    public static function everyVocabularyName(): array
    {
        return array_combine(self::NAMES, array_map(fn (string $n): array => [$n], self::NAMES));
    }

    #[Test]
    #[DataProvider('everyVocabularyName')]
    public function every_vocabulary_name_reaches_the_browser_channel_under_its_own_name(string $name): void
    {
        // A terminal frame follows the non-terminal names so the job ends the
        // stream cleanly instead of synthesising its own `failed`.
        $terminal = $name === 'failed' ? [] : [self::frame('completed', self::payloads()['completed'])];
        $body = self::frame($name, self::payloads()[$name]).implode('', $terminal);

        $relayed = $this->relayed($this->job($body));

        $this->assertContains($name, $relayed, "the relay did not forward a `{$name}` frame");
        Event::assertDispatched(
            QueryStreamEvent::class,
            fn (QueryStreamEvent $e): bool => $e->eventType === $name && ($e->payload['event'] ?? null) === $name,
        );
    }

    #[Test]
    public function a_whole_answer_stream_arrives_in_order(): void
    {
        $body = implode('', array_map(
            fn (string $n): string => self::frame($n, self::payloads()[$n]),
            ['status', 'bind', 'delta', 'citation', 'completed'],
        ));

        $this->assertSame(
            ['status', 'bind', 'delta', 'citation', 'completed'],
            $this->relayed($this->job($body)),
        );
    }

    #[Test]
    public function a_frame_with_no_event_line_is_relayed_as_a_delta(): void
    {
        // The default name is part of the vocabulary too: a bare `data:` line
        // is a token, not an unknown frame the browser would drop.
        $body = "data: plain text token\n\n".self::frame('completed', self::payloads()['completed']);

        $this->assertSame(['delta', 'completed'], $this->relayed($this->job($body)));
    }

    #[Test]
    public function a_failed_frame_from_upstream_is_forwarded_once_with_its_own_code(): void
    {
        $relayed = $this->relayed($this->job(self::frame('failed', self::payloads()['failed'])));

        // Not followed by the job's own STREAM_TRUNCATED: the upstream `failed`
        // already ended the stream.
        $this->assertSame(['failed'], $relayed);
        Event::assertDispatched(
            QueryStreamEvent::class,
            fn (QueryStreamEvent $e): bool => ($e->payload['code'] ?? null) === 'TIMEOUT',
        );
    }

    /**
     * @return array<string, array{0: string, 1: int}>
     */
    public static function theJobSpeaksForItself(): array
    {
        return [
            'upstream answers HTTP 502' => ['Traceback: boom', 502],
            'stream ends with deltas and no terminal frame' => [
                self::frame('delta', self::payloads()['delta']),
                200,
            ],
            'stream is empty' => ['', 200],
        ];
    }

    #[Test]
    #[DataProvider('theJobSpeaksForItself')]
    public function on_every_path_the_job_speaks_for_itself_it_uses_the_vocabulary_and_ends_the_stream(
        string $body,
        int $status,
    ): void {
        $relayed = $this->relayed($this->job($body, $status));

        foreach ($relayed as $name) {
            $this->assertContains($name, self::NAMES, "the job broadcast `{$name}`, which is not in the SSE vocabulary");
        }
        $this->assertContains(
            end($relayed),
            ['completed', 'failed'],
            'the last frame must be terminal, or the browser waits for one it will never see',
        );
        Event::assertDispatched(QueryStreamEvent::class, fn (QueryStreamEvent $e): bool => $e->eventType === 'failed');
    }

    #[Test]
    public function a_stream_that_ends_early_is_told_apart_from_an_upstream_error(): void
    {
        Event::fake([QueryStreamEvent::class]);

        $this->job(self::frame('delta', self::payloads()['delta']))->handle();

        Event::assertDispatched(
            QueryStreamEvent::class,
            fn (QueryStreamEvent $e): bool => $e->eventType === 'failed' && ($e->payload['code'] ?? null) === 'STREAM_TRUNCATED',
        );
    }
}
