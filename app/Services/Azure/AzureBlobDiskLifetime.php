<?php

declare(strict_types=1);

namespace App\Services\Azure;

use Illuminate\Support\Facades\Log;
use Illuminate\Support\Facades\Storage;

/**
 * Drops cached Azure blob disks once the token baked into them expires.
 *
 * `Storage::extend('azure', …)` builds a BlobRestProxy from a bearer token
 * fetched at that moment. Laravel's FilesystemManager then caches the
 * resolved disk in its own `$disks` array, and under Octane that manager is
 * a singleton living for the life of the worker process — `config/octane.php`
 * flushes nothing, so nothing re-resolves it.
 *
 * The result is that ManagedIdentityTokenProvider's careful Redis cache,
 * with its five-minute early refresh, is asked for a token exactly once per
 * worker. The token it hands back is copied into the SDK client and held
 * there. microsoft/azure-storage-blob ^1.1 takes the bearer as a
 * constructor string with no setter and no callback, so the client cannot
 * pick up a newer one; when the original expires, every blob call from that
 * worker returns 401 — uploads, report exports, figure downloads — until
 * the worker happens to recycle. With OCTANE_MAX_REQUESTS=500 on an app
 * this quiet, that can be days.
 *
 * So the disks have to be thrown away and rebuilt. This class records when
 * the token dies and forgets them at that point; the next resolution goes
 * back through Storage::extend and picks up whatever token Redis now holds.
 *
 * The expiry is tracked ONCE rather than per disk, because there is one
 * token: s3, s3-bronze and s3-exports all authenticate with the same
 * managed identity against the same storage resource, so they go stale
 * together. Tracking them separately would imply a distinction that does
 * not exist — and the Storage::extend closure is handed only `$config`,
 * never the disk name, so per-disk bookkeeping would have to invent one.
 *
 * On the Octane note about static state: this holds a single nullable int
 * and cannot accumulate. It is per-worker, which is the correct scope,
 * because the FilesystemManager it shadows is per-worker too.
 */
final class AzureBlobDiskLifetime
{
    /** Unix timestamp at which the token baked into the cached disks dies. */
    private static ?int $expiresAt = null;

    /**
     * Record the expiry of the token a freshly built disk just captured.
     *
     * The earliest expiry wins. In practice every disk built in a given
     * worker captures the same cached token and therefore the same value,
     * but if a rebuild straddles a refresh the conservative bound is the
     * one that keeps a stale client from surviving.
     */
    public static function remember(int $expiresAt): void
    {
        self::$expiresAt = self::$expiresAt === null
            ? $expiresAt
            : min(self::$expiresAt, $expiresAt);
    }

    /**
     * Forget every cached azure-driver disk if the shared token has expired.
     *
     * Called once per request from the Octane RequestReceived listener. The
     * common path is a single integer comparison — no cache round-trip, no
     * I/O — so it is cheap enough to run unconditionally. The expensive
     * part (fetching a token, rebuilding clients) happens only on the rare
     * request that crosses the expiry boundary, and only for disks that
     * request actually touches.
     *
     * @return list<string> the disks that were forgotten
     */
    public static function purgeExpired(?int $now = null): array
    {
        if (self::$expiresAt === null) {
            return [];
        }

        if (self::$expiresAt > ($now ?? time())) {
            return [];
        }

        self::$expiresAt = null;

        $forgotten = [];
        foreach (self::azureDiskNames() as $disk) {
            try {
                Storage::forgetDisk($disk);
                $forgotten[] = $disk;
            } catch (\Throwable $e) {
                // A disk that cannot be forgotten is not a reason to fail
                // the request; it keeps serving 401s, which is what it was
                // already doing.
                Log::warning('AzureBlobDiskLifetime: could not forget disk', [
                    'disk' => $disk,
                    'exception' => $e->getMessage(),
                ]);
            }
        }

        if ($forgotten !== []) {
            Log::info(
                'AzureBlobDiskLifetime: managed-identity token expired, '
                .'rebuilding blob disks on next use.',
                ['disks' => $forgotten],
            );
        }

        return $forgotten;
    }

    /**
     * Every disk configured with the azure driver.
     *
     * Read from config rather than hardcoded so adding a fourth blob disk
     * does not silently leave it holding a dead token.
     *
     * @return list<string>
     */
    public static function azureDiskNames(): array
    {
        $disks = config('filesystems.disks', []);
        if (! is_array($disks)) {
            return [];
        }

        $names = [];
        foreach ($disks as $name => $config) {
            if (is_array($config) && ($config['driver'] ?? null) === 'azure') {
                $names[] = (string) $name;
            }
        }

        return $names;
    }

    /** The tracked expiry, or null when nothing is being tracked. */
    public static function expiresAt(): ?int
    {
        return self::$expiresAt;
    }

    /** Clear the registry. For tests and for worker restarts. */
    public static function reset(): void
    {
        self::$expiresAt = null;
    }
}
