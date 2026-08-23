<?php

declare(strict_types=1);

namespace Tests\Feature;

use Illuminate\Support\Facades\Config;
use PHPUnit\Framework\Attributes\Test;
use Tests\TestCase;

/**
 * The Prometheus exposition endpoint.
 *
 * Two things had gone wrong with `horizon_queue_depth` and only the first
 * had been noticed:
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
 *
 * Asserting against config rather than a literal list, so adding a
 * supervisor to config/horizon.php is covered without editing this test.
 */
final class MetricsEndpointTest extends TestCase
{
    private const SERVICE_KEY = 'metrics-test-service-key-at-least-32-bytes-long';

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
        $body = $this->scrape();

        foreach ($this->configuredQueues() as $queue) {
            $this->assertStringContainsString(
                'horizon_queue_depth{queue="'.$queue.'"}',
                $body,
                "no horizon_queue_depth sample for the '{$queue}' queue",
            );
        }
    }

    #[Test]
    public function the_llm_queue_is_one_of_them(): void
    {
        // Belt and braces on the test above: if config/horizon.php ever
        // loses supervisor-llm, configuredQueues() would stop asking for it
        // and that test would pass on an empty promise. The LLM stream job
        // sets $this->queue = 'llm' in its constructor regardless.
        $this->assertContains('llm', $this->configuredQueues());
        $this->assertStringContainsString('horizon_queue_depth{queue="llm"}', $this->scrape());
    }

    #[Test]
    public function it_does_not_claim_horizon_is_missing(): void
    {
        $this->assertStringNotContainsString('Horizon not installed', $this->scrape());
    }

    private function scrape(): string
    {
        $response = $this->withHeader('X-Service-Key', self::SERVICE_KEY)->get('/metrics');
        $response->assertOk();

        return $response->getContent() ?: '';
    }

    /**
     * Every queue any supervisor is configured to consume.
     *
     * @return list<string>
     */
    private function configuredQueues(): array
    {
        $queues = [];
        foreach ((array) config('horizon.defaults', []) as $supervisor) {
            foreach ((array) ($supervisor['queue'] ?? []) as $queue) {
                $queues[] = (string) $queue;
            }
        }

        return array_values(array_unique($queues));
    }
}
