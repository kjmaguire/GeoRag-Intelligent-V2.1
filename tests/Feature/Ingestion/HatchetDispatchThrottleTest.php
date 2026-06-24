<?php

declare(strict_types=1);

namespace Tests\Feature\Ingestion;

use App\Services\Ingestion\HatchetDispatchThrottle;
use Illuminate\Contracts\Cache\Factory as CacheFactory;
use Illuminate\Contracts\Cache\Repository;
use Illuminate\Support\Facades\Cache;
use Tests\TestCase;

/**
 * Locks the contract for the per-workspace Hatchet dispatch throttle
 * introduced after the Cameco 2026-06-02 recovery.
 *
 * The throttle's job is to serialise concurrent uploads in a single
 * workspace so a bulk burst can't saturate Hatchet's per-workspace
 * GROUP_ROUND_ROBIN queue (max_runs=1) and lose the tail to silent
 * CANCELLED events.
 *
 * Uses the array cache store via swap() so the tests are deterministic
 * and don't depend on a live Redis. The behaviour is the same — both
 * stores implement the atomic add() semantics the throttle relies on.
 */
class HatchetDispatchThrottleTest extends TestCase
{
    private function throttle(): HatchetDispatchThrottle
    {
        // Use the array store explicitly — tests must not touch the
        // configured Redis cache (would leak sentinels between tests).
        Cache::store('array')->flush();
        $factoryStub = new class(Cache::store('array')) implements CacheFactory
        {
            public function __construct(private readonly Repository $repo) {}

            public function store($name = null)
            {
                return $this->repo;
            }
        };

        return new HatchetDispatchThrottle($factoryStub);
    }

    public function test_first_call_for_a_workspace_returns_immediately(): void
    {
        $throttle = $this->throttle();
        $start = microtime(true);
        $throttle->wait('ws-a', 500);
        $elapsedMs = (microtime(true) - $start) * 1000;

        $this->assertLessThan(
            100,
            $elapsedMs,
            'First wait should not sleep — the sentinel did not exist yet.',
        );
    }

    public function test_second_call_within_window_blocks(): void
    {
        $throttle = $this->throttle();
        // Claim the slot.
        $throttle->wait('ws-b', 500);

        $start = microtime(true);
        $throttle->wait('ws-b', 500);
        $elapsedMs = (microtime(true) - $start) * 1000;

        // The throttle TTL is rounded up to whole seconds (min 1s), so the
        // sentinel for ws-b will live for ~1 second after the first call.
        $this->assertGreaterThanOrEqual(
            400,
            $elapsedMs,
            'Second wait should sleep until the sentinel TTL expires.',
        );
    }

    public function test_different_workspaces_do_not_block_each_other(): void
    {
        $throttle = $this->throttle();
        $throttle->wait('ws-c', 5000);

        $start = microtime(true);
        $throttle->wait('ws-d', 5000);
        $elapsedMs = (microtime(true) - $start) * 1000;

        $this->assertLessThan(
            100,
            $elapsedMs,
            'A different workspace should never wait on another workspace.',
        );
    }

    public function test_zero_throttle_is_a_noop(): void
    {
        $throttle = $this->throttle();
        $start = microtime(true);
        $throttle->wait('ws-e', 0);
        $throttle->wait('ws-e', 0);
        $elapsedMs = (microtime(true) - $start) * 1000;

        $this->assertLessThan(
            50,
            $elapsedMs,
            'Throttle of 0ms must be a strict no-op.',
        );
    }

    public function test_empty_workspace_is_a_noop(): void
    {
        $throttle = $this->throttle();
        $start = microtime(true);
        $throttle->wait('', 2000);
        $elapsedMs = (microtime(true) - $start) * 1000;

        $this->assertLessThan(
            50,
            $elapsedMs,
            'Empty workspace_id should short-circuit so anonymous flows never hang.',
        );
    }
}
