<?php

declare(strict_types=1);

namespace App\Services;

use RuntimeException;
use Throwable;

/**
 * A PublicGeoSyncTrigger failure, carrying the HTTP status the Laravel caller
 * should answer with (422 for a filter FastAPI rejected, 502 for an
 * unreachable or failing FastAPI, 500 for missing configuration).
 */
final class PublicGeoSyncTriggerException extends RuntimeException
{
    public function __construct(string $message, public readonly int $status, ?Throwable $previous = null)
    {
        parent::__construct($message, 0, $previous);
    }
}
