<?php

declare(strict_types=1);

namespace App\Support;

use Throwable;

/**
 * What an exception is allowed to tell the caller.
 *
 * ExportController and UploadController already gated `$e->getMessage()`
 * behind `config('app.debug')`. Four others — ProjectController,
 * CollarController, DrillUploadController and ChatConversationController —
 * put it in the JSON body unconditionally, and APP_DEBUG is false in
 * production, so those were the only paths leaking internals.
 *
 * A PDO exception's message is not a user-facing string. For Postgres it
 * carries the failing SQL, the schema-qualified table and the column names;
 * for a connection failure it carries the database host and port. That is
 * free schema reconnaissance, and it was reachable by anyone who could reach
 * the endpoint.
 *
 * The exception still reaches the logs in full via `report()`. This governs
 * only what crosses the wire.
 */
final class SafeErrorMessage
{
    /**
     * The exception message when debugging, null otherwise.
     *
     * Returning null rather than a generic string keeps the response shape
     * stable while making the omission visible in the payload — a client
     * that renders `error` gets nothing to render rather than something
     * misleading.
     */
    public static function forResponse(Throwable $e): ?string
    {
        return config('app.debug') ? $e->getMessage() : null;
    }
}
