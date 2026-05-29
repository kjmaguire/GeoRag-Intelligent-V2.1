<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Create the `public_geo` schema and its Phase-1 registry tables.
 *
 * Public Geoscience is the second (read-only) corpus alongside the internal
 * `silver` archive. Phase 1 scaffolds only the jurisdiction + source registry
 * so the UI can render the country/jurisdiction picker and the "coming soon"
 * roadmap. Canonical entity tables (pg_mine, pg_mineral_occurrence,
 * pg_drillhole_collar, pg_resource_potential_zone) and their history siblings
 * land in Phase 2.
 *
 * See: georag-public-geoscience-plan.md v0.4 §02 (jurisdiction registry),
 *      §03a (Saskatchewan source inventory), §06a (PostGIS additions).
 */
return new class extends Migration
{
    public function up(): void
    {
        // ── 10th schema (alongside silver and core public) ───────────────
        DB::statement('CREATE SCHEMA IF NOT EXISTS public_geo');

        // ── jurisdictions registry (plan §02b) ───────────────────────────
        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.jurisdictions (
                jurisdiction_code    VARCHAR(16)  PRIMARY KEY,
                country_code         VARCHAR(3)   NOT NULL,
                display_name         VARCHAR(128) NOT NULL,
                level                VARCHAR(16)  NOT NULL
                    CHECK (level IN ('country','province','territory','state','federal')),
                status               VARCHAR(16)  NOT NULL
                    CHECK (status IN ('active','coming_soon','deprecated')),
                primary_authority    VARCHAR(255) NULL,
                license_summary      VARCHAR(255) NULL,
                license_url          TEXT         NULL,
                default_source_crs   INT          NULL,
                refresh_cadence      VARCHAR(64)  NULL,
                last_refreshed_at    TIMESTAMPTZ  NULL,
                teaser               VARCHAR(255) NULL,
                sort_order           INT          NOT NULL DEFAULT 100,
                created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        ");

        // PostGIS bbox polygon (EPSG:4326) — used by the map fly-to.
        DB::statement(
            "SELECT AddGeometryColumn('public_geo', 'jurisdictions', 'bbox', 4326, 'POLYGON', 2)"
        );

        DB::statement("
            CREATE INDEX IF NOT EXISTS idx_pg_jurisdictions_country_status
                ON public_geo.jurisdictions (country_code, status)
        ");

        DB::statement("
            CREATE INDEX IF NOT EXISTS idx_pg_jurisdictions_bbox
                ON public_geo.jurisdictions USING GIST (bbox)
        ");

        // ── sources registry (plan §03a) ─────────────────────────────────
        // One row per (jurisdiction, canonical_type) feed. Canonical entity
        // tables they populate land in Phase 2.
        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.sources (
                source_id            VARCHAR(64)  PRIMARY KEY,
                jurisdiction_code    VARCHAR(16)  NOT NULL,
                name                 VARCHAR(255) NOT NULL,
                canonical_type       VARCHAR(32)  NOT NULL
                    CHECK (canonical_type IN (
                        'mine',
                        'mineral_occurrence',
                        'drillhole_collar',
                        'resource_potential_zone'
                    )),
                service_url          TEXT         NOT NULL,
                layer_index          INT          NULL,
                source_crs           INT          NULL,
                license_summary      VARCHAR(255) NULL,
                license_url          TEXT         NULL,
                refresh_cadence      VARCHAR(64)  NULL,
                last_refreshed_at    TIMESTAMPTZ  NULL,
                notes                TEXT         NULL,
                created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT fk_pg_sources_jurisdiction
                    FOREIGN KEY (jurisdiction_code)
                    REFERENCES public_geo.jurisdictions(jurisdiction_code)
                    ON DELETE CASCADE
            )
        ");

        DB::statement("
            CREATE INDEX IF NOT EXISTS idx_pg_sources_jurisdiction
                ON public_geo.sources (jurisdiction_code)
        ");

        DB::statement("
            CREATE INDEX IF NOT EXISTS idx_pg_sources_canonical_type
                ON public_geo.sources (canonical_type)
        ");
    }

    public function down(): void
    {
        DB::statement('DROP TABLE IF EXISTS public_geo.sources CASCADE');
        DB::statement('DROP TABLE IF EXISTS public_geo.jurisdictions CASCADE');
        DB::statement('DROP SCHEMA IF EXISTS public_geo CASCADE');
    }
};
