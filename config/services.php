<?php

return [
    /*
    |--------------------------------------------------------------------------
    | Third Party Services
    |--------------------------------------------------------------------------
    |
    | This file is for storing the credentials for third party services such
    | as Mailgun, Postmark, AWS and more. This file provides the de facto
    | location for this type of information, allowing packages to have
    | a conventional file to locate the various service credentials.
    |
    */

    'postmark' => [
        'key' => env('POSTMARK_API_KEY'),
    ],

    'resend' => [
        'key' => env('RESEND_API_KEY'),
    ],

    'ses' => [
        'key' => env('AWS_ACCESS_KEY_ID'),
        'secret' => env('AWS_SECRET_ACCESS_KEY'),
        'region' => env('AWS_DEFAULT_REGION', 'us-east-1'),
    ],

    'slack' => [
        'notifications' => [
            'bot_user_oauth_token' => env('SLACK_BOT_USER_OAUTH_TOKEN'),
            'channel' => env('SLACK_BOT_USER_DEFAULT_CHANNEL'),
        ],
    ],

    /*
    |--------------------------------------------------------------------------
    | Horizon dashboard access
    |--------------------------------------------------------------------------
    |
    | App\Providers\HorizonServiceProvider::gate() has read
    | `services.horizon.admin_emails` since the allowlist stopped being an
    | empty array literal, and its docblock describes exactly the
    | normalisation below. The block itself was never added, so the key did
    | not resolve, the `[]` default won, and the gate denied everyone in
    | every non-local environment -- which is the bug that change was written
    | to fix. Setting HORIZON_ADMIN_EMAILS had no effect because nothing
    | read it.
    |
    | Normalised here rather than in the provider so the gate compares
    | like with like: lowercased, trimmed, empties dropped, reindexed.
    | Unset still means an empty allowlist and no access -- fail closed is
    | deliberate, a deploy that forgets the variable must not expose the
    | queue dashboard.
    |
    */
    'horizon' => [
        'admin_emails' => array_values(array_filter(array_map(
            static fn (string $email): string => strtolower(trim($email)),
            explode(',', (string) env('HORIZON_ADMIN_EMAILS', '')),
        ))),
    ],

    /*
    |--------------------------------------------------------------------------
    | MapLibre basemap styles
    |--------------------------------------------------------------------------
    |
    | CLAUDE.md hard rule #8: GeoRAG uses MapLibre GL so an on-prem
    | deployment can run fully air-gapped. The style URL is the one thing
    | maplibre-gl fetches over the network, so it is configured here,
    | shared to the SPA as the `basemap_styles` Inertia prop by
    | HandleInertiaRequests, and read through resources/js/lib/basemap.ts.
    |
    | That chain was complete at both ends and missing in the middle: the
    | prop was shared, the accessor read it, and this block did not exist --
    | so the prop was null on every response, every map fell back to the
    | hard-coded public-CDN defaults in basemap.ts, and the documented
    | one-env-var swap could not be performed at all.
    |
    | `glyphs` is not a style: it is the font-PBF endpoint a hand-built
    | style object needs (WorkspaceMap's terrain style). It lived as a
    | fourth hard-coded URL outside the registry, so an air-gapped
    | deployment that swapped all three styles still reached for fonts on
    | the public internet.
    |
    */
    'basemap' => [
        'styles' => [
            'positron' => env('BASEMAP_STYLE_POSITRON', 'https://tiles.openfreemap.org/styles/positron'),
            'bright' => env('BASEMAP_STYLE_BRIGHT', 'https://tiles.openfreemap.org/styles/bright'),
            'dark_matter' => env(
                'BASEMAP_STYLE_DARK_MATTER',
                'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json',
            ),
        ],
        'glyphs' => env(
            'BASEMAP_GLYPHS_URL',
            'https://basemaps.cartocdn.com/gl/dark-matter-gl-style/glyphs/{fontstack}/{range}.pbf',
        ),
        // The satellite basemap is a raster tile template, not a style.json,
        // so WorkspaceMap wraps it in a minimal style object it builds
        // inline. Configured here for the same reason as the rest: it is a
        // network dependency an air-gapped deployment has to be able to
        // repoint.
        'satellite_tiles' => env(
            'BASEMAP_SATELLITE_TILES',
            'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        ),
        'satellite_attribution' => env('BASEMAP_SATELLITE_ATTRIBUTION', 'Tiles © Esri'),

        // MapView's terrain + imagery sources. MapView is a different
        // component from WorkspaceMap -- it backs Foundry/PublicGeoscience
        // and the inline maps in chat -- and it reached for two hosts that
        // appear nowhere else: a terrain-RGB DEM and Sentinel-2 cloudless
        // imagery.
        //
        // Both were hard-coded, and both were absent from the CSP's
        // connect-src while resources/views/app.blade.php preconnects to
        // them, so the page warmed a TLS connection to a host the browser
        // then refused to fetch from. Configuring them here puts them in
        // the allowlist SecurityHeadersMiddleware derives, which is the
        // actual fix; being repointable is the bonus.
        //
        // NOTE: `imagery_tiles` (EOX Sentinel-2) and `satellite_tiles`
        // (Esri World Imagery) above are two different providers for the
        // same idea, chosen by whichever component you happen to be
        // looking at. That is drift, but resolving it changes which
        // imagery a geologist sees over their project, so it is a product
        // call and not a cleanup.
        'dem_tiles' => env('BASEMAP_DEM_TILES', 'https://tiles.mapterhorn.com/tilejson.json'),
        'imagery_tiles' => env(
            'BASEMAP_IMAGERY_TILES',
            'https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020_3857/default/g/{z}/{y}/{x}.jpg',
        ),
    ],

    /*
    |--------------------------------------------------------------------------
    | FastAPI Internal Service
    |--------------------------------------------------------------------------
    |
    | Used by Laravel to proxy RAG queries to the FastAPI domain service over
    | the internal Docker network. The service key is shared via env and must
    | match LARAVEL_SERVICE_KEY on the FastAPI side.
    |
    | B7: `service_key` doubles as the HS256 signing secret for the short-TTL
    | JWTs minted by App\Services\FastApiJwtMinter on every outbound call.
    | FastAPI verifies the signature with the same key, then reads user_id /
    | project_id / roles from the payload for document-level RBAC.
    |
    */
    'fastapi' => [
        'internal_url' => env('FASTAPI_INTERNAL_URL', 'http://fastapi:8000'),
        // Audit 2026-06-28: base_url alias so controllers read it via config()
        // (config:cache-safe) instead of a bare env('FASTAPI_BASE_URL').
        'base_url' => env('FASTAPI_BASE_URL', env('FASTAPI_INTERNAL_URL', 'http://fastapi:8000')),
        'service_key' => env('FASTAPI_SERVICE_KEY'),
        // V1.5-03 — `kid` (key id) header on every minted JWT. FastAPI uses it
        // to pick the matching secret from a kid→key map, enabling
        // zero-downtime rotation (operator stages a new key + new kid, FastAPI
        // accepts both, Laravel switches mint kid, operator drops the old).
        // Default `primary` is the canonical "current" key tag; rotate by
        // setting this env to e.g. `2026-q3` and provisioning the new secret.
        'service_key_kid' => env('FASTAPI_SERVICE_KEY_KID', 'primary'),
        // 2026-09-06 — the outgoing key during a rotation window. The
        // VerifyServiceKey middleware (FastAPI / Hatchet → Laravel callbacks)
        // accepts either this or `service_key`, mirroring FastAPI's
        // FASTAPI_SERVICE_KEY_PREVIOUS on the reverse path, so neither side
        // 401s while the other's consumers roll. Empty in steady state.
        // Minting never uses it: outbound JWTs are always signed with
        // `service_key`. See ops/runbooks/secret-rotation.md § 3.
        'service_key_previous' => env('FASTAPI_SERVICE_KEY_PREVIOUS', ''),
        // Guzzle read timeout for the streaming answer response, in seconds.
        //
        // This is the SOURCE of the inner-must-expire-first invariant, not
        // one half of it: StreamQueryFromFastApi derives its own Horizon
        // $timeout as this value plus a fixed headroom, so raising this
        // raises that. It used to be a comment here asserting "must be less
        // than the Horizon job $timeout (300 s)" against a 300 hard-coded in
        // the job, which raising FASTAPI_STREAM_TIMEOUT would have inverted
        // without a word.
        'stream_timeout' => (int) env('FASTAPI_STREAM_TIMEOUT', 270),
        // Seconds after /queries/{id}/start past which a StreamQueryFromFastApi
        // job that has only just been picked up is abandoned (terminal
        // `failed` QUEUE_STALE) instead of calling FastAPI. Chat.tsx's idle
        // watchdog gives up after 120 s with nothing received; a job popped
        // later than that streams to nobody and still pays for the LLM run.
        // Kept under 120 so the stale frame still reaches a listening client.
        'queue_stale_after' => (int) env('FASTAPI_QUEUE_STALE_AFTER', 110),
        // Largest `completed` frame, in bytes of JSON, that the job hands to
        // Reverb as-is. Reverb rejects a request over REVERB_MAX_REQUEST_SIZE
        // (1,000,000 in production) and the Pusher SDK re-escapes `data`
        // (+~20%), so an oversized frame is dropped and the chat never
        // terminates. Over budget, the job broadcasts a slim `completed`
        // (answer, citations, verdicts) marked payload_truncated=true.
        'completed_frame_budget_bytes' => (int) env('FASTAPI_COMPLETED_FRAME_BUDGET_BYTES', 700_000),
        // Provisional model name stamped onto every query_audit_log row by
        // QueryController at reservation time. StreamQueryFromFastApi
        // overwrites it from the `completed` frame's `llm_model`, so only a
        // run that never completes keeps this value. Set FASTAPI_LLM_MODEL
        // when the chat backend moves; a stale default makes the audit
        // question "which model was meant to answer" wrong for failed runs.
        'llm_model' => env('FASTAPI_LLM_MODEL', 'Cohere-command-a-plus-05-2026'),
    ],

    'hatchet' => [
        // Per-workspace dispatch smoothing (HatchetDispatchThrottle). The
        // old hard-coded 2000ms was sized for 500-file bulk replays and
        // pinned an Octane worker >=2s per interactive upload.
        'dispatch_throttle_ms' => (int) env('HATCHET_DISPATCH_THROTTLE_MS', 250),
    ],

    /*
    |--------------------------------------------------------------------------
    | Octane worker count (for /internal/metrics only)
    |--------------------------------------------------------------------------
    |
    | MetricsController::octaneWorkers() has read
    | `config('services.octane_metrics.workers')` since it was written, and
    | that key did not exist — so it resolved to null, `max(1, 0)` returned
    | 1, and octane_workers_total reported 1 on a deployment running 4.
    | Worker saturation was therefore invisible: the busy gauge could never
    | exceed the total.
    |
    | Same source of truth as the runtime: the OCTANE_WORKERS env var the
    | container start command passes to `octane:start --workers`.
    |
    */

    'octane_metrics' => [
        'workers' => (int) env('OCTANE_WORKERS', 4),
    ],

    /*
    |--------------------------------------------------------------------------
    | Martin Vector Tile Server
    |--------------------------------------------------------------------------
    |
    | Martin serves MVT tiles from PostGIS functions. Laravel proxies
    | /tiles/... requests so tile fetches go through session auth and the
    | project-access check; clients never hit Martin directly, and Martin has
    | no ingress of its own.
    |
    | Removed with the rest of the demo-external services in 0eada56c
    | (2026-07-27) and restored here. The DATABASE side was never removed:
    | 18 tile functions (silver.pg_*_by_project, public_geo.pg_*_tiles) and
    | the `martin_readonly` role with EXECUTE on 24 of them are live on the
    | Azure server today, and migrations kept ADDING to them after the
    | service went — silver.pg_spatial_features_by_project was created
    | 2026-08-23, a month later. Only the service and this proxy were missing.
    |
    */
    'martin' => [
        'internal_url' => env('MARTIN_INTERNAL_URL', 'http://martin:3000'),
        'request_timeout' => (int) env('MARTIN_REQUEST_TIMEOUT', 15),
    ],

    /*
    |--------------------------------------------------------------------------
    | Dormant, routed features (LAR-10 / LAR-11, 2026-09-29)
    |--------------------------------------------------------------------------
    |
    | Both surfaces below are wired to routes but have no page in
    | resources/js/Pages and are not configured in deploy/aws/terraform, so in
    | AWS they answered 500 (OAuth: a request-path CREATE TABLE the app role
    | may not run) or 503 (integrations: AUDIT_ENCRYPTION_KEY unset). They
    | are gated OFF by default and answer 404 until an operator turns them on
    | deliberately. Values moved out of raw env() reads in the controllers,
    | which return null the day the image runs `config:cache`.
    |
    | Turning cloud_ingest_oauth on is NOT enough by itself: the
    | silver.cloud_ingest_connections table it persists to is created by no
    | migration (the runtime DDL was removed), and a new table needs FORCE
    | ROW LEVEL SECURITY plus a tenant_isolation policy (§06b).
    |
    */
    'admin_integrations' => [
        'enabled' => (bool) env('ADMIN_INTEGRATIONS_ENABLED', false),
    ],

    'audit' => [
        // pgp_sym_encrypt key for usage.* sender secrets and flow JWT keys.
        'encryption_key' => env('AUDIT_ENCRYPTION_KEY'),
    ],

    'cloud_ingest_oauth' => [
        'enabled' => (bool) env('CLOUD_INGEST_OAUTH_ENABLED', false),
        'providers' => [
            'sharepoint' => [
                'client_id' => env('OAUTH_SHAREPOINT_CLIENT_ID'),
                'client_secret' => env('OAUTH_SHAREPOINT_CLIENT_SECRET'),
                'auth_url' => env('OAUTH_SHAREPOINT_AUTH_URL', 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize'),
                'token_url' => env('OAUTH_SHAREPOINT_TOKEN_URL', 'https://login.microsoftonline.com/common/oauth2/v2.0/token'),
            ],
            'onedrive' => [
                'client_id' => env('OAUTH_ONEDRIVE_CLIENT_ID'),
                'client_secret' => env('OAUTH_ONEDRIVE_CLIENT_SECRET'),
                'auth_url' => env('OAUTH_ONEDRIVE_AUTH_URL', 'https://login.microsoftonline.com/common/oauth2/v2.0/authorize'),
                'token_url' => env('OAUTH_ONEDRIVE_TOKEN_URL', 'https://login.microsoftonline.com/common/oauth2/v2.0/token'),
            ],
            'googledrive' => [
                'client_id' => env('OAUTH_GOOGLEDRIVE_CLIENT_ID'),
                'client_secret' => env('OAUTH_GOOGLEDRIVE_CLIENT_SECRET'),
                'auth_url' => env('OAUTH_GOOGLEDRIVE_AUTH_URL', 'https://accounts.google.com/o/oauth2/v2/auth'),
                'token_url' => env('OAUTH_GOOGLEDRIVE_TOKEN_URL', 'https://oauth2.googleapis.com/token'),
            ],
        ],
    ],

];
