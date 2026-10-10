<?php

declare(strict_types=1);

namespace Tests\Feature;

use Illuminate\Support\Facades\Config;
use Illuminate\Support\Facades\Queue;
use Illuminate\Support\Facades\Redis;
use Illuminate\Support\Str;
use Tests\TestCase;
use Throwable;

/**
 * `horizon_queue_depth` against a real Redis: the part MetricsEndpointTest
 * cannot show, which is which KEY gets counted.
 *
 * It counted `llen("queues:{$queue}")` on the `horizon` Redis connection. That
 * connection's key prefix is horizon.prefix, not the queue's, so it read
 * `<horizon prefix>queues:default` -- a list nobody pushes to -- and the gauge
 * was 0 for every queue whatever was waiting.
 *
 * Skips when no Redis is reachable (the SQLite suite has none); the Postgres
 * suite's CI job has one.
 */
final class MetricsEndpointRedisTest extends TestCase
{
    private const SERVICE_KEY = 'metrics-redis-test-service-key-32-bytes-long';

    private string $queue;

    private string $redisConnection;

    protected function setUp(): void
    {
        parent::setUp();

        // The Redis connection the `redis` queue connection pushes to and the
        // workers pop from.
        $this->redisConnection = (string) config('queue.connections.redis.connection', 'default');

        try {
            Redis::connection($this->redisConnection)->ping();
        } catch (Throwable $e) {
            $this->markTestSkipped('Needs a reachable Redis: '.$e->getMessage());
        }

        // A queue of this test's own, so nothing else on a shared Redis moves.
        $this->queue = 'metrics-test-'.Str::lower(Str::random(10));

        Config::set('services.fastapi.service_key', self::SERVICE_KEY);
        Config::set('horizon.defaults', [
            'supervisor-test' => ['connection' => 'redis', 'queue' => [$this->queue]],
        ]);
        Config::set('horizon.environments', []);
    }

    protected function tearDown(): void
    {
        if (isset($this->queue)) {
            try {
                Redis::connection($this->redisConnection)->del("queues:{$this->queue}");
            } catch (Throwable) {
                // Redis went away; nothing left to clean.
            }
        }

        parent::tearDown();
    }

    public function test_it_counts_the_list_the_workers_pop_from(): void
    {
        // Pushed onto the queue connection's own Redis connection, so under its
        // prefix -- which is where a worker (and the queue's own size calls)
        // looks.
        Redis::connection($this->redisConnection)->rpush("queues:{$this->queue}", 'job-a', 'job-b', 'job-c');

        $this->assertSame(
            3,
            Queue::connection('redis')->pendingSize($this->queue),
            'precondition: this is the list the queue itself sees',
        );

        $body = $this->withHeader('X-Service-Key', self::SERVICE_KEY)->get('/metrics')->assertOk()->getContent();

        $this->assertStringContainsString('horizon_queue_depth{queue="'.$this->queue."\"} 3\n", (string) $body);
    }

    public function test_an_empty_queue_is_a_real_zero(): void
    {
        $body = $this->withHeader('X-Service-Key', self::SERVICE_KEY)->get('/metrics')->assertOk()->getContent();

        $this->assertStringContainsString('horizon_queue_depth{queue="'.$this->queue."\"} 0\n", (string) $body);
    }
}
