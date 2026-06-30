<?php

return [
    /*
    |--------------------------------------------------------------------------
    | Fixture Mode
    |--------------------------------------------------------------------------
    |
    | When true, dashboard controllers serve static JSON fixtures instead of
    | querying materialised views. Originally the default during early
    | dashboard UI development; the underlying tables (documents,
    | message_feedback, etc.) shipped in Module 6 (2026-04-22), so the
    | default is now `false`. Opt in via DASHBOARD_USE_FIXTURES=true if a
    | dev environment lacks seeded data.
    |
    */

    'use_fixtures' => env('DASHBOARD_USE_FIXTURES', false),

];
