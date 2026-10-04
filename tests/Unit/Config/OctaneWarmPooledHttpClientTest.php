<?php

declare(strict_types=1);

namespace Tests\Unit\Config;

use App\Support\Http\PooledHttpClient;
use Illuminate\Http\Client\Factory;
use ReflectionClass;
use ReflectionNamedType;
use Tests\TestCase;

/**
 * PooledHttpClient is a singleton whose whole value is surviving between
 * requests, which under Octane only happens for instances resolved at worker
 * boot. It must therefore be listed in octane.warm, and it must hold nothing
 * that belongs to one request.
 */
final class OctaneWarmPooledHttpClientTest extends TestCase
{
    public function test_the_pool_is_warmed_at_worker_boot(): void
    {
        $this->assertContains(PooledHttpClient::class, config('octane.warm'));
    }

    public function test_the_pool_is_a_container_singleton(): void
    {
        $this->assertSame(app(PooledHttpClient::class), app(PooledHttpClient::class));
    }

    public function test_the_pool_holds_no_request_scoped_state(): void
    {
        $allowed = [
            // Bounded by MAX_CLIENTS with LRU eviction; keyed by base URL.
            'clients' => 'array',
            'lastUsed' => 'array',
            'tick' => 'int',
            // The stateless HTTP factory; every call builds a fresh PendingRequest.
            'factory' => Factory::class,
        ];

        $actual = [];
        foreach ((new ReflectionClass(PooledHttpClient::class))->getProperties() as $property) {
            $type = $property->getType();
            $actual[$property->getName()] = $type instanceof ReflectionNamedType ? $type->getName() : 'mixed';
            $this->assertFalse($property->isStatic(), "{$property->getName()} must not be static");
        }

        $this->assertSame($allowed, $actual, 'a new property on the pool needs an Octane-safety review');
    }
}
