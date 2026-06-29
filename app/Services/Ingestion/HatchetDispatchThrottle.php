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
 * window; the rest spin in 100ms increments until the key expires or
 * the 30s safety cap is hit. A safety-cap hit logs + falls through
 * (fail open) — better to dispatch and possibly cancel than to deadlock
 * an Octane worker indefinitely.
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

    /** Hard ceiling on time spent waiting so we don't pin an Octane worker. */
    public const MAX_WAIT_MS = 30_000;

    /** Spin interval while waiting for the sentinel to expire. */
    public const POLL_INTERVAL_MS = 100;

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
        $ms = $throttleMs ?? self::DEFAULT_THROTTLE_MS;
        if ($ms <= 0 || $workspaceId === '') {
            return;
        }

        $key = "hatchet:dispatch-throttle:{$workspaceId}";
        $ttlSeconds = max(1, (int) ceil($ms / 1000));
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
            if ($waitedMs >= self::MAX_WAIT_MS) {
                // Hatchet's queue-saturation cancel-window is much longer
                // than 30s; this branch only trips when something is wedged
                // (cache stuck, clock skew). Better to dispatch and risk a
                // single CANCELLED than to indefinitely pin the worker.
                Log::warning('HatchetDispatchThrottle: max wait exceeded, failing open', [
                    'workspace_id' => $workspaceId,
                    'waited_ms' => $waitedMs,
                    'throttle_ms' => $ms,
                ]);

                return;
            }
            usleep(self::POLL_INTERVAL_MS * 1000);
            $waitedMs += self::POLL_INTERVAL_MS;
        }
    }
}
