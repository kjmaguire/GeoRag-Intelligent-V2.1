<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Phase 2.3 — add `last_service_edit_ms` to public_geo.sources.
 *
 * The column caches the upstream ArcGIS FeatureServer's `serviceLastEditDate`
 * (milliseconds since epoch, as published by Esri). Used by the Phase-2.3
 * daily Dagster short-circuit check:
 *
 *   IF current `serviceLastEditDate` == stored `last_service_edit_ms`
 *   THEN the daily Bronze run skips the full pull (plan §05e).
 *
 * NULL values mean "never successfully refreshed" — the short-circuit always
 * fails open and the next run pulls everything.
 */
return new class extends Migration
{
    public function up(): void
    {
        DB::statement(<<<'SQL'
            ALTER TABLE public_geo.sources
              ADD COLUMN IF NOT EXISTS last_service_edit_ms BIGINT NULL
        SQL);

        DB::statement(<<<'SQL'
            COMMENT ON COLUMN public_geo.sources.last_service_edit_ms IS
                'Upstream serviceLastEditDate (ms since epoch) captured on the most recent successful Bronze pull. Used by the daily short-circuit in plan §05e.'
        SQL);
    }

    public function down(): void
    {
        DB::statement(<<<'SQL'
            ALTER TABLE public_geo.sources
              DROP COLUMN IF EXISTS last_service_edit_ms
        SQL);
    }
};
