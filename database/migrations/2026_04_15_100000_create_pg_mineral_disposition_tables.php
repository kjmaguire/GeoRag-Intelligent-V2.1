<?php

use Illuminate\Database\Migrations\Migration;
use Illuminate\Support\Facades\DB;

/**
 * Tier 2 expansion — Mineral Tenure / Dispositions.
 *
 *   pg_mineral_disposition  ← Economy/Mining/MapServer/0-8
 *                             (SK mineral/potash/alkali/coal/quarry
 *                             dispositions, legacy + modern field schemas)
 *                           ← Economy/Mineral_Tenure_Crown_Dispositions/
 *                             MapServer/8 (Oil and Gas — unique data)
 *
 * Unified multi-layer table pattern (mirrors pg_resource_potential_zone):
 *   One canonical table with `disposition_type` + `status` discriminators.
 *   Each Mining layer maps to a (disposition_type, status) combination:
 *
 *     Mining/0 → disposition_type=mineral, status=active
 *     Mining/1 → mineral / legacy
 *     Mining/2 → mineral / pending
 *     Mining/3 → mineral / reopening
 *     Mining/4 → mineral / lapsed
 *     Mining/5 → potash  / active
 *     Mining/6 → alkali  / active
 *     Mining/7 → coal    / active
 *     Mining/8 → quarry  / active
 *     CrownDispositions/8 → oil_gas / active
 *
 * CR Preclude layers (Mining/9-15) intentionally deferred — they overlap
 * with the active dispositions and would inflate counts without adding new
 * tenure-tracking semantics. Revisit if SME requests.
 *
 * Field schema note: Mining layers 0-4 use legacy cryptic column names
 * (DISPOSITIO, DISPOSIT_1, OWNERS, EFFECTIVED, GOODSTANDI) while layers 5-8
 * use clean modern names (DISPOSITION, STATUS, HOLDER, ANNIVERSARYDATE,
 * HECTARES). The Silver FieldMapping registry handles both variants.
 */
return new class extends Migration
{
    public function up(): void
    {
        // ── pg_mineral_disposition ────────────────────────────────────
        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.pg_mineral_disposition (
                id                   UUID         PRIMARY KEY,
                jurisdiction_code    VARCHAR(16)  NOT NULL,
                source_id            VARCHAR(64)  NOT NULL,
                source_feature_id    VARCHAR(128) NOT NULL,
                disposition_number   VARCHAR(64)  NULL,
                disposition_type     VARCHAR(32)  NOT NULL DEFAULT 'mineral'
                    CHECK (disposition_type IN ('mineral','potash','alkali','coal','quarry','oil_gas')),
                status               VARCHAR(32)  NOT NULL DEFAULT 'active'
                    CHECK (status IN ('active','legacy','pending','reopening','lapsed','precluded','unknown')),
                holder_name          VARCHAR(512) NULL,
                issue_date           DATE         NULL,
                expiry_date          DATE         NULL,
                area_ha              NUMERIC(14,2) NULL,
                commodity_codes      TEXT[]       NULL,
                geographic_area      VARCHAR(128) NULL,
                source_crs           INT          NOT NULL,
                source_geom_wkt      TEXT         NULL,
                source_url           TEXT         NULL,
                source_attributes    JSONB        NOT NULL DEFAULT '{}'::jsonb,
                first_seen_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                last_seen_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                checksum             CHAR(64)     NOT NULL,
                created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                updated_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
                CONSTRAINT uq_pg_md_source UNIQUE (source_id, source_feature_id),
                CONSTRAINT fk_pg_md_jurisdiction
                    FOREIGN KEY (jurisdiction_code)
                    REFERENCES public_geo.jurisdictions(jurisdiction_code)
                    ON DELETE RESTRICT,
                CONSTRAINT fk_pg_md_source
                    FOREIGN KEY (source_id)
                    REFERENCES public_geo.sources(source_id)
                    ON DELETE RESTRICT
            )
        ");
        DB::statement("SELECT AddGeometryColumn('public_geo', 'pg_mineral_disposition', 'geom', 4326, 'MULTIPOLYGON', 2)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_md_jurisdiction ON public_geo.pg_mineral_disposition (jurisdiction_code)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_md_source ON public_geo.pg_mineral_disposition (source_id)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_md_geom ON public_geo.pg_mineral_disposition USING GIST (geom)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_md_type_status ON public_geo.pg_mineral_disposition (disposition_type, status)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_md_commodity ON public_geo.pg_mineral_disposition USING GIN (commodity_codes)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_md_holder ON public_geo.pg_mineral_disposition (holder_name)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_md_disp_number ON public_geo.pg_mineral_disposition (jurisdiction_code, disposition_number)");

        DB::statement("
            CREATE TABLE IF NOT EXISTS public_geo.pg_mineral_disposition_history (
                history_id           BIGSERIAL    PRIMARY KEY,
                id                   UUID         NOT NULL,
                jurisdiction_code    VARCHAR(16)  NOT NULL,
                source_id            VARCHAR(64)  NOT NULL,
                source_feature_id    VARCHAR(128) NOT NULL,
                disposition_number   VARCHAR(64)  NULL,
                disposition_type     VARCHAR(32)  NOT NULL,
                status               VARCHAR(32)  NOT NULL,
                holder_name          VARCHAR(512) NULL,
                issue_date           DATE         NULL,
                expiry_date          DATE         NULL,
                area_ha              NUMERIC(14,2) NULL,
                commodity_codes      TEXT[]       NULL,
                geographic_area      VARCHAR(128) NULL,
                source_crs           INT          NOT NULL,
                source_geom_wkt      TEXT         NULL,
                source_url           TEXT         NULL,
                source_attributes    JSONB        NOT NULL DEFAULT '{}'::jsonb,
                checksum             CHAR(64)     NOT NULL,
                superseded_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW()
            )
        ");
        DB::statement("SELECT AddGeometryColumn('public_geo', 'pg_mineral_disposition_history', 'geom', 4326, 'MULTIPOLYGON', 2)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_md_history_id ON public_geo.pg_mineral_disposition_history (id)");
        DB::statement("CREATE INDEX IF NOT EXISTS idx_pg_md_history_superseded ON public_geo.pg_mineral_disposition_history (superseded_at DESC)");

        // Martin MVT view — drops source_attributes + source_geom_wkt to keep
        // tile payload lean. Disposition number + holder are included so
        // click-through popups can show "owner + expiry" at a glance.
        DB::statement("
            CREATE OR REPLACE VIEW public_geo.v_pg_mineral_dispositions_mvt AS
            SELECT
                d.id, d.jurisdiction_code, d.source_id, d.source_feature_id,
                d.disposition_number, d.disposition_type, d.status,
                d.holder_name, d.issue_date, d.expiry_date, d.area_ha,
                d.commodity_codes, d.geographic_area,
                d.source_url, d.last_seen_at, d.geom
              FROM public_geo.pg_mineral_disposition d
        ");
        DB::statement("COMMENT ON VIEW public_geo.v_pg_mineral_dispositions_mvt IS 'MVT tile source for Martin. Mineral tenure/dispositions polygons from SK Mining + Crown Dispositions services. Consumed by /tiles/public-geoscience/pg_mineral_dispositions.'");

        // Expand canonical_type CHECK on sources to include the new type.
        // One DROP/ADD pass per migration keeps the constraint tight at
        // every checkpoint. Final consolidated pass at end of plan.
        DB::statement("ALTER TABLE public_geo.sources DROP CONSTRAINT IF EXISTS sources_canonical_type_check");
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

    public function down(): void
    {
        DB::statement('DROP VIEW IF EXISTS public_geo.v_pg_mineral_dispositions_mvt');
        DB::statement('DROP TABLE IF EXISTS public_geo.pg_mineral_disposition_history CASCADE');
        DB::statement('DROP TABLE IF EXISTS public_geo.pg_mineral_disposition CASCADE');

        // Restore CHECK to pre-Tenure state (Tier 1 types only).
        DB::statement("ALTER TABLE public_geo.sources DROP CONSTRAINT IF EXISTS sources_canonical_type_check");
        DB::statement("
            ALTER TABLE public_geo.sources
              ADD CONSTRAINT sources_canonical_type_check
              CHECK (canonical_type IN (
                'mine', 'mineral_occurrence', 'drillhole_collar',
                'resource_potential_zone', 'rock_sample', 'assessment_survey'
              ))
        ");
    }
};
