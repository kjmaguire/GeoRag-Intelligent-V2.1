<?php

declare(strict_types=1);

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * SMDI ingestion plan v1.1 (2026-05-24, georag-smdi-ingestion-plan).
 *
 * Standalone public.smdi_deposits table for the Saskatchewan Mineral
 * Deposit Index FeatureServer feed. 6,012 point features pulled from
 * https://gis.saskatchewan.ca/egis/rest/services/Economy/Mineral_Exploration/FeatureServer/2.
 *
 * NOTE — architectural reconciliation (see docs/handoffs/smdi_ingestion_2026_05_25.md):
 *   public_geo.pg_mineral_occurrence already exists as the canonical
 *   multi-jurisdiction mineral-occurrence table and currently holds 14
 *   synthetic SK stubs. This new table coexists by design: the plan called
 *   for a single-purpose SK-only table tied to a specific upstream URL,
 *   while pg_mineral_occurrence is part of the multi-jurisdiction Bronze→
 *   Silver lakehouse. Unification is a Kyle decision documented in the
 *   handoff doc.
 */
return new class extends Migration
{
    public function up(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        // PostGIS is required for the GEOMETRY column. CREATE EXTENSION is
        // idempotent and guarded so dev clusters without it bootstrap cleanly.
        DB::statement('CREATE EXTENSION IF NOT EXISTS postgis');

        DB::statement(<<<'SQL'
            CREATE TABLE IF NOT EXISTS public.smdi_deposits (
                objectid               INTEGER PRIMARY KEY,
                smdi                   VARCHAR(20)        NOT NULL,
                name                   TEXT,
                historic_names         TEXT,
                primary_commodities    TEXT,
                associated_commodities TEXT,
                grouping               TEXT,
                discovery_type         TEXT,
                production             BOOLEAN,
                reserves_resources     BOOLEAN,
                status                 TEXT,
                symbology_status       TEXT,
                symbology_grouping     TEXT,
                utm13e                 DOUBLE PRECISION,
                utm13n                 DOUBLE PRECISION,
                weblink                TEXT,
                global_id              TEXT,
                geom                   GEOMETRY(Point, 4326) NOT NULL,
                fetched_at             TIMESTAMPTZ DEFAULT now(),
                updated_at             TIMESTAMPTZ DEFAULT now()
            )
        SQL);

        DB::statement(
            'CREATE INDEX IF NOT EXISTS smdi_deposits_geom_idx
             ON public.smdi_deposits USING GIST (geom)',
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS smdi_deposits_smdi_idx
             ON public.smdi_deposits (smdi)',
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS smdi_deposits_grouping_idx
             ON public.smdi_deposits (symbology_grouping)',
        );

        DB::statement(
            'CREATE INDEX IF NOT EXISTS smdi_deposits_status_idx
             ON public.smdi_deposits (status)',
        );

        DB::statement(
            "COMMENT ON TABLE public.smdi_deposits IS
             'Saskatchewan Mineral Deposit Index — sourced from gis.saskatchewan.ca/egis/.../Mineral_Exploration/FeatureServer/2, refreshed via Dagster smdi_deposits_refresh asset'",
        );

        // Martin reads as the martin_readonly role; grant SELECT so the tile
        // server can publish the table without elevation. Matches the grant
        // pattern from 2026_04_22_150000_grant_martin_readonly_select.
        DB::statement('GRANT USAGE ON SCHEMA public TO martin_readonly');
        DB::statement('GRANT SELECT ON public.smdi_deposits TO martin_readonly');
    }

    public function down(): void
    {
        if (DB::connection()->getDriverName() !== 'pgsql') {
            return;
        }

        DB::statement('DROP TABLE IF EXISTS public.smdi_deposits');
    }
};
