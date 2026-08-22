<?php

namespace Tests\Unit\Jobs;

use App\Events\QueryStreamEvent;
use App\Jobs\StreamQueryFromFastApi;
use Illuminate\Support\Facades\Event;
use Tests\TestCase;

/**
 * Unit tests for StreamQueryFromFastApi.
 *
 * R2 rewrite: the job uses native fopen() to talk to FastAPI, NOT Laravel's
 * Http client — so the old Http::fake() tests silently did nothing and
 * would try to hit fastapi:8000 for real. We now subclass the job
 * (TestableStreamQueryFromFastApi below) and override two protected seams
 * (openHttpStream, responseHeaders) to inject a pre-populated in-memory
 * SSE stream and a canned response header list. The rest of the job
 * runs unchanged, so the line-reader, SSE parser, event switch, and
 * broadcast layer all get real exercise.
 *
 * These tests do NOT use RefreshDatabase — they exclusively exercise the
 * in-memory stream plumbing + broadcast dispatching. Tests that require
 * DB state (completed/failed audit row updates, failed() handler) live
 * in tests/Feature/StreamQueryFailedHandlerTest.php.
 */
class StreamQueryFromFastApiTest extends TestCase
{
    private string $queryId = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';

    private string $projectId = 'ffffffff-0000-0000-0000-000000000000';

    private string $channel = 'query.aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee';

    protected function setUp(): void
    {
        parent::setUp();

        config([
            'services.fastapi.internal_url' => 'http://fastapi:8000',
            // R13 — min 32 bytes; padding avoids the FastApiJwtMinter length
            // validator if any future test path mints a JWT.
            'services.fastapi.service_key' => str_repeat('test-service-key-', 3).'pad',
            'services.fastapi.stream_timeout' => 270,
        ]);
    }

    /**
     * Spin up a testable job subclass with a canned SSE body and status.
     */
    private function makeJob(string $sseBody, int $status = 200): TestableStreamQueryFromFastApi
    {
        $job = new TestableStreamQueryFromFastApi(
            $this->queryId,
            $this->projectId,
            'Test query',
            $this->channel,
        );
        $job->fakeSseBody = $sseBody;
        $job->fakeStatus = $status;

        return $job;
    }

    public function test_handle_broadcasts_delta_and_completed_events(): void
    {
        Event::fake([QueryStreamEvent::class]);

        $sseBody = implode("\n", [
            'event: delta',
            'data: {"token":"Gold grade "}',
            '',
            'event: delta',
            'data: {"token":"averaged 2.3 g/t."}',
            '',
            'event: completed',
            'data: {"text":"final","citations":[],"confidence":0.9}',
            '',
            '',
        ]);

        $this->makeJob($sseBody)->handle();

        // 2 delta broadcasts + 1 completed broadcast.
        Event::assertDispatched(QueryStreamEvent::class, 3);
    }

    public function test_handle_broadcasts_error_event_on_non_200_response(): void
    {
        Event::fake([QueryStreamEvent::class]);

        $this->makeJob('Service unavailable', 503)->handle();

        // Non-2xx response triggers broadcastError() with event='error'
        // and code=statusCode.
        Event::assertDispatched(QueryStreamEvent::class, function (QueryStreamEvent $e) {
            return ($e->eventType ?? null) === 'error'
                && (($e->payload['code'] ?? null) === 503);
        });
    }

    public function test_handle_wraps_plain_string_data_in_text_key(): void
    {
        Event::fake([QueryStreamEvent::class]);

        // SSE `data:` line with non-JSON payload — job wraps in {text: ...}.
        $sseBody = "data: plain text token\n\n";

        $this->makeJob($sseBody)->handle();

        Event::assertDispatched(QueryStreamEvent::class, function (QueryStreamEvent $e) {
            return ($e->payload['text'] ?? null) === 'plain text token';
        });
    }

    public function test_handle_captures_the_answering_model_from_the_completed_frame(): void
    {
        // This replaces a test that fed the job a hand-written `routing`
        // frame and asserted it was rebroadcast. It passed for months while
        // the feature was entirely dead: FastAPI never emitted a routing
        // frame — the producer was lost when app/agent/orchestrator.py
        // became a package — so query_audit_log.llm_model kept whatever
        // dispatch-time default QueryController::store() stamped on it. A
        // test that supplies the input the real producer does not is not
        // testing the pipeline, only the parser.
        //
        // The model now rides the terminal `completed` frame, which the job
        // must receive to do anything at all, so a broken producer shows up
        // as a broken answer rather than as a quietly wrong audit column.
        Event::fake([QueryStreamEvent::class]);

        $sseBody = implode("\n", [
            'event: completed',
            'data: {"text":"ok","citations":[],"confidence":0.9,'
                .'"llm_model":"Cohere-command-a-plus-05-2026"}',
            '',
            '',
        ]);

        $this->makeJob($sseBody)->handle();

        Event::assertDispatched(QueryStreamEvent::class, function (QueryStreamEvent $e) {
            return ($e->eventType ?? null) === 'completed'
                && ($e->payload['llm_model'] ?? null) === 'Cohere-command-a-plus-05-2026';
        });
    }

    public function test_handle_no_longer_treats_routing_as_a_special_frame(): void
    {
        // Belt-and-braces on the removal: if someone reintroduces a
        // `routing` branch without a producer, this says so. An unknown
        // event type is still broadcast verbatim — that is the generic
        // path, and it is the correct behaviour for a frame the job has no
        // special handling for.
        Event::fake([QueryStreamEvent::class]);

        $sseBody = implode("\n", [
            'event: routing',
            'data: {"tier":"fast","model":"claude-haiku-4-5","reason":"classifier"}',
            '',
            'event: completed',
            'data: {"text":"ok","citations":[],"confidence":0.9}',
            '',
            '',
        ]);

        $this->makeJob($sseBody)->handle();

        $source = file_get_contents(app_path('Jobs/StreamQueryFromFastApi.php'));
        $this->assertStringNotContainsString(
            "\$eventType === 'routing'",
            $source,
            'A `routing` branch is back in StreamQueryFromFastApi. Nothing in '
            .'FastAPI emits that frame; check for a producer before relying on it.',
        );
        $this->assertStringNotContainsString(
            'routingPayload',
            $source,
            'routingPayload is back. It was always null in production.',
        );
    }

    public function test_job_is_queued_on_the_llm_queue(): void
    {
        // A3 regression — the $queue property MUST be 'llm' so concurrent
        // 270s streams land on the dedicated Horizon supervisor, not the
        // default pool.
        $job = new StreamQueryFromFastApi(
            $this->queryId,
            $this->projectId,
            'unused',
            $this->channel,
        );
        $this->assertSame('llm', $job->queue);
    }
}

/**
 * Test double: overrides openHttpStream/responseHeaders so the job's
 * fgets() line-reader drains a fake SSE body and sees a fake status line.
 * Everything ELSE in handle() runs for real (JWT mint is fine — the test
 * secret is >= 32 bytes; audit-row updates are guarded by `where first()`
 * returning null in a clean DB).
 */
class TestableStreamQueryFromFastApi extends StreamQueryFromFastApi
{
    public string $fakeSseBody = '';

    public int $fakeStatus = 200;

    protected function openHttpStream(string $url, $context): array
    {
        // php://memory stream pre-populated with the canned body. Rewind
        // so the job's fgets() reads from the beginning.
        $stream = fopen('php://memory', 'r+');
        fwrite($stream, $this->fakeSseBody);
        rewind($stream);

        // Real fopen+HTTP would also populate $http_response_header;
        // tests inject via responseHeaders() so we return [] here.
        return [$stream, []];
    }

    protected function responseHeaders(?array $magic): array
    {
        // Canned status line matching the usual HTTP/1.1 NNN X format so
        // the job's preg_match on the first header captures $fakeStatus.
        $phrase = match (true) {
            $this->fakeStatus >= 200 && $this->fakeStatus < 300 => 'OK',
            default => 'Service Unavailable',
        };

        return ["HTTP/1.1 {$this->fakeStatus} {$phrase}"];
    }
}
