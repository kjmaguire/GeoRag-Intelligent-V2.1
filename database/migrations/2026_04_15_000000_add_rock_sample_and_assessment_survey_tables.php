<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Tier 1 expansion — two new canonical entity types:
 *
 *   pg_rock_sample        ← Economy/Mineral_Exploration/MapServer/4
 *                           (Government Rock Samples — point locations with
 *                           station IDs, geologist, report/map references)
 *
 *   pg_assessment_survey  ← Economy/P_Mineral_Assessment_File_Information/MapServer/1-3
 *                           (SMAD survey footprints — airborne, ground,
 *                           underground polygon coverage areas)
 *
 * Both follow the same canonical-table conventions as the Phase 2.1 tables:
 *   - UUID PK, (source_id, source_feature_id) UNIQUE for upsert keying
 *   - GIST on geom, B-tree on jurisdiction_code / source_id
 *   - source_attributes JSONB for full raw attribute preservation
 *   - _history sibling table for soft-versioning on checksum drift
 */
return new class extends Migration
{
    public function up(): void
    {
        // ── pg_rock_sample ───────────────────────────────────────────
        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.pg_rock_sample (
                id                   UUID         PRIMARY KEY,
                jurisdiction_code    VARCHAR(16)  NOT NULL,
                source_id            VARCHAR(64)  NOT NULL,
                source_feature_id    VARCHAR(128) NOT NULL,
                station              VARCHAR(128) NULL,
                sample_number        VARCHAR(128) NULL,
                geologist            VARCHAR(255) NULL,
                geographic_area      VARCHAR(255) NULL,
                report_number        VARCHAR(128) NULL,
                map_number           VARCHAR(128) NULL,
                map_scale            VARCHAR(64)  NULL,
                nts_250k             VARCHAR(16)  NULL,
                nts_50k              VARCHAR(128)  NULL,
                date_collected       DATE         NULL,
                source_crs           INT          NOT NULL,
                source_geom_wkt      TEXT         NULL,
                source_url           TEXT         NULL,
                source_attributes    JSONB        NOT NULL DEFAULT '{}'::jsonb,
                first_seen_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                last_seen_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                checksum             CHAR(64)     NOT NULL,
                created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_pg_rock_sample_source UNIQUE (source_id, source_feature_id),
                CONSTRAINT fk_pg_rs_jurisdiction
                    FOREIGN KEY (jurisdiction_code)
                    REFERENCES public_geo.jurisdictions(jurisdiction_code)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_pg_rs_source
                    FOREIGN KEY (source_id)
                    REFERENCES public_geo.sources(source_id)
                    ON DELETE RESTRICT
            )
        ");
        DB::statement("SELECT AddGeometryColumn('public_geo', 'pg_rock_sample', 'geom', 4326, 'POINT', 2)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_rs_jurisdiction ON public_geo.pg_rock_sample (jurisdiction_code)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_rs_source ON public_geo.pg_rock_sample (source_id)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_rs_geom ON public_geo.pg_rock_sample USING GIST (geom)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_rs_nts ON public_geo.pg_rock_sample (nts_250k)");

        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.pg_rock_sample_history (
                history_id           BIGSERIAL    PRIMARY KEY,
                id                   UUID         NOT NULL,
                jurisdiction_code    VARCHAR(16)  NOT NULL,
                source_id            VARCHAR(64)  NOT NULL,
                source_feature_id    VARCHAR(128) NOT NULL,
                station              VARCHAR(128) NULL,
                sample_number        VARCHAR(128) NULL,
                geologist            VARCHAR(255) NULL,
                geographic_area      VARCHAR(255) NULL,
                report_number        VARCHAR(128) NULL,
                map_number           VARCHAR(128) NULL,
                map_scale            VARCHAR(64)  NULL,
                nts_250k             VARCHAR(16)  NULL,
                nts_50k              VARCHAR(128)  NULL,
                date_collected       DATE         NULL,
                source_crs           INT          NOT NULL,
                source_geom_wkt      TEXT         NULL,
                source_url           TEXT         NULL,
                source_attributes    JSONB        NOT NULL DEFAULT '{}'::jsonb,
                checksum             CHAR(64)     NOT NULL,
                superseded_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        ");
        DB::statement("SELECT AddGeometryColumn('public_geo', 'pg_rock_sample_history', 'geom', 4326, 'POINT', 2)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_rs_history_id ON public_geo.pg_rock_sample_history (id)");

        // Martin MVT view
        DB::statement("
            CREATE OR REPLACE VIEW public_geo.v_pg_rock_samples_mvt AS
            SELECT
                r.id, r.jurisdiction_code, r.source_id, r.source_feature_id,
                r.station, r.sample_number, r.geologist, r.geographic_area,
                r.report_number, r.nts_250k, r.last_seen_at, r.geom
              FROM public_geo.pg_rock_sample r
        ");

        // ── pg_assessment_survey ─────────────────────────────────────
        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.pg_assessment_survey (
                id                   UUID         PRIMARY KEY,
                jurisdiction_code    VARCHAR(16)  NOT NULL,
                source_id            VARCHAR(64)  NOT NULL,
                source_feature_id    VARCHAR(128) NOT NULL,
                survey_type          VARCHAR(32)  NOT NULL DEFAULT 'unknown'
                    CHECK (survey_type IN ('airborne','ground','underground','unknown')),
                source_crs           INT          NOT NULL,
                source_geom_wkt      TEXT         NULL,
                source_url           TEXT         NULL,
                source_attributes    JSONB        NOT NULL DEFAULT '{}'::jsonb,
                first_seen_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                last_seen_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                checksum             CHAR(64)     NOT NULL,
                created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_pg_as_source UNIQUE (source_id, source_feature_id),
                CONSTRAINT fk_pg_as_jurisdiction
                    FOREIGN KEY (jurisdiction_code)
                    REFERENCES public_geo.jurisdictions(jurisdiction_code)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_pg_as_source
                    FOREIGN KEY (source_id)
                    REFERENCES public_geo.sources(source_id)
                    ON DELETE RESTRICT
            )
        ");
        DB::statement("SELECT AddGeometryColumn('public_geo', 'pg_assessment_survey', 'geom', 4326, 'MULTIPOLYGON', 2)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_as_jurisdiction ON public_geo.pg_assessment_survey (jurisdiction_code)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_as_source ON public_geo.pg_assessment_survey (source_id)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_as_geom ON public_geo.pg_assessment_survey USING GIST (geom)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_as_type ON public_geo.pg_assessment_survey (survey_type)");

        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.pg_assessment_survey_history (
                history_id           BIGSERIAL    PRIMARY KEY,
                id                   UUID         NOT NULL,
                jurisdiction_code    VARCHAR(16)  NOT NULL,
                source_id            VARCHAR(64)  NOT NULL,
                source_feature_id    VARCHAR(128) NOT NULL,
                survey_type          VARCHAR(32)  NOT NULL DEFAULT 'unknown',
                source_crs           INT          NOT NULL,
                source_geom_wkt      TEXT         NULL,
                source_url           TEXT         NULL,
                source_attributes    JSONB        NOT NULL DEFAULT '{}'::jsonb,
                checksum             CHAR(64)     NOT NULL,
                superseded_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        ");
        DB::statement("SELECT AddGeometryColumn('public_geo', 'pg_assessment_survey_history', 'geom', 4326, 'MULTIPOLYGON', 2)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_as_history_id ON public_geo.pg_assessment_survey_history (id)");

        // Martin MVT view
        DB::statement("
            CREATE OR REPLACE VIEW public_geo.v_pg_assessment_surveys_mvt AS
            SELECT
                a.id, a.jurisdiction_code, a.source_id, a.source_feature_id,
                a.survey_type, a.last_seen_at, a.geom
              FROM public_geo.pg_assessment_survey a
        ");

        // Update canonical_type CHECK on sources table to include new types
        // (existing CHECK only allows the original 4 types)
        DB::statement("
            ALTER TABLE public_geo.sources
              DROP CONSTRAINT IF EXISTS sources_canonical_type_check
        ");
        DB::statement("
            ALTER TABLE public_geo.sources
              ADD CONSTRAINT sources_canonical_type_check
              CHECK (canonical_type IN (
                'mine', 'mineral_occurrence', 'drillhole_collar',
                'resource_potential_zone', 'rock_sample', 'assessment_survey'
              ))
        ");
    }

    public function down(): void
    {
        DB::statement('DROP VIEW IF EXISTS public_geo.v_pg_assessment_surveys_mvt');
        DB::statement('DROP VIEW IF EXISTS public_geo.v_pg_rock_samples_mvt');
        DB::statement('DROP TABLE IF EXISTS public_geo.pg_assessment_survey_history CASCADE');
        DB::statement('DROP TABLE IF EXISTS public_geo.pg_assessment_survey CASCADE');
        DB::statement('DROP TABLE IF EXISTS public_geo.pg_rock_sample_history CASCADE');
        DB::statement('DROP TABLE IF EXISTS public_geo.pg_rock_sample CASCADE');

        // Restore original CHECK constraint
        DB::statement("ALTER TABLE public_geo.sources DROP CONSTRAINT IF EXISTS sources_canonical_type_check");
        DB::statement("
            ALTER TABLE public_geo.sources
              ADD CONSTRAINT sources_canonical_type_check
              CHECK (canonical_type IN (
                'mine', 'mineral_occurrence', 'drillhole_collar', 'resource_potential_zone'
              ))
        ");
    }
};
