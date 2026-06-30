<?php

declare(strict_types=1);

namespace App\Http\Controllers\Admin;

use App\Http\Controllers\Controller;
use Illuminate\Http\Request;
use Illuminate\Http\Response;
use Illuminate\Support\Facades\Http;

/**
 * Phase 4 Step 2 — Sanctum-gated reverse proxy to the Kestra UI / API.
 *
 * The Kestra community edition only supports basic auth. To avoid asking
 * operators to remember a second password, we put Kestra behind a
 * Laravel-side proxy gated by the existing `admin` Gate. The proxy
 * injects `Authorization: Basic <base64>` on behalf of the operator;
 * Kestra sees an authenticated request, the operator sees no auth
 * prompt, and revocation cascades (lose the Laravel session → lose
 * Kestra access).
 *
 * Routes (in routes/web.php):
 *   Route::any('/admin/integrations/kestra/{path?}',
 *              [KestraSsoController::class, 'forward'])
 *       ->where('path', '.*');
 *
 * The `{path?}` capture handles the empty path (UI landing page) plus
 * every sub-path. WebSocket upgrades are out of scope for Phase 4 —
 * Kestra's flow execution streaming uses WS but the static + REST API
 * paths cover the operator's read-mostly workflow.
 *
 * Phase 4 ships the basic-auth-fronting approach. Phase 5 (if needed)
 * can replace this with header-based auth at an nginx/caddy edge so
 * Laravel doesn't proxy bytes.
 */
class KestraSsoController extends Controller
{
    /** Upstream Kestra URL — internal Docker network. */
    private const KESTRA_UPSTREAM = 'http://kestra:8080';

    /**
     * Headers we strip from the response before sending back to the
     * browser. `transfer-encoding` + `connection` confuse PHP-FPM /
     * Swoole; `content-length` we recompute from the body length.
     */
    private const HOP_BY_HOP_HEADERS = [
        'transfer-encoding',
        'connection',
        'keep-alive',
        'proxy-authenticate',
        'proxy-authorization',
        'te',
        'trailers',
        'upgrade',
    ];

    public function forward(Request $request, ?string $path = null): \Symfony\Component\HttpFoundation\Response
    {
        $this->authorize('admin');

        $user = config('services.kestra.basic_auth_user');
        $pass = config('services.kestra.basic_auth_password');
        if ($user === null || $pass === null || $user === '' || $pass === '') {
            abort(503, 'Kestra basic-auth credentials not configured server-side.');
        }

        $upstreamPath = $path ?? '';
        $query = $request->getQueryString();
        $upstreamUrl = sprintf(
            '%s/%s%s',
            self::KESTRA_UPSTREAM,
            ltrim($upstreamPath, '/'),
            $query !== null ? '?'.$query : '',
        );

        // Forward the method + body. Strip inbound Authorization to avoid
        // leaking the client's Sanctum bearer to Kestra.
        $method = strtolower($request->method());
        $headers = $this->forwardableRequestHeaders($request);

        try {
            $response = Http::withBasicAuth($user, $pass)
                ->withHeaders($headers)
                ->withOptions([
                    'allow_redirects' => false,
                    'connect_timeout' => 5,
                    'timeout' => 30,
                ])
                ->withBody(
                    $request->getContent(),
                    $request->header('Content-Type') ?? 'application/octet-stream',
                )
                ->send(strtoupper($method), $upstreamUrl);
        } catch (\Throwable $e) {
            return response()->json([
                'error' => 'kestra_upstream_unreachable',
                'message' => $e->getMessage(),
            ], 502);
        }

        return $this->relayResponse($response);
    }

    /**
     * @return array<string, string>
     */
    private function forwardableRequestHeaders(Request $request): array
    {
        $skip = [
            'host',
            'authorization',                 // Sanctum bearer — don't leak to Kestra
            'cookie',                        // Laravel session cookie — same reason
            'content-length',                // recomputed by Http client
            ...self::HOP_BY_HOP_HEADERS,
        ];

        $out = [];
        foreach ($request->headers->all() as $name => $values) {
            $lname = strtolower((string) $name);
            if (in_array($lname, $skip, true)) {
                continue;
            }
            $out[$name] = is_array($values) ? implode(', ', $values) : $values;
        }

        return $out;
    }

    private function relayResponse(\Illuminate\Http\Client\Response $response): \Symfony\Component\HttpFoundation\Response
    {
        $body = $response->body();
        $status = $response->status();

        $headers = [];
        foreach ($response->headers() as $name => $values) {
            $lname = strtolower($name);
            if (in_array($lname, self::HOP_BY_HOP_HEADERS, true)) {
                continue;
            }
            if ($lname === 'content-length') {
                continue;   // will recompute
            }
            $headers[$name] = is_array($values) ? implode(', ', $values) : $values;
        }

        return response($body, $status, $headers);
    }
}
