<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Tier 2 expansion — Bedrock Geology.
 *
 *   pg_bedrock_geology  ← Economy/Geology/MapServer/10
 *                         (SK Bedrock Geology 250K — polygon layer,
 *                         several thousand bedrock unit polygons)
 *
 * Single-layer table. Scale column defaults to '250K' and is stored
 * as a constant so future 1M-scale layers can share this schema via
 * a second source_id (CA-SK-GEOLOGY-BEDROCK-1M) without schema changes.
 *
 * Field mapping (ArcGIS source → canonical):
 *   ROCK_CODE   → unit_code     (NOT NULL; primary discriminator)
 *   NAME        → unit_name     (nullable; human-readable label)
 *   EON         → eon
 *   ERA         → era
 *   PERIOD      → period
 *   GROUP_      → group_name    (renamed from SQL reserved `GROUP`)
 *   FORMATION   → formation
 *   MEMBER      → member
 *   DOMAIN      → structural_domain (renamed from SQL reserved `DOMAIN`)
 *   LITHOLOGY   → lithology
 *   (constant)  → scale         ('250K' for this source)
 */
return new class extends Migration
{
    public function up(): void
    {
        // ── pg_bedrock_geology ────────────────────────────────────────────
        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.pg_bedrock_geology (
                id                   UUID         PRIMARY KEY,
                jurisdiction_code    VARCHAR(16)  NOT NULL,
                source_id            VARCHAR(64)  NOT NULL,
                source_feature_id    VARCHAR(128) NOT NULL,
                unit_code            VARCHAR(16)  NOT NULL,
                unit_name            VARCHAR(128) NULL,
                eon                  VARCHAR(32)  NULL,
                era                  VARCHAR(64)  NULL,
                period               VARCHAR(64)  NULL,
                group_name           VARCHAR(64)  NULL,
                formation            VARCHAR(64)  NULL,
                member               VARCHAR(64)  NULL,
                structural_domain    VARCHAR(64)  NULL,
                lithology            VARCHAR(256) NULL,
                scale                VARCHAR(8)   NOT NULL DEFAULT '250K',
                source_crs           INT          NOT NULL,
                source_geom_wkt      TEXT         NULL,
                source_url           TEXT         NULL,
                source_attributes    JSONB        NOT NULL DEFAULT '{}'::jsonb,
                first_seen_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                last_seen_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                checksum             CHAR(64)     NOT NULL,
                created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_pg_bg_source UNIQUE (source_id, source_feature_id),
                CONSTRAINT fk_pg_bg_jurisdiction
                    FOREIGN KEY (jurisdiction_code)
                    REFERENCES public_geo.jurisdictions(jurisdiction_code)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_pg_bg_source
                    FOREIGN KEY (source_id)
                    REFERENCES public_geo.sources(source_id)
                    ON DELETE RESTRICT
            )
        ");
        DB::statement("SELECT AddGeometryColumn('public_geo', 'pg_bedrock_geology', 'geom', 4326, 'MULTIPOLYGON', 2)");
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_bg_jurisdiction ON public_geo.pg_bedrock_geology (jurisdiction_code)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_bg_source ON public_geo.pg_bedrock_geology (source_id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_bg_geom ON public_geo.pg_bedrock_geology USING GIST (geom)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_bg_unit_scale ON public_geo.pg_bedrock_geology (unit_code, scale)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_bg_period ON public_geo.pg_bedrock_geology (period)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_bg_formation ON public_geo.pg_bedrock_geology (formation)');

        // ── pg_bedrock_geology_history ────────────────────────────────────
        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.pg_bedrock_geology_history (
                history_id           BIGSERIAL    PRIMARY KEY,
                id                   UUID         NOT NULL,
                jurisdiction_code    VARCHAR(16)  NOT NULL,
                source_id            VARCHAR(64)  NOT NULL,
                source_feature_id    VARCHAR(128) NOT NULL,
                unit_code            VARCHAR(16)  NOT NULL,
                unit_name            VARCHAR(128) NULL,
                eon                  VARCHAR(32)  NULL,
                era                  VARCHAR(64)  NULL,
                period               VARCHAR(64)  NULL,
                group_name           VARCHAR(64)  NULL,
                formation            VARCHAR(64)  NULL,
                member               VARCHAR(64)  NULL,
                structural_domain    VARCHAR(64)  NULL,
                lithology            VARCHAR(256) NULL,
                scale                VARCHAR(8)   NOT NULL,
                source_crs           INT          NOT NULL,
                source_geom_wkt      TEXT         NULL,
                source_url           TEXT         NULL,
                source_attributes    JSONB        NOT NULL DEFAULT '{}'::jsonb,
                checksum             CHAR(64)     NOT NULL,
                superseded_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        ");
        DB::statement("SELECT AddGeometryColumn('public_geo', 'pg_bedrock_geology_history', 'geom', 4326, 'MULTIPOLYGON', 2)");
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_bg_history_id ON public_geo.pg_bedrock_geology_history (id)');
        DB::statement('CREATE INDEX IF NOT EXISTS idx_pg_bg_history_superseded ON public_geo.pg_bedrock_geology_history (superseded_at DESC)');

        // ── Martin MVT view ───────────────────────────────────────────────
        // Excludes source_attributes + source_geom_wkt to keep tile payload
        // lean. Canonical geology columns (unit_code, period, formation,
        // lithology) drive popup display and filtering in the frontend.
        DB::statement('
            CREATE OR REPLACE VIEW public_geo.v_pg_bedrock_geology_mvt AS
            SELECT
                b.id, b.jurisdiction_code, b.source_id, b.source_feature_id,
                b.unit_code, b.unit_name, b.eon, b.era, b.period,
                b.group_name, b.formation, b.member, b.structural_domain,
                b.lithology, b.scale,
                b.source_url, b.last_seen_at, b.geom
              FROM public_geo.pg_bedrock_geology b
        ');
        DB::statement("COMMENT ON VIEW public_geo.v_pg_bedrock_geology_mvt IS 'MVT tile source for Martin. Bedrock geology polygons from SK Geology/MapServer/10 (250K). Consumed by /tiles/public-geoscience/pg_bedrock_geology.'");

        // ── Expand canonical_type CHECK on sources ────────────────────────
        // Drop the constraint placed by the mineral_disposition migration and
        // re-add it with bedrock_geology appended. Final consolidated pass at
        // end of plan per §12.
        DB::statement('ALTER TABLE public_geo.sources DROP CONSTRAINT IF EXISTS sources_canonical_type_check');
        DB::statement("
            ALTER TABLE public_geo.sources
              ADD CONSTRAINT sources_canonical_type_check
              CHECK (canonical_type IN (
                'mine', 'mineral_occurrence', 'drillhole_collar',
                'resource_potential_zone', 'rock_sample', 'assessment_survey',
                'mineral_disposition', 'bedrock_geology'
              ))
        ");
    }

    public function down(): void
    {
        DB::statement('DROP VIEW IF EXISTS public_geo.v_pg_bedrock_geology_mvt');
        DB::statement('DROP TABLE IF EXISTS public_geo.pg_bedrock_geology_history CASCADE');
        DB::statement('DROP TABLE IF EXISTS public_geo.pg_bedrock_geology CASCADE');

        // Restore CHECK to pre-bedrock_geology state (mineral_disposition era).
        DB::statement('ALTER TABLE public_geo.sources DROP CONSTRAINT IF EXISTS sources_canonical_type_check');
        DB::statement("
            ALTER TABLE public_geo.sources
              ADD CONSTRAINT sources_canonical_type_check
              CHECK (canonical_type IN (
                'mine', 'mineral_occurrence', 'drillhole_collar',
                'resource_potential_zone', 'rock_sample', 'assessment_survey',
                'mineral_disposition'
              ))
        ");
    }
};
