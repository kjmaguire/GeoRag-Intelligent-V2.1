<?php

declare(strict_types=1);

namespace App\Services;

use RuntimeException;
use Throwable;

/**
 * A HatchetWorkflowTrigger failure, carrying the HTTP status the Laravel
 * caller should answer with: FastAPI's own 404/422 passed through (a resource
 * outside the workspace, an input it refused), 502 for an unreachable or
 * failing FastAPI, 500 for missing configuration.
 */
final class HatchetWorkflowTriggerException extends RuntimeException
{
    public function __construct(string $message, public readonly int $status, ?Throwable $previous = null)
    {
        parent::__construct($message, 0, $previous);
    }
}
