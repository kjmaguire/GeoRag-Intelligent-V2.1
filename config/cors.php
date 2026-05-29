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

    'allowed_origins' => explode(',', env('CORS_ALLOWED_ORIGINS',
        'http://localhost:3000,http://localhost:5173,http://localhost:8888,http://127.0.0.1:8000,http://127.0.0.1:8888,http://georag.local,http://georag.local:8000'
    )),

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
