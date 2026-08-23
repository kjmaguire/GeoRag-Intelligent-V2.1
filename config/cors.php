<?php

return [
    /*
    |--------------------------------------------------------------------------
    | Cross-Origin Resource Sharing (CORS) Configuration
    |--------------------------------------------------------------------------
    |
    | Credentials must be allowed for Sanctum SPA cookie auth to function.
    | When supports_credentials is true, allowed_origins must list explicit
    | origins — wildcard '*' is rejected by browsers with credentials.
    |
    */

    'paths' => ['api/*', 'sanctum/csrf-cookie'],

    // Module 9 Chunk 9.6 (A5-03) — explicit method allowlist instead of '*'.
    'allowed_methods' => ['GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'],

    /*
     * CORS_ALLOWED_ORIGINS is not set on the production container, so this
     * fallback was what production actually served: five localhost origins
     * and two georag.local ones, with supports_credentials => true. The real
     * production FQDN is not among them, so the documented public API is
     * unusable from any browser client — while any page served from one of
     * those local ports can make credentialed cross-origin requests.
     * SameSite=lax blunts the cookie half; Sanctum Bearer clients get no
     * protection from SameSite at all.
     *
     * The dev list is now the DEVELOPMENT fallback only. In production an
     * unset CORS_ALLOWED_ORIGINS means no cross-origin caller is trusted,
     * which is the safe reading of "nobody said" — same-origin requests
     * (the Inertia app itself) do not involve CORS and are unaffected.
     */
    'allowed_origins' => array_values(array_filter(array_map(
        'trim',
        explode(',', (string) env(
            'CORS_ALLOWED_ORIGINS',
            env('APP_ENV') === 'production'
                ? ''
                : 'http://localhost:3000,http://localhost:5173,http://localhost:8888,'
                    .'http://127.0.0.1:8000,http://127.0.0.1:8888,'
                    .'http://georag.local,http://georag.local:8000',
        )),
    ))),

    'allowed_origins_patterns' => [],

    // Module 9 Chunk 9.6 (A5-03) — explicit header allowlist instead of '*'.
    // Includes Inertia control headers, CSRF, Authorization, Content-Type,
    // and X-Request-ID (Server-Timing trace propagation).
    'allowed_headers' => [
        'Accept',
        'Authorization',
        'Content-Type',
        'X-CSRF-TOKEN',
        'X-XSRF-TOKEN',
        'X-Inertia',
        'X-Inertia-Version',
        'X-Inertia-Partial-Component',
        'X-Inertia-Partial-Data',
        'X-Requested-With',
        'X-Request-ID',
    ],

    // Module 9 Chunk 9.6 — Server-Timing emitted by the tile proxy
    // (Chunk 8.4) and X-Request-ID emitted by trace middleware should be
    // readable from the SPA.
    'exposed_headers' => ['X-Request-ID', 'Server-Timing'],

    'max_age' => 0,

    'supports_credentials' => true,

];
