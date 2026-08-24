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
        // Stamped onto every query_audit_log row by QueryController.
        //
        // The default was 'Qwen/Qwen3-14B-AWQ' with a docblock explaining
        // that it existed precisely so the audit row would be accurate
        // — and FASTAPI_LLM_MODEL was never set on the production
        // container, so the wrong default was exactly what got stamped.
        // The vLLM cutover to Azure AI Foundry completed 2026-07-30, so
        // every audit row since then names a model that has not served a
        // request. For a platform selling cited, auditable answers for
        // regulated mining disclosure, that makes any retrospective
        // "which model produced this answer" question wrong, along with
        // any cost or quality attribution built on the column.
        //
        // The default now matches what fastapi-cc actually runs
        // (AZURE_FOUNDRY_DEPLOYMENT=Cohere-command-a-plus-05-2026, verified
        // live 2026-08-21). Set FASTAPI_LLM_MODEL when the backend moves,
        // and treat a mismatch between this and AZURE_FOUNDRY_DEPLOYMENT as
        // a deploy error rather than a cosmetic one.
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
    | Qdrant
    |--------------------------------------------------------------------------
    |
    | Drift L-02 (Wave 3.A audit): config/services.php had no 'qdrant' key,
    | so HealthController's `config('services.qdrant.host', env('QDRANT_HOST'
    | , 'qdrant'))` was load-bearing on the env() default. Surfaced here so
    | the config cache picks it up and the defensive env() in the consumer
    | becomes belt-and-suspenders.
    */
    'qdrant' => [
        'host' => env('QDRANT_HOST', 'qdrant'),
        'port' => (int) env('QDRANT_PORT', 6333),
    ],

    /*
    |--------------------------------------------------------------------------
    | Tempo (distributed tracing backend)
    |--------------------------------------------------------------------------
    |
    | Phase 0 Step 3 — Workflow Run Dashboard renders one anchor per row that
    | links to Tempo's HTTP API for the trace_id stamped on the run. Operators
    | click through to the span tree without leaving the dashboard. Defaulted
    | to localhost:3200 to match the dev compose stack; production overrides
    | via TEMPO_HOST_URL (typically the in-cluster grafana/tempo URL or the
    | external operator-facing hostname behind SSO).
    |
    */
    'tempo' => [
        'url' => env('TEMPO_HOST_URL', 'http://localhost:3200'),
    ],

    /*
    |--------------------------------------------------------------------------
    | Kestra — REMOVED 2026-07-28 (A7)
    |--------------------------------------------------------------------------
    |
    | KestraSsoController (which read this block) and the compose kestra +
    | caddy services are gone. Kestra was never deployed — KESTRA_URL was
    | unset in every environment. See database/raw/phase3/95-kestra-sunset.sql.
    |
    */

    /*
    |--------------------------------------------------------------------------
    | Dagster GraphQL
    |--------------------------------------------------------------------------
    |
    | CC-01 Item 1 Slice 1 — Laravel synchronously launches asset
    | materialisations (silver_collars / silver_lithology / silver_samples /
    | silver_xlsx) via Dagster's GraphQL endpoint to avoid the 5-minute MinIO
    | sensor poll on the drill-upload UX path. The location/repository defaults
    | match the standard georag_dagster package layout; override when the
    | code-location naming diverges.
    |
    */
    'dagster' => [
        // Dagster was retired 2026-07-28 (trim B2). OFF by default: with it
        // on, every /internal/metrics scrape opened a PDO connection to the
        // hardcoded docker-compose host `postgresql:5432`, which does not
        // resolve on Azure — so each scrape paid a failed connect against a
        // 2s timeout and wrote a `dagster_metrics_query_failed` warning into
        // Log Analytics, then emitted `dagster_runs_total{status="none"} 0`,
        // making a decommissioned stack read as merely idle.
        //
        // Set DAGSTER_METRICS_ENABLED=true only where a Dagster runs DB is
        // genuinely reachable (i.e. the self-hosted docker-compose stack).
        'enabled' => (bool) env('DAGSTER_METRICS_ENABLED', false),

        'url' => env('DAGSTER_GRAPHQL_URL', 'http://dagster-webserver:3001'),
        'location' => env('DAGSTER_LOCATION', 'georag_dagster'),
        'repository' => env('DAGSTER_REPOSITORY', '__repository__'),
        'timeout' => (int) env('DAGSTER_GRAPHQL_TIMEOUT', 10),

        // Direct PDO connection to the Dagster runs DB — used by
        // MetricsController::dagsterRunsRowsViaPdo() to surface
        // dagster_runs_total{status=...} on /internal/metrics. Without
        // these, every scrape tick logged `dagster_metrics_query_failed:
        // no password supplied` because config(...pg_db) and friends
        // resolved to null → empty string. (Added 2026-05-25 after a
        // 15-second-cadence log spam was traced back here.)
        //
        // The controller hard-codes host=postgresql:5432 because
        // PgBouncer doesn't proxy the Dagster DB; only the credentials
        // + DB name are taken from config. Defaults mirror the .env
        // example so a fresh checkout works without manual wiring.
        'pg_db' => env('DAGSTER_PG_DB', 'georag_dagster'),
        'pg_user' => env('DAGSTER_PG_USER', 'georag'),
        'pg_password' => env('DAGSTER_PG_PASSWORD', ''),
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

];
