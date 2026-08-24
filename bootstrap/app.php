<?php

use App\Http\Middleware\BindWorkspaceRlsContext;
use App\Http\Middleware\HandleInertiaRequests;
use App\Http\Middleware\InjectTraceparent;
use App\Http\Middleware\SecurityHeadersMiddleware;
use App\Http\Middleware\VerifyServiceKey;
use App\Support\ProxyTrust;
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

        // Module 9 Chunk 9.5 — honour the reverse proxy's X-Forwarded-*.
        //
        // Security fix 2026-08-20 (CRITICAL): production no longer trusts
        // X-Forwarded-FOR, because the Azure Container Apps ingress passes a
        // client-supplied one through rather than appending to it, which made
        // $request->ip() whatever the caller typed. The full measurement and
        // the conditions for re-enabling it are on ProxyTrust.
        $isProduction = env('APP_ENV') === 'production';

        $middleware->trustProxies(
            at: ProxyTrust::proxies(env('TRUSTED_PROXIES'), $isProduction),
            headers: ProxyTrust::forwardedHeaders(
                $isProduction,
                env('TRUST_FORWARDED_FOR', false),
            ),
        );

        // Arm row-level security before any controller runs. Every canonical
        // policy on silver/gold reads current_setting('app.workspace_id',
        // true) and treats the unset case as permissive, so an unbound
        // request sees every workspace — which was the state of 31 of the 38
        // controllers, because SetsWorkspaceRlsContext was opt-in and seven
        // of them opted in. Registered on both stacks: the Foundry pages are
        // web routes and the project API is api routes, and the gap was the
        // point.
        $middleware->web(append: [
            BindWorkspaceRlsContext::class,
            HandleInertiaRequests::class,
        ]);

        $middleware->api(append: [
            BindWorkspaceRlsContext::class,
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
            $status = $response->getStatusCode();

            if (config('app.debug') || ! in_array($status, [403, 404, 419, 429, 500, 503], true)) {
                return $response;
            }

            // The branded Error page is for BROWSERS. This handler had no
            // guard, so it caught API requests too — and since fetch() and
            // API clients do not send the X-Inertia header, Inertia fell back
            // to rendering the full HTML root view. The documented /api/v1
            // JSON API (advertised at /api/v1/openapi.json) answered a 429
            // from the queries limiter, or a 403 from a tenancy gate, with
            // 2.6 KB of text/html. response.json() then threw and the client
            // reported a parse failure instead of the reason.
            //
            // It also hid errors from our own SPA: DataImportWizard does
            // `await res.json().catch(() => ({}))`, so the HTML was swallowed
            // and the user saw a bare "HTTP 500" in place of the controller's
            // "File upload failed." message.
            if ($request->expectsJson() || $request->is('api/*')) {
                return $response;
            }

            return Inertia::render('Error', ['status' => $status])
                ->toResponse($request)
                ->setStatusCode($status);
        });
    })->create();
