<?php

use App\Http\Middleware\HandleInertiaRequests;
use App\Http\Middleware\InjectTraceparent;
use App\Http\Middleware\SecurityHeadersMiddleware;
use App\Http\Middleware\VerifyServiceKey;
use Illuminate\Foundation\Application;
use Illuminate\Foundation\Configuration\Exceptions;
use Illuminate\Foundation\Configuration\Middleware;
use Illuminate\Http\Request;
use Laravel\Sanctum\Http\Middleware\EnsureFrontendRequestsAreStateful;

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
        // Production overrides TRUSTED_PROXIES with an explicit CIDR allowlist;
        // '*' is a dev-friendly default.
        $middleware->trustProxies(
            at: env('TRUSTED_PROXIES', '*'),
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
        //
    })->create();
