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
        // Generous timeout for streaming responses (seconds). Must be less than
        // the Horizon job $timeout (300 s) to avoid a race between Guzzle and
        // the queue worker.
        'stream_timeout' => (int) env('FASTAPI_STREAM_TIMEOUT', 270),
        // Canonical V1 LLM is Qwen/Qwen3-14B-AWQ served by vLLM (reverted from
        // Qwen3-30B-A3B MoE in 2026-05 — see CLAUDE.md "LLM" entry and .env
        // comment above LLM_PRIMARY_MODEL). The FastAPI app/config.py
        // VLLM_MODEL default matches. Surfaced here so QueryController stamps
        // accurate llm_model on every query_audit_log row instead of falling
        // through to a hard-coded wrong default. Override per deploy when
        // models swap.
        'llm_model' => env('FASTAPI_LLM_MODEL', 'Qwen/Qwen3-14B-AWQ'),
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
    | Martin Vector Tile Server
    |--------------------------------------------------------------------------
    |
    | Martin serves MVT tiles from PostGIS views. Laravel proxies /tiles/...
    | requests so tile fetches go through Sanctum auth. `internal_url` is
    | the in-cluster address; clients never hit Martin directly.
    |
    */
    'martin' => [
        'internal_url' => env('MARTIN_INTERNAL_URL', 'http://martin:3000'),
        'request_timeout' => (int) env('MARTIN_REQUEST_TIMEOUT', 15),
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
    | Kestra (integration-edge orchestrator)
    |--------------------------------------------------------------------------
    |
    | Phase 4 Step 2 — Sanctum-fronted reverse proxy needs the Kestra basic
    | auth credentials to inject `Authorization: Basic …` on behalf of the
    | authenticated operator. KestraSsoController reads these.
    |
    */
    'kestra' => [
        'basic_auth_user' => env('KESTRA_BASIC_AUTH_USER', 'admin@georag.local'),
        'basic_auth_password' => env('KESTRA_BASIC_AUTH_PASSWORD'),
    ],

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

];
