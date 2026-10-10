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
 *
 * `$maybeDispatched` is true when the request may have reached FastAPI and
 * started a run whose reply was lost (a read timeout, a reset after the
 * request was written). The caller must not treat that like a refusal: a
 * retry could start a second run.
 */
final class HatchetWorkflowTriggerException extends RuntimeException
{
    public function __construct(
        string $message,
        public readonly int $status,
        ?Throwable $previous = null,
        public readonly bool $maybeDispatched = false,
    ) {
        parent::__construct($message, 0, $previous);
    }
}
