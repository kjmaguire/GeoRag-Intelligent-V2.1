<?php

declare(strict_types=1);

namespace Tests\Unit\Support\Http;

use App\Support\Http\PooledHttpClient;
use GuzzleHttp\Client as GuzzleClient;
use Illuminate\Http\Client\Factory as HttpFactory;
use PHPUnit\Framework\TestCase;
use ReflectionClass;

/**
 * Unit tests for PooledHttpClient.
 *
 * Asserts that the pool keeps one Guzzle\Client per base URL, that
 * subsequent requests for the same base URL reuse the same client
 * (i.e. the curl handle survives), and that the LRU eviction bound
 * holds so the pool can't grow without limit.
 */
final class PooledHttpClientTest extends TestCase
{
    private function pool(): PooledHttpClient
    {
        return new PooledHttpClient(new HttpFactory);
    }

    private function clientsArray(PooledHttpClient $pool): array
    {
        $ref = new ReflectionClass($pool);
        $prop = $ref->getProperty('clients');

        return $prop->getValue($pool);
    }

    public function test_reuses_same_guzzle_client_for_same_base_url(): void
    {
        $pool = $this->pool();

        $a = $pool->forBaseUrl('http://martin:3000');
        $b = $pool->forBaseUrl('http://martin:3000');

        // PendingRequests differ (each is a fresh chain wrapper), but the
        // underlying Guzzle client is the same instance — pulled from the pool.
        $clients = $this->clientsArray($pool);
        $this->assertArrayHasKey('http://martin:3000', $clients);
        $this->assertInstanceOf(GuzzleClient::class, $clients['http://martin:3000']);
        $this->assertCount(1, $clients);
    }

    public function test_separate_base_urls_keep_separate_clients(): void
    {
        $pool = $this->pool();

        $pool->forBaseUrl('http://martin:3000');
        $pool->forBaseUrl('http://fastapi:8000');
        $pool->forBaseUrl('http://qdrant:6333');

        $clients = $this->clientsArray($pool);
        $this->assertCount(3, $clients);
        $this->assertSame(
            ['http://martin:3000', 'http://fastapi:8000', 'http://qdrant:6333'],
            array_keys($clients),
        );
    }

    public function test_lru_eviction_bounds_the_pool(): void
    {
        $pool = $this->pool();

        // MAX_CLIENTS = 16. Use 18 distinct URLs to force two evictions.
        for ($i = 0; $i < 18; $i++) {
            $pool->forBaseUrl("http://service-{$i}.example:8080");
        }

        $clients = $this->clientsArray($pool);
        $this->assertSame(16, count($clients), 'pool must respect MAX_CLIENTS');

        // The first two URLs should have been evicted as LRU.
        $this->assertArrayNotHasKey('http://service-0.example:8080', $clients);
        $this->assertArrayNotHasKey('http://service-1.example:8080', $clients);
        // The most recently used should still be present.
        $this->assertArrayHasKey('http://service-17.example:8080', $clients);
    }

    public function test_recently_touched_url_survives_eviction(): void
    {
        $pool = $this->pool();

        $pool->forBaseUrl('http://hot.example:80');
        // Fill the rest of the pool.
        for ($i = 0; $i < 15; $i++) {
            $pool->forBaseUrl("http://cold-{$i}.example:80");
        }
        // Touch the hot one again so it's MRU.
        $pool->forBaseUrl('http://hot.example:80');
        // Adding one more must evict an LRU entry, not the hot one.
        $pool->forBaseUrl('http://new.example:80');

        $clients = $this->clientsArray($pool);
        $this->assertArrayHasKey('http://hot.example:80', $clients);
        $this->assertArrayHasKey('http://new.example:80', $clients);
        $this->assertArrayNotHasKey('http://cold-0.example:80', $clients);
    }

    public function test_returns_a_pending_request_with_base_url_and_timeout(): void
    {
        $pool = $this->pool();
        $pending = $pool->forBaseUrl('http://martin:3000', 7);

        $ref = new ReflectionClass($pending);

        // baseUrl is stored as a protected property on PendingRequest.
        $baseUrlProp = $ref->getProperty('baseUrl');
        $this->assertSame('http://martin:3000', $baseUrlProp->getValue($pending));

        // Timeout (in seconds) maps to the Guzzle options on the pending
        // request; surfaced via the protected $options array.
        $optionsProp = $ref->getProperty('options');
        $opts = $optionsProp->getValue($pending);
        $this->assertSame(7, $opts['timeout'] ?? null);
    }
}
