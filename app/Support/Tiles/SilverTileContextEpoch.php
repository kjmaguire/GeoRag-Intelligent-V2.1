<?php

declare(strict_types=1);

namespace App\Support\Tiles;

use Illuminate\Support\Facades\Cache;
use Illuminate\Support\Facades\Log;
use Throwable;

/**
 * Per-project epoch that namespaces the silver tile proxy's cached
 * (user, project) contexts.
 *
 * TileProxyController caches a user's membership, the project's workspace and
 * its data_version for 60 s. Advancing the epoch orphans every user's cached
 * context for that project at once, without enumerating users: the next tile
 * reads the new epoch, misses, and re-reads data_version. The ingestion
 * data_version bump calls advance() right after it commits, so a new ETag is
 * served immediately rather than up to 60 s late.
 *
 * Octane: stateless. The epoch lives in the cache, never in the process.
 */
final class SilverTileContextEpoch
{
    /** Longer than the context TTL, so an expired epoch can never resurrect an older context. */
    private const TTL_SECONDS = 3600;

    private static function key(string $projectId): string
    {
        return "silver-tile-ctx-epoch:{$projectId}";
    }

    /** The current epoch token for the project ('0' before any bump). */
    public static function current(string $projectId): string
    {
        return (string) Cache::get(self::key($projectId), '0');
    }

    /**
     * Invalidate every cached silver tile context for the project. Never
     * throws: a cache outage must not fail the data_version bump that
     * triggered it (the 60 s context TTL is the fallback).
     */
    public static function advance(string $projectId): void
    {
        try {
            Cache::put(self::key($projectId), bin2hex(random_bytes(6)), self::TTL_SECONDS);
        } catch (Throwable $e) {
            Log::warning('Silver tile context invalidation failed', [
                'project_id' => $projectId,
                'error' => $e->getMessage(),
            ]);
        }
    }
}
