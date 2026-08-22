<?php

declare(strict_types=1);

namespace App\Services\Azure;

/**
 * Octane RequestReceived listener — drop Azure blob disks whose managed
 * identity token has expired.
 *
 * Registered in config/octane.php. See {@see AzureBlobDiskLifetime} for why
 * a long-lived Octane worker otherwise serves 401s from a stale token until
 * it recycles.
 *
 * The work is a few integer comparisons on the common path. It runs on
 * RequestReceived rather than on a tick so that a request never begins with
 * a disk already known to be dead — a tick would leave a window between
 * expiry and the next tick in which requests use the stale client.
 */
final class RefreshExpiredAzureDisks
{
    /**
     * Handle the event.
     *
     * @param mixed $event Laravel\Octane\Events\RequestReceived
     */
    public function handle($event): void
    {
        AzureBlobDiskLifetime::purgeExpired();
    }
}
