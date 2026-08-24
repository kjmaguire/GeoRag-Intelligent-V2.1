<?php

declare(strict_types=1);

namespace App\Services\Ingestion;

use Illuminate\Contracts\Cache\Factory as CacheFactory;
use Illuminate\Support\Facades\Log;

/**
 * Per-workspace throttle for Laravel→FastAPI Hatchet trigger dispatch.
 *
 * Background — Cameco recovery 2026-06-02
 * ----------------------------------------
 * The ingest_pdf workflow is configured with a per-workspace
 * concurrency cap (`max_runs=1`, `GROUP_ROUND_ROBIN`) so the docling
 * + PaddleOCR models don't OOM the worker. When uploads burst-fire at
 * the trigger endpoint faster than Hatchet can queue them, runs above
 * the queue-depth threshold get silently CANCELLED before the preflight
 * task ever writes a `silver.ingest_progress` row. The 2026-06-01
 * Cameco batch lost 529 files this way (~41% of the burst).
 *
 * The artisan recovery command `ingest:reingest-project` already
 * throttles its trigger loop via `--throttle-ms` (default 2000ms). The
 * upload path didn't, so any bulk upload could repeat the same
 * cancellation pattern. This service mirrors the artisan throttle for
 * the web path.
 *
 * Mechanism
 * ---------
 * Single Redis (or whatever {@see CacheFactory} resolves to) sentinel
 * key per workspace, written with a TTL equal to the throttle window.
 * `Cache::add()` is atomic — only one caller per workspace wins per
 * window; the rest spin in 100ms increments until the key expires or the
 * safety cap is hit. A safety-cap hit logs + falls through (fail open) —
 * better to dispatch and possibly cancel than to deadlock an Octane
 * worker indefinitely.
 *
 * The safety cap is DERIVED from the window ({@see maxWaitMsFor}), not
 * configured separately. It was a flat 30 seconds against a sentinel that
 * expires after one, and the wait is a `usleep` inside an Octane worker.
 * Octane serves from a fixed worker pool, so a bulk import in a SINGLE
 * workspace could park every worker in this loop — and while they are
 * parked the application answers nothing at all, including requests that
 * have nothing to do with uploads. A throttle that smooths an ingestion
 * queue must not be able to take the web tier down with it.
 *
 * The sentinel TTL is rounded up to whole seconds because every backing
 * cache driver (database, file, array, even Redis via the Illuminate
 * Cache repository) coerces TTL to seconds. Sub-second precision isn't
 * needed — the goal is to keep concurrent dispatches at most one per
 * window, not to enforce a precise inter-arrival time.
 *
 * Wait is best-effort: any unexpected cache exception is logged and
 * swallowed so we never block the user's upload response on cache
 * plumbing.
 */
class HatchetDispatchThrottle
{
    /** Default window — matches `ingest:reingest-project --throttle-ms`. */
    public const DEFAULT_THROTTLE_MS = 2000;

    /**
     * Effective default, env-tunable. The 2000ms constant was sized for
     * 500-file bulk replays; on the interactive upload path it pins an
     * Octane worker in usleep for >=2s per file (uploads are sequential
     * in the wizard, so a 20-file import spends 40s just waiting). 250ms
     * still smooths Hatchet's GROUP_ROUND_ROBIN intake.
     *
     * Note the granularity before tuning this: the sentinel TTL is
     * `ceil($ms / 1000)` seconds because every cache driver coerces TTL to
     * whole seconds, so every value from 1 through 1000 produces the same
     * one-second window. Dropping 250 to 150 changes nothing; the next
     * distinct setting up is 1001.
     */
    public static function defaultThrottleMs(): int
    {
        return (int) config('services.hatchet.dispatch_throttle_ms', 250);
    }

    /** Absolute ceiling, whatever the window. See {@see maxWaitMsFor}. */
    public const MAX_WAIT_MS = 30_000;

    /** Spin interval while waiting for the sentinel to expire. */
    public const POLL_INTERVAL_MS = 100;

    /**
     * Longest wait this mechanism can legitimately produce for a window.
     *
     * A sentinel cannot outlive its own TTL, so waiting appreciably longer
     * than that TTL does not mean "still busy" — it means the cache is
     * wedged or the clock has skewed, which is precisely what the fail-open
     * branch in {@see wait()} exists for. Deriving the cap from the window
     * keeps the two from drifting apart: MAX_WAIT_MS was thirty times the
     * longest wait the default window can produce, and every millisecond of
     * that overshoot was an Octane worker held out of the pool.
     */
    public static function maxWaitMsFor(int $throttleMs): int
    {
        $ttlMs = max(1, (int) ceil($throttleMs / 1000)) * 1000;

        return min(self::MAX_WAIT_MS, $ttlMs + (self::POLL_INTERVAL_MS * 2));
    }

    /**
     * Resolver, not the repository itself — Octane safety: the underlying
     * Redis connection in the cache repository is managed by the cache
     * manager and can be reset between requests. Resolving each wait()
     * call avoids holding a stale connection across the request boundary.
     * See CLAUDE.md Hard Rule 3 + the Octane guidelines.
     */
    public function __construct(
        private readonly CacheFactory $cacheFactory,
    ) {}

    /**
     * Block until the workspace's throttle slot is free, then claim it.
     *
     * Returns once the caller may safely fire the Hatchet dispatch.
     * Never throws — every cache failure path falls through with a log.
     */
    public function wait(string $workspaceId, ?int $throttleMs = null): void
    {
        $ms = $throttleMs ?? self::defaultThrottleMs();
        if ($ms <= 0 || $workspaceId === '') {
            return;
        }

        $key = "hatchet:dispatch-throttle:{$workspaceId}";
        $ttlSeconds = max(1, (int) ceil($ms / 1000));
        $maxWaitMs = self::maxWaitMsFor($ms);
        $waitedMs = 0;
        $cache = $this->cacheFactory->store();

        while (true) {
            try {
                $claimed = $cache->add($key, '1', $ttlSeconds);
            } catch (\Throwable $e) {
                Log::warning('HatchetDispatchThrottle: cache add failed, failing open', [
                    'workspace_id' => $workspaceId,
                    'error' => $e->getMessage(),
                ]);

                return;
            }
            if ($claimed) {
                return;
            }
            if ($waitedMs >= $maxWaitMs) {
                // Past the sentinel's own TTL, so this is not contention —
                // it is a wedged cache or a skewed clock. Hatchet's
                // queue-saturation cancel window is far longer than one
                // dispatch, so risking a single CANCELLED beats holding an
                // Octane worker out of the pool any longer.
                Log::warning('HatchetDispatchThrottle: max wait exceeded, failing open', [
                    'workspace_id' => $workspaceId,
                    'waited_ms' => $waitedMs,
                    'max_wait_ms' => $maxWaitMs,
                    'throttle_ms' => $ms,
                ]);

                return;
            }
            usleep(self::POLL_INTERVAL_MS * 1000);
            $waitedMs += self::POLL_INTERVAL_MS;
        }
    }
}
