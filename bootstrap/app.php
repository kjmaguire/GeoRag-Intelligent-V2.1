<?php

use App\Http\Middleware\HandleInertiaRequests;
use App\Http\Middleware\InjectTraceparent;
use App\Http\Middleware\SecurityHeadersMiddleware;
use App\Http\Middleware\VerifyServiceKey;
use Illuminate\Foundation\Application;
use Illuminate\Foundation\Configuration\Exceptions;
use Illuminate\Foundation\Configuration\Middleware;
use Illuminate\Http\Request;
use Inertia\Inertia;
use Laravel\Sanctum\Http\Middleware\EnsureFrontendRequestsAreStateful;
use Symfony\Component\HttpFoundation\Response;

return Application::configure(basePath: dirname(__DIR__))
    ->withRouting(
        web: __DIR__.'/../routes/web.php',
        api: __DIR__.'/../routes/api.php',
        commands: __DIR__.'/../routes/console.php',
        channels: __DIR__.'/../routes/channels.php',
        health: '/up',
    )
    ->withMiddleware(function (Middleware $middleware): void {
        // Module 10 Chunk 10.6 — W3C Trace Context. Mint or accept inbound
        // `traceparent`, expose on request attributes, echo on response.
        // Outbound HTTP calls to FastAPI must read the attribute and forward.
        $middleware->prepend(InjectTraceparent::class);

        // Module 9 Chunk 9.5 — defence-in-depth security headers on every
        // HTTP response (XFO, XCTO, Referrer-Policy, Permissions-Policy,
        // CSP, HSTS-when-secure). Registered globally so both web (Inertia)
        // and api responses carry the headers.
        $middleware->append(SecurityHeadersMiddleware::class);

        // Module 9 Chunk 9.5 — trust the reverse proxy so X-Forwarded-* is
        // honoured (otherwise per-IP rate-limiting collapses to a single
        // bucket behind nginx/Traefik, and request->isSecure() reads false).
        // Production sets TRUSTED_PROXIES to an explicit CIDR allowlist.
        //
        // Security fix 2026-08-14 (LOW): production fails CLOSED. When
        // APP_ENV=production and TRUSTED_PROXIES is unset/empty, NO proxies
        // are trusted (clients cannot spoof X-Forwarded-For to bypass per-IP
        // rate limits or forge isSecure()). The '*' wildcard remains the
        // dev-friendly default for every non-production environment.
        $trustedProxies = env('TRUSTED_PROXIES');
        if ($trustedProxies === null || $trustedProxies === '') {
            $trustedProxies = env('APP_ENV') === 'production' ? [] : '*';
        }
        $middleware->trustProxies(
            at: $trustedProxies,
            headers: Request::HEADER_X_FORWARDED_FOR
                | Request::HEADER_X_FORWARDED_HOST
                | Request::HEADER_X_FORWARDED_PORT
                | Request::HEADER_X_FORWARDED_PROTO,
        );

        $middleware->web(append: [
            HandleInertiaRequests::class,
        ]);

        // Sanctum SPA stateful auth: detects first-party requests (matching
        // SANCTUM_STATEFUL_DOMAINS) and activates session/cookie-based auth
        // so the SPA can authenticate without Bearer tokens.
        $middleware->api(prepend: [
            EnsureFrontendRequestsAreStateful::class,
        ]);

        // Phase H4 §7 — service-key alias for the internal FastAPI → Laravel
        // callback channel (real-time broadcast bridge).
        $middleware->alias([
            'service.key' => VerifyServiceKey::class,
        ]);
    })
    ->withExceptions(function (Exceptions $exceptions): void {
        // UI/UX fix (2026-08-15, live-browser-observed): before this handler,
        // any 403/404/419/429/500/503 fell through to Laravel's bare
        // framework error page (plain "404 | Not Found" text - no header,
        // no nav, no way back into the app). Reproduced live via a stale
        // citation "Open in Reader ->" deep link. Route these statuses
        // through the branded Inertia Error page instead so there's always
        // a path back into the app. Skipped when APP_DEBUG is on so local
        // debugging still gets Laravel's full Whoops trace.
        $exceptions->respond(function (Response $response, Throwable $exception, Request $request) {
            if (! config('app.debug') && in_array($response->getStatusCode(), [403, 404, 419, 429, 500, 503], true)) {
                return Inertia::render('Error', ['status' => $response->getStatusCode()])
                    ->toResponse($request)
                    ->setStatusCode($response->getStatusCode());
            }

            return $response;
        });
    })->create();
