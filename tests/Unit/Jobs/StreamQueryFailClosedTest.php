<?php

declare(strict_types=1);

namespace Tests\Unit\Jobs;

use App\Events\QueryStreamEvent;
use App\Models\QueryAuditLog;
use Illuminate\Support\Facades\Event;
use PHPUnit\Framework\Attributes\DataProvider;
use Tests\TestCase;

/**
 * StreamQueryFromFastApi must not run the RAG path without a resolved user
 * and workspace (tenant guard), and must stop hammering a dead Reverb with
 * per-token deltas.
 *
 * Same openHttpStream/responseHeaders seam as StreamQueryDeliveryTest; the
 * identity lookups are stubbed through TestableStreamQueryFromFastApi.
 */
final class StreamQueryFailClosedTest extends TestCase
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

    private function job(string $sseBody = ''): TestableStreamQueryFromFastApi
    {
        $job = new TestableStreamQueryFromFastApi(
            $this->queryId,
            'ffffffff-0000-0000-0000-000000000000',
            'Show me the drill traces',
            'query.'.$this->queryId,
        );
        $job->fakeSseBody = $sseBody !== '' ? $sseBody : "event: completed\ndata: {\"text\":\"ok\",\"citations\":[],\"confidence\":0.8}\n\n";

        return $job;
    }

    /**
     * @return array<string, array{0: string, 1: string}>
     */
    public static function unresolvedIdentityCases(): array
    {
        return [
            'audit lookup throws' => ['auditLookupThrows', 'AUDIT_LOOKUP_FAILED'],
            'workspace lookup throws' => ['workspaceLookupThrows', 'WORKSPACE_LOOKUP_FAILED'],
            'project has no workspace' => ['workspaceMissing', 'WORKSPACE_UNRESOLVED'],
            'audit row missing' => ['auditRowMissing', 'IDENTITY_UNRESOLVED'],
            'audit row has no user' => ['userMissing', 'IDENTITY_UNRESOLVED'],
        ];
    }

    #[DataProvider('unresolvedIdentityCases')]
    public function test_an_unresolved_identity_fails_the_job_without_calling_fastapi(string $break, string $expectedCode): void
    {
        Event::fake([QueryStreamEvent::class]);
        $job = $this->job();

        match ($break) {
            'auditLookupThrows' => $job->auditLookupThrows = true,
            'workspaceLookupThrows' => $job->workspaceLookupThrows = true,
            'workspaceMissing' => $job->workspaceId = null,
            'auditRowMissing' => $job->auditRowMissing = true,
            'userMissing' => $job->auditRow = (new QueryAuditLog)->forceFill(['user_id' => null]),
        };

        $job->handle();

        $this->assertNull($job->sentPayload, 'FastAPI must not be called without a resolved user and workspace.');
        Event::assertNotDispatched(QueryStreamEvent::class, fn (QueryStreamEvent $e): bool => $e->eventType === 'completed');
        Event::assertDispatchedTimes(QueryStreamEvent::class, 1);
        Event::assertDispatched(QueryStreamEvent::class, function (QueryStreamEvent $e) use ($expectedCode): bool {
            $wire = (string) json_encode($e->payload);

            return $e->eventType === 'failed'
                && $e->payload['code'] === $expectedCode
                && ! str_contains($wire, 'SQLSTATE')
                && ! str_contains($wire, 'unknown');
        });
    }

    public function test_a_resolved_identity_streams_normally(): void
    {
        Event::fake([QueryStreamEvent::class]);
        $job = $this->job();

        $job->handle();

        $this->assertNotNull($job->sentPayload);
        Event::assertDispatched(QueryStreamEvent::class, fn (QueryStreamEvent $e): bool => $e->eventType === 'completed');
    }

    public function test_deltas_stop_after_five_consecutive_broadcast_failures_but_the_terminal_still_goes_out(): void
    {
        $deltaAttempts = 0;
        $terminals = [];
        Event::listen(QueryStreamEvent::class, function (QueryStreamEvent $e) use (&$deltaAttempts, &$terminals): void {
            if ($e->eventType === 'delta') {
                $deltaAttempts++;

                throw new \RuntimeException('cURL error 28: Operation timed out');
            }
            $terminals[] = $e->eventType;
        });

        $frames = [];
        for ($i = 0; $i < 20; $i++) {
            $frames[] = "event: delta\ndata: {\"token\":\"t{$i} \",\"token_seq\":{$i}}\n";
        }
        $body = implode("\n", $frames)."\nevent: completed\ndata: {\"text\":\"ok\",\"citations\":[],\"confidence\":0.8}\n\n";

        $this->job($body)->handle();

        $this->assertSame(5, $deltaAttempts, 'broadcasting must be abandoned after 5 consecutive failures');
        $this->assertSame(['completed'], $terminals, 'the terminal frame must still be broadcast');
    }

    public function test_a_successful_broadcast_resets_the_consecutive_failure_count(): void
    {
        $attempts = 0;
        Event::listen(QueryStreamEvent::class, function (QueryStreamEvent $e) use (&$attempts): void {
            if ($e->eventType !== 'delta') {
                return;
            }
            $attempts++;
            // Fail four, succeed one, fail four, succeed one, ...: never five in a row.
            if ($attempts % 5 !== 0) {
                throw new \RuntimeException('cURL error 28');
            }
        });

        $frames = [];
        for ($i = 0; $i < 20; $i++) {
            $frames[] = "event: delta\ndata: {\"token\":\"t{$i} \",\"token_seq\":{$i}}\n";
        }

        $this->job(implode("\n", $frames)."\n")->handle();

        $this->assertSame(20, $attempts, 'intermittent failures must not trip the cutoff');
    }

    public function test_reverb_client_options_bound_the_inline_broadcast(): void
    {
        $options = config('broadcasting.connections.reverb.client_options');

        $this->assertSame(2.0, $options['timeout']);
        $this->assertSame(1.0, $options['connect_timeout']);
    }
}
