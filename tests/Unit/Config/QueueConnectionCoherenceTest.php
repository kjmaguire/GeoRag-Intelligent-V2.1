<?php

declare(strict_types=1);

namespace Tests\Unit\Config;

use Tests\TestCase;

/**
 * LAR-17 (2026-09-29 audit): the three layers of queue naming must line up.
 *
 *   Horizon supervisor `connection`  → a key in config/queue.php connections
 *   that queue connection's `connection` → a key in config/database.php redis
 *
 * .env.example told operators to set HORIZON_REDIS_CONNECTION=queue, which
 * points the first arrow at a name that exists only at the third level;
 * Horizon then refuses to start ("queue connection [queue] not configured").
 * This pins the shape with the shipped defaults, and pins that the
 * dedicated `queue` Redis connection the docs describe is reachable through
 * the documented switch (REDIS_QUEUE_CONNECTION=queue).
 */
final class QueueConnectionCoherenceTest extends TestCase
{
    public function test_every_horizon_supervisor_names_a_configured_redis_queue_connection(): void
    {
        $supervisors = (array) config('horizon.defaults');
        $this->assertNotEmpty($supervisors);

        foreach ($supervisors as $name => $supervisor) {
            $queueConnection = (string) ($supervisor['connection'] ?? '');

            $this->assertNotNull(
                config("queue.connections.{$queueConnection}"),
                "horizon.defaults.{$name}.connection = '{$queueConnection}' is not a key of config/queue.php connections",
            );
            $this->assertSame('redis', config("queue.connections.{$queueConnection}.driver"), "{$name} must use a redis queue connection");

            $redisConnection = (string) config("queue.connections.{$queueConnection}.connection");
            $this->assertNotNull(
                config("database.redis.{$redisConnection}"),
                "queue.connections.{$queueConnection}.connection = '{$redisConnection}' is not a Redis connection",
            );
        }
    }

    public function test_the_dedicated_queue_redis_connection_exists_for_the_documented_switch(): void
    {
        $this->assertIsArray(config('database.redis.queue'));
        $this->assertNull(
            config('queue.connections.queue'),
            '`queue` is a Redis connection name, never a queue connection name — '
            .'HORIZON_REDIS_CONNECTION=queue would stop Horizon',
        );
    }

    public function test_the_env_template_no_longer_recommends_horizon_redis_connection_queue(): void
    {
        $template = (string) file_get_contents(base_path('.env.example'));

        $this->assertDoesNotMatchRegularExpression('/^\s*#?\s*HORIZON_REDIS_CONNECTION=queue\s*$/m', $template);
        $this->assertMatchesRegularExpression('/^\s*#?\s*REDIS_QUEUE_CONNECTION=queue\s*$/m', $template);
    }
}
