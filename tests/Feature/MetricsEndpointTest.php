<?php

declare(strict_types=1);

namespace Tests\Feature;

use App\Support\AuthorizationAuditLogger;
use Illuminate\Queue\RedisQueue;
use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Config;
use Illuminate\Support\Facades\Queue;
use Mockery;
use PHPUnit\Framework\Attributes\Test;
use RuntimeException;
use Tests\TestCase;
use Throwable;

/**
 * The Prometheus exposition endpoint.
 *
 * Three things had gone wrong with `horizon_queue_depth` and only the first
 * two had been noticed:
 *
 *   1. The install guard used `class_exists()` on what is an INTERFACE, so
 *      it was always true and the metric emitted the string "Horizon not
 *      installed" instead of a sample -- forever, on an app where Horizon
 *      is a hard composer requirement.
 *   2. With that fixed, the queue list came from `horizon.defaults.queue`,
 *      which is not a path that exists: `defaults` is keyed by supervisor
 *      NAME. The lookup returned null and the `['default']` fallback won,
 *      so the metric watched exactly one queue -- and `llm`, the queue the
 *      code comment says the metric exists to watch and the one that
 *      actually backs up because a stalled stream holds its worker for the
 *      full job timeout, was the one it could not see.
 *   3. It read `Redis::connection('horizon')->llen("queues:{$queue}")`. The
 *      `horizon` connection carries Horizon's key prefix, not the queue's, so
 *      it counted a list that does not exist and reported 0 for every queue,
 *      always -- and a failed read was reported as 0 as well. It now asks the
 *      queue's own connection (MetricsEndpointRedisTest proves the key
 *      against a real Redis), and a queue it cannot read has no sample.
 *
 * Asserting against config rather than a literal list, so adding a
 * supervisor to config/horizon.php is covered without editing this test.
 */
final class MetricsEndpointTest extends TestCase
{
    private const SERVICE_KEY = 'metrics-test-service-key-at-least-32-bytes-long';

    /** @var list<string> "connection:queue" of every pendingSize() the controller asked for */
    private array $reads = [];

    protected function setUp(): void
    {
        parent::setUp();
        Config::set('services.fastapi.service_key', self::SERVICE_KEY);
    }

    #[Test]
    public function it_requires_the_service_key(): void
    {
        // The previous gate compared $request->ip() against RFC-1918
        // ranges, and ip() reads the client-supplied X-Forwarded-For chain,
        // so the whole metric set was readable by anyone willing to send
        // one header.
        $this->get('/metrics')->assertStatus(401);
        $this->withHeader('X-Service-Key', 'wrong')->get('/metrics')->assertStatus(401);
    }

    #[Test]
    public function it_reports_a_depth_for_every_configured_horizon_queue(): void
    {
        $depths = [];
        foreach ($this->configuredQueues() as $i => ['connection' => $connection, 'queue' => $queue]) {
            $depths["{$connection}:{$queue}"] = $i + 5;
        }
        $this->fakeQueueDepths($depths);

        $body = $this->scrape();

        foreach ($this->configuredQueues() as $i => ['queue' => $queue]) {
            $this->assertStringContainsString(
                'horizon_queue_depth{queue="'.$queue.'"} '.($i + 5)."\n",
                $body,
                "no horizon_queue_depth sample for the '{$queue}' queue",
            );
        }
    }

    #[Test]
    public function the_llm_queue_is_one_of_them_and_is_read_on_its_own_connection(): void
    {
        // Belt and braces on the test above: if config/horizon.php ever
        // loses supervisor-llm, configuredQueues() would stop asking for it
        // and that test would pass on an empty promise. The LLM stream job
        // sets $this->queue = 'llm' in its constructor regardless.
        //
        // And `llm` is consumed on the `redis-llm` queue connection (a longer
        // retry_after), not `redis`: the depth has to be read where its
        // workers pop from.
        $this->assertContains('llm', array_column($this->configuredQueues(), 'queue'));
        $this->fakeQueueDepths(['redis:default' => 7, 'redis-llm:llm' => 3]);

        $body = $this->scrape();

        $this->assertStringContainsString("horizon_queue_depth{queue=\"llm\"} 3\n", $body);
        $this->assertStringContainsString("horizon_queue_depth{queue=\"default\"} 7\n", $body);
        $this->assertContains('redis-llm:llm', $this->reads);
        $this->assertContains('redis:default', $this->reads);
        $this->assertNotContains('redis:llm', $this->reads, 'llm is not a queue of the shared connection');
    }

    #[Test]
    public function a_queue_that_cannot_be_read_has_no_sample_rather_than_a_zero(): void
    {
        // A failed read used to become 0, indistinguishable from an empty
        // queue and the one value an absent() alert can never catch.
        $this->fakeQueueDepths([
            'redis:default' => 7,
            'redis-llm:llm' => new RuntimeException('Connection refused'),
        ]);

        $body = $this->scrape();

        $this->assertStringContainsString("horizon_queue_depth{queue=\"default\"} 7\n", $body);
        $samples = array_filter(
            explode("\n", $body),
            fn (string $line): bool => str_starts_with($line, 'horizon_queue_depth{queue="llm"}'),
        );
        $this->assertSame([], array_values($samples), 'no sample for the unreadable queue');
        $this->assertStringContainsString(
            '# warning: horizon_queue_depth{queue="llm"} unavailable: Connection refused',
            $body,
        );
    }

    #[Test]
    public function an_error_message_cannot_forge_a_sample(): void
    {
        // The warning is a comment line. An exception message with a newline
        // would end the comment and the rest would parse as a sample.
        $this->fakeQueueDepths([
            'redis:default' => 7,
            'redis-llm:llm' => new RuntimeException("boom\nhorizon_queue_depth{queue=\"llm\"} 0"),
        ]);

        $lines = explode("\n", $this->scrape());

        $samples = array_filter($lines, fn (string $l): bool => str_starts_with($l, 'horizon_queue_depth{queue="llm"}'));
        $this->assertSame([], array_values($samples));
    }

    #[Test]
    public function it_does_not_claim_horizon_is_missing(): void
    {
        $this->fakeQueueDepths([]);

        $this->assertStringNotContainsString('Horizon not installed', $this->scrape());
    }

    #[Test]
    public function every_reason_the_application_writes_has_an_authz_deny_series(): void
    {
        // not_project_owner is what ProjectController writes on an owner-only
        // denial. It was counted in the cache and never exported, because the
        // list here was hand-kept and omitted it.
        $this->fakeQueueDepths([]);
        Cache::put('metrics:authz_deny:not_project_owner', 4);

        $body = $this->scrape();

        $this->assertStringContainsString("laravel_authz_deny_total{reason=\"not_project_owner\"} 4\n", $body);
        // Zero-filled, so the series exists before the first deny.
        $this->assertStringContainsString("laravel_authz_deny_total{reason=\"no_pivot_row\"} 0\n", $body);
        foreach (AuthorizationAuditLogger::REASONS as $reason) {
            $this->assertStringContainsString("laravel_authz_deny_total{reason=\"{$reason}\"} ", $body);
        }
    }

    #[Test]
    public function reasons_nothing_emits_are_not_exported(): void
    {
        $this->fakeQueueDepths([]);

        $body = $this->scrape();

        foreach (['cross_workspace', 'unauthenticated', 'cross_user', 'admin_only', 'none'] as $phantom) {
            $this->assertStringNotContainsString("reason=\"{$phantom}\"", $body);
        }
    }

    private function scrape(): string
    {
        $response = $this->withHeader('X-Service-Key', self::SERVICE_KEY)->get('/metrics');
        $response->assertOk();

        return $response->getContent() ?: '';
    }

    /**
     * Every queue any supervisor is configured to consume, with its queue
     * connection (a supervisor names none -> `redis`, as Horizon's stub does).
     *
     * @return list<array{connection: string, queue: string}>
     */
    private function configuredQueues(): array
    {
        $queues = [];
        foreach ((array) config('horizon.defaults', []) as $supervisor) {
            $connection = (string) ($supervisor['connection'] ?? 'redis');
            foreach ((array) ($supervisor['queue'] ?? []) as $queue) {
                $queues["{$connection}:{$queue}"] = ['connection' => $connection, 'queue' => (string) $queue];
            }
        }

        return array_values($queues);
    }

    /**
     * Answer the queue manager's connections from a table instead of a Redis
     * server: this suite has none. Each entry is the depth pendingSize()
     * returns for that "connection:queue", or the exception it throws.
     *
     * @param array<string, int|Throwable> $answers
     */
    private function fakeQueueDepths(array $answers): void
    {
        $this->reads = [];

        Queue::shouldReceive('connection')->andReturnUsing(function (string $connection) use ($answers): RedisQueue {
            $driver = Mockery::mock(RedisQueue::class);
            $driver->shouldReceive('pendingSize')->andReturnUsing(function (string $queue) use ($connection, $answers): int {
                $this->reads[] = "{$connection}:{$queue}";
                $answer = $answers["{$connection}:{$queue}"] ?? throw new RuntimeException("unexpected read of {$connection}:{$queue}");
                if ($answer instanceof Throwable) {
                    throw $answer;
                }

                return $answer;
            });

            return $driver;
        });
    }
}
