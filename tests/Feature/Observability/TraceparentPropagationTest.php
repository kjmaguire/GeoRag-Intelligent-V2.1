<?php

declare(strict_types=1);

namespace Tests\Feature\Observability;

use App\Http\Middleware\InjectTraceparent;
use App\Jobs\StreamQueryFromFastApi;
use Tests\TestCase;

/**
 * L1555 — the trace chain must survive the Laravel → queue → FastAPI hop.
 *
 * InjectTraceparent's own docblock states the policy: "when Laravel makes
 * an internal HTTP call to FastAPI ... the caller pulls the trace-id from
 * `$request->attributes->get('traceparent')` and includes it as a header
 * on the outbound request." No caller ever did — before this change,
 * `grep -rn traceparent app/ --include='*.php'` matched only the
 * middleware that mints it.
 *
 * So FastAPI's StructuredAccessLogMiddleware minted a fresh, unrelated
 * trace id for every chat request, and there was no join key between the
 * two services' logs. Worse, middleware.py's docstring asserted that
 * Laravel already forwarded the header, which is how the gap survived
 * long enough to be found by an audit rather than by a debugging session.
 *
 * These tests read the headers the job actually puts on the wire, by
 * capturing the stream context it hands to openHttpStream() — the same
 * seam the existing unit tests use for the SSE body.
 */
final class TraceparentPropagationTest extends TestCase
{
    private const VALID = '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01';

    protected function setUp(): void
    {
        parent::setUp();

        config([
            'services.fastapi.internal_url' => 'http://fastapi:8000',
            'services.fastapi.service_key' => str_repeat('test-service-key-', 3).'pad',
            'services.fastapi.stream_timeout' => 270,
        ]);
    }

    private function job(): HeaderCapturingStreamQuery
    {
        return new HeaderCapturingStreamQuery(
            'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            'ffffffff-0000-0000-0000-000000000000',
            'what is the average grade?',
            'query.aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        );
    }

    /** @return array<int, string> */
    private function headersFor(?string $traceparent): array
    {
        $job = $this->job();
        $job->withTraceparent($traceparent);
        $job->handle();

        return $job->capturedHeaders;
    }

    // -----------------------------------------------------------------
    // The wire
    // -----------------------------------------------------------------

    public function test_the_traceparent_is_forwarded_to_fastapi(): void
    {
        $headers = $this->headersFor(self::VALID);

        $this->assertContains(
            'traceparent: '.self::VALID,
            $headers,
            'Without this header FastAPI mints its own trace id and the two '
            .'services share no join key.',
        );
    }

    public function test_the_query_id_is_forwarded_as_the_request_id(): void
    {
        $headers = $this->headersFor(self::VALID);

        $this->assertContains(
            'X-Request-ID: aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            $headers,
            'A support ticket quoting one id has to find both sides of the hop.',
        );
    }

    public function test_the_existing_headers_are_untouched(): void
    {
        $headers = $this->headersFor(self::VALID);

        $this->assertContains('Content-Type: application/json', $headers);
        $this->assertContains('Accept: text/event-stream', $headers);
        $this->assertTrue(
            (bool) array_filter($headers, fn ($h) => str_starts_with($h, 'Authorization: Bearer ')),
            'The B7 JWT must still be sent.',
        );
        $this->assertTrue(
            (bool) array_filter($headers, fn ($h) => str_starts_with($h, 'X-Service-Key: ')),
            'The graceful-cutover service key must still be sent.',
        );
    }

    // -----------------------------------------------------------------
    // Absence and invalidity
    // -----------------------------------------------------------------

    public function test_no_traceparent_header_is_sent_when_there_is_no_trace_context(): void
    {
        // Console dispatch, or a job replayed outside a request. Sending
        // `traceparent: ` empty would fail the middleware's v00 validation
        // and be replaced by a minted one anyway — so send nothing and let
        // FastAPI mint deliberately rather than as a fallback.
        $headers = $this->headersFor(null);

        $this->assertSame(
            [],
            array_values(array_filter($headers, fn ($h) => str_starts_with($h, 'traceparent'))),
        );
        // Everything else still goes out.
        $this->assertContains('Content-Type: application/json', $headers);
    }

    public function test_a_malformed_traceparent_is_dropped_rather_than_forwarded(): void
    {
        // A client can put anything in this header. Forwarding garbage
        // makes FastAPI's slice at [3:35] produce a nonsense trace id
        // instead of a clean minted one.
        $headers = $this->headersFor('not-a-traceparent');

        $this->assertSame(
            [],
            array_values(array_filter($headers, fn ($h) => str_starts_with($h, 'traceparent'))),
        );
    }

    public function test_trace_id_exposes_the_32_hex_slice_for_log_correlation(): void
    {
        $job = $this->job();
        $job->withTraceparent(self::VALID);

        $this->assertSame('4bf92f3577b34da6a3ce929d0e0e4736', $job->traceId());

        $job->withTraceparent(null);
        $this->assertNull($job->traceId());
    }

    // -----------------------------------------------------------------
    // Deploy safety
    // -----------------------------------------------------------------

    public function test_a_job_serialised_without_a_traceparent_still_runs(): void
    {
        // In-flight jobs queued by the previous release have no
        // `traceparent` key in their payload. If the property were a
        // PROMOTED constructor property its default would live on the
        // parameter, not the property, so unserialize would leave it
        // uninitialised and the first read would throw a TypeError —
        // silently killing every queued chat request during a deploy.
        // Serialised on the REAL class — Laravel's SerializesModels mangles
        // private property names with get_class($this), so a test subclass
        // round-trips differently from what the queue actually stores.
        $job = new StreamQueryFromFastApi(
            'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
            'ffffffff-0000-0000-0000-000000000000',
            'what is the average grade?',
            'query.aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        );
        $payload = serialize($job);

        // __serialize() skips any property still equal to its declared
        // default, so a job with no trace context serialises to exactly the
        // byte shape the previous release produced. That is what makes this
        // a faithful old-payload test rather than a simulation.
        $this->assertStringNotContainsString('traceparent', $payload);

        $revived = unserialize($payload);

        $this->assertNull(
            $revived->traceId(),
            'An absent traceparent must read back as null, not throw. '
            .'__unserialize() skips keys the payload does not carry, so a '
            .'promoted property would be left uninitialised here.',
        );
    }

    public function test_the_dispatcher_hands_the_request_trace_context_to_the_job(): void
    {
        $source = file_get_contents(app_path('Http/Controllers/Api/V1/QueryController.php'));

        $this->assertStringContainsString(
            'InjectTraceparent::ATTRIBUTE_KEY',
            $source,
            'QueryController must read back what InjectTraceparent stored; '
            .'nothing had ever read that attribute.',
        );
        $this->assertStringContainsString('->withTraceparent(', $source);
    }

    public function test_the_fastapi_middleware_no_longer_claims_laravel_already_forwards(): void
    {
        $path = base_path('src/fastapi/app/middleware.py');
        if (! is_file($path)) {
            $this->markTestSkipped('FastAPI source not present in this checkout.');
        }

        $source = file_get_contents($path);

        $this->assertStringNotContainsString(
            '`StreamQueryFromFastApi` already forwards the inbound header',
            $source,
            'That sentence was false for the entire life of the middleware '
            .'and is why nobody looked at the job.',
        );
    }
}

/**
 * Captures the outbound header list from the stream context.
 *
 * `handle()` builds its headers inside a `stream_context_create()` call,
 * so the only honest way to read them is where the context is handed off.
 * openHttpStream() is already a protected seam for exactly this reason.
 */
class HeaderCapturingStreamQuery extends StreamQueryFromFastApi
{
    /** @var array<int, string> */
    public array $capturedHeaders = [];

    protected function openHttpStream(string $url, $context): array
    {
        $options = stream_context_get_options($context);
        $this->capturedHeaders = $options['http']['header'] ?? [];

        $stream = fopen('php://memory', 'r+');
        // Terminal frame so the job's reader loop exits cleanly.
        fwrite($stream, "event: completed\ndata: {\"answer\":\"ok\"}\n\n");
        rewind($stream);

        return [$stream, []];
    }

    protected function responseHeaders(?array $magic): array
    {
        return ['HTTP/1.1 200 OK'];
    }
}
