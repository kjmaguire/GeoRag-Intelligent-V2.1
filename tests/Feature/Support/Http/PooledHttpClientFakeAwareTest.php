<?php

declare(strict_types=1);

namespace Tests\Feature\Support\Http;

use App\Support\Http\PooledHttpClient;
use Illuminate\Support\Facades\Http;
use Tests\TestCase;

/**
 * `Http::fake()` must reach a pooled client.
 *
 * This has been fixed once already, on a branch that never merged here, and
 * nothing caught it coming back — the only tests that exercised the path were
 * deleted along with Martin in 0eada56c, so the regression rode in silently
 * and surfaced months later as five tile-proxy tests failing with 502 where
 * the fake said 200.
 *
 * The mechanism: `PendingRequest::buildClient()` returns `$this->client`
 * verbatim once `setClient()` has been called, so a pooled Guzzle client
 * carries none of the factory's stub-handler middleware. The fake is bypassed
 * and a REAL SOCKET opens.
 *
 * The dangerous part is not the failures. It is that some tests then pass
 * COINCIDENTALLY — a real service answering the same way the fake would — so
 * a suite can be green while testing the network instead of the code.
 *
 * Lives in Feature, not Unit: it needs a booted application, which is exactly
 * the condition the guard keys on.
 */
final class PooledHttpClientFakeAwareTest extends TestCase
{
    public function test_a_pooled_request_is_intercepted_by_the_fake(): void
    {
        Http::fake(['tiles.internal/*' => Http::response('FAKED', 200)]);

        $response = app(PooledHttpClient::class)
            ->forBaseUrl('http://tiles.internal')
            ->get('/x/1/2/3.pbf');

        $this->assertSame(200, $response->status());
        $this->assertSame('FAKED', $response->body());
    }

    public function test_the_fake_records_the_request_rather_than_the_network_swallowing_it(): void
    {
        Http::fake(['tiles.internal/*' => Http::response('', 204)]);

        app(PooledHttpClient::class)
            ->forBaseUrl('http://tiles.internal')
            ->get('/probe');

        Http::assertSent(fn ($request) => str_contains($request->url(), '/probe'));
    }

    public function test_a_faked_failure_surfaces_as_that_status_not_as_a_connection_error(): void
    {
        // The 502-instead-of-200 shape: without the guard this never reaches
        // the fake, the real dial fails, and the caller reports a transport
        // error that has nothing to do with what the test set up.
        Http::fake(['tiles.internal/*' => Http::response('upstream boom', 503)]);

        $response = app(PooledHttpClient::class)
            ->forBaseUrl('http://tiles.internal')
            ->get('/x');

        $this->assertSame(503, $response->status());
    }

    public function test_two_calls_to_one_base_url_are_both_faked(): void
    {
        // Pooling keys on the base URL, so if any caching of the client
        // leaked past the guard it would show up on the SECOND call.
        Http::fake(['tiles.internal/*' => Http::response('ok', 200)]);

        $pool = app(PooledHttpClient::class);
        $first = $pool->forBaseUrl('http://tiles.internal')->get('/a');
        $second = $pool->forBaseUrl('http://tiles.internal')->get('/b');

        $this->assertSame(200, $first->status());
        $this->assertSame(200, $second->status());
        Http::assertSentCount(2);
    }

    public function test_the_base_url_and_timeout_still_apply_under_the_guard(): void
    {
        // The guard drops setClient(), not the configuration around it — a
        // request that lost its base URL would fail in a completely
        // different and more confusing way.
        Http::fake(['tiles.internal/*' => Http::response('ok', 200)]);

        app(PooledHttpClient::class)
            ->forBaseUrl('http://tiles.internal', 7)
            ->get('/relative/path');

        Http::assertSent(
            fn ($request) => $request->url() === 'http://tiles.internal/relative/path',
        );
    }
}
