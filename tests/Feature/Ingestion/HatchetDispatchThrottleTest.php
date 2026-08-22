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

    /**
     * The safety cap was a flat 30 seconds against a sentinel that expires
     * after one, and the wait is a usleep inside an Octane worker. Octane
     * serves from a fixed worker pool, so a bulk import in a single
     * workspace could park every worker here and the application would
     * answer nothing at all — including requests unrelated to uploads.
     */
    public function test_the_safety_cap_is_derived_from_the_window(): void
    {
        // ceil(250/1000) = 1s of sentinel, plus two poll intervals of slack.
        $this->assertSame(1_200, HatchetDispatchThrottle::maxWaitMsFor(250));
        $this->assertSame(1_200, HatchetDispatchThrottle::maxWaitMsFor(1_000));
        $this->assertSame(2_200, HatchetDispatchThrottle::maxWaitMsFor(2_000));
    }

    public function test_an_absurd_window_still_hits_the_absolute_ceiling(): void
    {
        $this->assertSame(
            HatchetDispatchThrottle::MAX_WAIT_MS,
            HatchetDispatchThrottle::maxWaitMsFor(600_000),
        );
    }

    public function test_a_wedged_sentinel_releases_the_worker_in_about_a_second(): void
    {
        // A sentinel that will not expire during the test: the wedged-cache
        // case the fail-open branch exists for. Before the cap was derived,
        // this call held the worker for the full 30 seconds.
        Cache::store('array')->flush();
        Cache::store('array')->put('hatchet:dispatch-throttle:ws-wedged', '1', 600);

        $throttle = $this->throttle_without_flush();

        $start = microtime(true);
        $throttle->wait('ws-wedged', 250);
        $elapsedMs = (microtime(true) - $start) * 1000;

        $this->assertGreaterThanOrEqual(
            1_000,
            $elapsedMs,
            'It should still wait out the window before failing open.',
        );
        $this->assertLessThan(
            3_000,
            $elapsedMs,
            'It must not hold the worker anywhere near the 30s absolute ceiling.',
        );
    }

    /** Same stub as throttle(), without the flush that would clear the sentinel. */
    private function throttle_without_flush(): HatchetDispatchThrottle
    {
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
