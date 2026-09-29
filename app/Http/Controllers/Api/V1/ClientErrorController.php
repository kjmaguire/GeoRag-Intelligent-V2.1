<?php

declare(strict_types=1);

namespace App\Http\Controllers\Api\V1;

use App\Http\Controllers\Controller;
use Illuminate\Http\Request;
use Illuminate\Http\Response;
use Illuminate\Support\Facades\Log;
use Illuminate\Support\Str;

/**
 * POST /api/v1/client-errors — crash telemetry from the React ErrorBoundary.
 *
 * `resources/js/Components/ErrorBoundary.tsx` has POSTed here since it was
 * written, and no route existed, so every render crash in production went
 * unrecorded — the "Something went wrong" reports from the 2026-09-29 AWS
 * session among them (FE-22). This is the minimal receiving end: validate,
 * truncate, and write ONE structured log line (CloudWatch in production).
 * Nothing is stored in the database.
 *
 * Public, because a crash on the login page is still a crash, and therefore
 * throttled per IP and bounded in size (routes/api.php). The reporting page
 * is reduced to its path — query strings can carry tokens (reset links) or
 * return_to targets and are not worth the risk in a log line.
 */
class ClientErrorController extends Controller
{
    private const MAX_MESSAGE = 1000;

    private const MAX_STACK = 8000;

    public function __invoke(Request $request): Response
    {
        $data = $request->validate([
            'scope' => ['nullable', 'string', 'max:64'],
            'message' => ['nullable', 'string', 'max:20000'],
            'stack' => ['nullable', 'string', 'max:50000'],
            'componentStack' => ['nullable', 'string', 'max:50000'],
            'url' => ['nullable', 'string', 'max:4000'],
            'userAgent' => ['nullable', 'string', 'max:1000'],
        ]);

        $path = null;
        if (is_string($data['url'] ?? null)) {
            $parsed = parse_url($data['url'], PHP_URL_PATH);
            $path = is_string($parsed) ? Str::limit($parsed, 300, '') : null;
        }

        Log::warning('client.render_error', [
            'scope' => $data['scope'] ?? 'root',
            'message' => Str::limit((string) ($data['message'] ?? ''), self::MAX_MESSAGE),
            'stack' => Str::limit((string) ($data['stack'] ?? ''), self::MAX_STACK),
            'component_stack' => Str::limit((string) ($data['componentStack'] ?? ''), self::MAX_STACK),
            'path' => $path,
            'user_agent' => Str::limit((string) ($data['userAgent'] ?? ''), 300),
            'user_id' => $request->user()?->getAuthIdentifier(),
        ]);

        return response()->noContent();
    }
}
